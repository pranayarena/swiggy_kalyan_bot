#!/usr/bin/env python3
"""Kalyan gold coin price tracker for Swiggy Instamart -> Telegram alerts.

Needs only Python 3.8+ (no pip packages). Uses the api-anything CLI
(`npx api-anything call instamart findProducts query=...`) to read Instamart,
because Instamart blocks plain scripted requests.

Three ways to read Instamart:
  C) BROWSER (no Node.js, best for servers): USE_BROWSER=1. Runs headless Chromium through Playwright,
     so Instamart's AWS WAF anti-bot check passes. Set INSTAMART_LAT / INSTAMART_LON.
  A) DIRECT (no Node.js): put a Chrome "Copy as cURL (bash)" of the Instamart search request in
     instamart_curl.txt next to this script, or in env var INSTAMART_CURL.
  B) api-anything CLI (needs Node.js) when no cURL is given.

Env vars (or a .env file next to this script):
  TELEGRAM_BOT_TOKEN   token from @BotFather
  TELEGRAM_CHAT_ID     your chat id (run: python kalyan_tracker.py --get-chat-id)
  CHECK_INTERVAL_SEC   seconds between checks, default 60, minimum 30
  SEARCH_QUERY         default "kalyan gold coin"
  TRACK_WORDS          comma list, ALL must appear in item name+brand. default "kalyan,gold coin"
  STATE_FILE           default kalyan_state.json
  API_ANYTHING_EXTRA   extra CLI args, optional (space separated)
  INSTAMART_CURL       the cURL text (direct mode); ORIGINAL_QUERY = the search text inside it
"""
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
MIN_INTERVAL = 30  # below this Instamart returns 429 and may block your IP
MISSES_BEFORE_GONE = 3


def load_env_file():
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env_file()

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
QUERY = os.environ.get("SEARCH_QUERY", "kalyan gold coin")
WORDS = [w.strip().lower() for w in os.environ.get("TRACK_WORDS", "kalyan,gold coin").split(",") if w.strip()]
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(HERE, "kalyan_state.json"))
EXTRA = os.environ.get("API_ANYTHING_EXTRA", "").split()
try:
    INTERVAL = max(MIN_INTERVAL, int(os.environ.get("CHECK_INTERVAL_SEC", "60")))
except ValueError:
    INTERVAL = 60


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------- Telegram ----------
def tg_api(method, payload=None):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def send(text):
    log("ALERT: " + text.replace("\n", " | "))
    if not TOKEN or not CHAT_ID:
        log("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID). Printed only.")
        return
    for attempt in range(2):
        try:
            tg_api("sendMessage", {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True})
            return
        except Exception as e:  # noqa: BLE001
            log(f"Telegram send failed ({e})")
            time.sleep(3)


def get_chat_id():
    if not TOKEN:
        sys.exit("Set TELEGRAM_BOT_TOKEN first. Then message your bot once, then run this again.")
    res = tg_api("getUpdates")
    seen = {}
    for u in res.get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            seen[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    if not seen:
        sys.exit("No messages found. Open your bot in Telegram, press Start, send 'hi', run again.")
    for cid, name in seen.items():
        print(f"chat id: {cid}  ({name})")


# ---------- Instamart via api-anything ----------
def load_curl_text():
    """cURL text from env INSTAMART_CURL, or file instamart_curl.txt next to the script."""
    txt = os.environ.get("INSTAMART_CURL", "").strip()
    if txt:
        return txt
    path = os.path.join(HERE, "instamart_curl.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def parse_curl(text):
    """Parse a Chrome 'Copy as cURL (bash)' command -> (method, url, headers, body)."""
    text = text.replace("\\\r\n", " ").replace("\\\n", " ")
    parts = shlex.split(text, posix=True)
    url, method, body, headers = None, None, None, {}
    i = 1 if parts and parts[0] == "curl" else 0
    while i < len(parts):
        a = parts[i]
        nxt = parts[i + 1] if i + 1 < len(parts) else ""
        if a in ("-H", "--header"):
            k, _, v = nxt.partition(":")
            headers[k.strip()] = v.strip()
            i += 2
        elif a in ("-b", "--cookie"):
            headers["Cookie"] = nxt
            i += 2
        elif a in ("-X", "--request"):
            method = nxt.upper()
            i += 2
        elif a in ("--data", "--data-raw", "--data-binary", "--data-ascii", "-d"):
            body = nxt
            i += 2
        elif a.startswith("-"):
            i += 1  # flags like --compressed
        else:
            url = a
            i += 1
    if not url:
        raise RuntimeError("No URL found in the cURL text.")
    if method is None:
        method = "POST" if body is not None else "GET"
    for k in list(headers):
        if k.lower() in ("accept-encoding", "content-length", "host"):
            del headers[k]  # we read plain responses; urllib sets the rest
    return method, url, headers, body


def run_direct(curl_text):
    method, url, headers, body = parse_curl(curl_text)
    orig = os.environ.get("ORIGINAL_QUERY", "kalyan gold coin")
    if orig != QUERY:
        url = url.replace(urllib.parse.quote(orig), urllib.parse.quote(QUERY)).replace(orig, QUERY)
        if body:
            body = body.replace(orig, QUERY)
    req = urllib.request.Request(url, data=body.encode() if body is not None else None,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 429):
            raise RuntimeError(f"Instamart answered HTTP {e.code}. If 401/403 the cURL cookies/tokens "
                               f"expired or the IP is blocked: copy a fresh cURL.") from e
        raise RuntimeError(f"Instamart HTTP {e.code}") from e


class BrowserSession:
    """One headless Chromium kept alive between checks (keeps the AWS WAF token and cookies).

    Needs: pip install playwright && playwright install chromium (the Dockerfile does both).
    Set INSTAMART_LAT / INSTAMART_LON to your delivery spot so Instamart shows your local store.
    """

    def __init__(self):
        self.pw = self.browser = self.ctx = self.page = None

    def start(self):
        from playwright.sync_api import sync_playwright  # imported late: only needed in browser mode
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        opts = dict(locale="en-IN", timezone_id="Asia/Kolkata", viewport={"width": 1366, "height": 900})
        lat, lon = os.environ.get("INSTAMART_LAT"), os.environ.get("INSTAMART_LON")
        if lat and lon:
            opts.update(geolocation={"latitude": float(lat), "longitude": float(lon)}, permissions=["geolocation"])
        self.ctx = self.browser.new_context(**opts)
        self.page = self.ctx.new_page()

    def close(self):
        for obj in (self.browser, self.pw):
            try:
                if obj:
                    obj.close() if obj is self.browser else obj.stop()
            except Exception:  # noqa: BLE001
                pass
        self.pw = self.browser = self.ctx = self.page = None

    def fetch(self):
        if not self.page:
            self.start()
        hits = []

        def on_resp(r):
            if "/api/instamart/search/v2" in r.url and r.request.method == "POST":
                hits.append(r)

        self.page.on("response", on_resp)
        try:
            self.page.goto("https://instamart.in/search?custom_back=true&query=" + urllib.parse.quote(QUERY),
                           wait_until="domcontentloaded", timeout=60000)
            for _ in range(30):  # wait up to ~30s for the search call
                if hits:
                    break
                self.page.wait_for_timeout(1000)
            if not hits:
                raise RuntimeError("Search call never happened (page blocked, location prompt, or WAF challenge).")
            return hits[-1].text()
        finally:
            self.page.remove_listener("response", on_resp)


_BROWSER = None


def run_browser():
    global _BROWSER
    if _BROWSER is None:
        _BROWSER = BrowserSession()
    try:
        return _BROWSER.fetch()
    except Exception:
        _BROWSER.close()  # next cycle starts a fresh browser
        raise


def run_api_anything():
    if os.environ.get("USE_BROWSER", "0") == "1":
        return run_browser()
    curl_text = load_curl_text()
    if curl_text:
        return run_direct(curl_text)
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        raise RuntimeError("npx not found. Install Node.js.")
    cmd = [npx, "-y", "api-anything", "call", "instamart", "findProducts", f"query={QUERY}"] + EXTRA
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    return p.stdout or ""


def to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def walk(obj):
    """Yield every dict that looks like a product item."""
    if isinstance(obj, dict):
        if "displayName" in obj and isinstance(obj.get("variations"), list):
            yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def parse_items(raw):
    """Return (items, rate_limited). items: {sku: {...}}."""
    start = raw.find('{"ok"')
    if start < 0:
        start = raw.find("{")
    body = raw[start:] if start >= 0 else ""
    items = {}
    try:
        doc = json.loads(body)
    except json.JSONDecodeError:
        doc = None
    if doc is not None:
        if doc.get("ok") is False or doc.get("class") in ("rate", "auth"):
            return {}, True
        for it in walk(doc):
            name = it.get("displayName", "")
            brand = it.get("brand", "")
            for var in it["variations"]:
                price = var.get("price") or {}
                offer = to_int((price.get("offerPrice") or {}).get("units"))
                mrp = to_int((price.get("mrp") or {}).get("units"))
                stock = bool(it.get("inStock")) and bool((var.get("inventory") or {}).get("inStock", True))
                sku = var.get("skuId")
                if sku and offer is not None:
                    items[sku] = {"name": var.get("displayName") or name, "brand": brand,
                                  "qty": var.get("quantityDescription", ""), "price": offer,
                                  "mrp": mrp, "in_stock": stock}
    else:
        # regex fallback for cut-off / odd output
        pat = re.compile(r'"displayName":"([^"]+)","brand":"([^"]*)".*?"skuId":"(\w+)".*?'
                         r'"mrp":\{"currencyCode":"INR","units":"(\d+)".*?'
                         r'"offerPrice":\{"currencyCode":"INR","units":"(\d+)"', re.S)
        for name, brand, sku, mrp, offer in pat.findall(body):
            items[sku] = {"name": name, "brand": brand, "qty": "", "price": int(offer),
                          "mrp": int(mrp), "in_stock": True}
        if not items and ("429" in body or "statusCode" in body and "stack" in body):
            return {}, True
    if not items and re.search(r'"statusCode"\s*:\s*429', body):
        return {}, True
    wanted = {}
    for sku, d in items.items():
        hay = (d["name"] + " " + d["brand"]).lower()
        if all(w in hay for w in WORDS):
            wanted[sku] = d
    return wanted, False


# ---------- state ----------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def inr(n):
    return f"₹{n:,}"


def link():
    return "https://instamart.in/search?query=" + urllib.parse.quote(QUERY)


def compare(state, current):
    alerts = []
    for sku, cur in current.items():
        old = state.get(sku)
        label = f"{cur['name']}\nSKU {sku}"
        if old is None:
            if state:  # not the first run
                alerts.append(f"🆕 NEW LISTING\n{label}\n{inr(cur['price'])} (MRP {inr(cur['mrp'] or 0)})\n{link()}")
        else:
            if cur["price"] != old["price"]:
                diff = cur["price"] - old["price"]
                pct = diff / old["price"] * 100 if old["price"] else 0
                icon, word = ("🔺", "PRICE UP") if diff > 0 else ("🔻", "PRICE DOWN")
                alerts.append(f"{icon} {word}\n{label}\n{inr(old['price'])} → {inr(cur['price'])} "
                              f"({'+' if diff > 0 else '-'}{inr(abs(diff))}, {pct:+.1f}%)\n{link()}")
            if cur["in_stock"] != old.get("in_stock", True):
                alerts.append(("✅ BACK IN STOCK" if cur["in_stock"] else "⛔ OUT OF STOCK") + f"\n{label}\n{inr(cur['price'])}")
        state[sku] = {**cur, "misses": 0}
    for sku, old in list(state.items()):
        if sku not in current:
            old["misses"] = old.get("misses", 0) + 1
            if old["misses"] == MISSES_BEFORE_GONE:
                alerts.append(f"❓ NOT SHOWING IN SEARCH\n{old['name']}\nSKU {sku}\nLast price {inr(old['price'])}")
    return alerts


def main():
    if "--get-chat-id" in sys.argv:
        return get_chat_id()
    log(f"Tracking '{QUERY}' (match: {WORDS}) every ~{INTERVAL}s. State: {STATE_FILE}")
    state = load_state()
    fails = 0
    warned = False
    first = not state
    while True:
        wait = INTERVAL
        try:
            raw = run_api_anything()
            current, limited = parse_items(raw)
            if limited:
                raise RuntimeError("Instamart rate-limited or blocked the call (429/auth)")
            if not current:
                log("No matching items in this scan. Output may be cut off or item is gone.")
            else:
                alerts = compare(state, current)
                save_state(state)
                if first:
                    lines = "\n".join(f"• {d['name']} — {inr(d['price'])}" for d in current.values())
                    send(f"📈 Tracking started ({len(current)} item(s)). Check every ~{INTERVAL}s.\n{lines}")
                    first = False
                for a in alerts:
                    send(a)
            if fails >= 5 and warned:
                send("✅ Tracker recovered.")
            fails, warned = 0, False
        except Exception as e:  # noqa: BLE001
            fails += 1
            wait = min(INTERVAL * (2 ** fails), 1800)
            log(f"Check failed ({fails}): {e}. Next try in {wait}s")
            if fails == 5 and not warned:
                send(f"⚠️ Tracker failing 5x in a row: {e}")
                warned = True
        time.sleep(wait * random.uniform(0.9, 1.1))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped.")
