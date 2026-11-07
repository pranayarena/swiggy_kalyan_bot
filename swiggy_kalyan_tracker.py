#!/usr/bin/env python3
"""Kalyan gold coin price tracker for Swiggy Instamart -> Telegram alerts.

Pure Python standard library. No Node.js, no api-anything, no browser, no pip packages.
Small on RAM (about 30-60 MB): it sends one plain HTTPS POST per check to Instamart's own search API.

Env vars (or a .env file next to this script):
  TELEGRAM_BOT_TOKEN   token from @BotFather
  TELEGRAM_CHAT_ID     your chat id (run: python swiggy_kalyan_tracker.py --get-chat-id)
  CHECK_INTERVAL_SEC   seconds between checks, default 60, minimum 30
  SEARCH_QUERY         default "kalyan gold coin"
  TRACK_WORDS          comma list, ALL must appear in item name+brand. default "kalyan,gold coin"
  STATE_FILE           default kalyan_state.json (lost on restart on hosts without a disk)
  INSTAMART_COOKIE     optional: a Cookie header string to pin your delivery location
  NOTIFY_ERRORS        default 0 = Telegram gets ONLY price changes and new SKUs. 1 = also failure warnings.
  X_BUILD_VERSION      optional: Instamart web build, default 2.392.0
"""
import gzip
import http.cookiejar
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
MIN_INTERVAL = 30  # faster than this gets HTTP 429 and may get the IP blocked
MISSES_BEFORE_GONE = 3
HOME = "https://instamart.in/"
SEARCH_URL = ("https://instamart.in/api/instamart/search/v2?offset=0&ageConsent=false"
              "&voiceSearchTrackingId=&storeId=&primaryStoreId=&secondaryStoreId=")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/157.0.0.0 Safari/537.36")


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
COOKIE = os.environ.get("INSTAMART_COOKIE", "").strip()
NOTIFY_ERRORS = os.environ.get("NOTIFY_ERRORS", "0").strip() == "1"  # 1 = also Telegram a warning when checks keep failing
BUILD = os.environ.get("X_BUILD_VERSION", "2.392.0").strip()
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
    for _ in range(2):
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


# ---------- Instamart (plain HTTP) ----------
class Blocked(Exception):
    """Instamart refused: 429 rate limit, 403/405 bot check, or an HTML challenge page."""


class Session:
    """Cookie-keeping HTTP session that looks like Chrome. Reset it when blocked."""

    def __init__(self, variant="bare"):
        # bare    = no cookies, browser-like headers, no home-page visit
        # minimal = no cookies, only User-Agent + JSON headers (closest to the call that worked in a browser)
        # cookies = visit the home page first and keep its cookies
        self.variant = variant
        self.jar = http.cookiejar.CookieJar()
        if variant == "cookies":
            self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        else:
            self.opener = urllib.request.build_opener()
        self.warm = variant != "cookies"
        self.last = ""

    def _headers(self, extra=None):
        if self.variant == "minimal":
            h = {"User-Agent": UA, "Accept-Encoding": "gzip"}
            h.update(extra or {})
            return h
        h = {"User-Agent": UA, "Accept-Encoding": "gzip", "Accept-Language": "en-IN,en;q=0.9",
             "sec-ch-ua": '"Not-A.Brand";v="99", "Google Chrome";v="157", "Chromium";v="157"',
             "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"'}
        if COOKIE:
            h["Cookie"] = COOKIE
        h.update(extra or {})
        return h

    def _read(self, resp):
        raw = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", errors="replace")

    def _note(self, status, headers, body):
        """Remember a short, secret-free summary of the last response, for the failure log."""
        keep = {k.lower(): v for k, v in headers.items()
                if k.lower() in ("server", "via", "retry-after", "content-type", "content-length", "x-cache")
                or k.lower().startswith(("x-amzn", "x-waf", "cf-"))}
        self.last = f"status={status} headers={keep} body={body[:120]!r} cookies={[c.name for c in self.jar]}"

    def _open(self, req):
        try:
            with self.opener.open(req, timeout=30) as r:
                body = self._read(r)
                self._note(r.status, r.headers, body)
                return r.status, body
        except urllib.error.HTTPError as e:
            try:
                body = self._read(e)
            except Exception:  # noqa: BLE001
                body = ""
            self._note(e.code, e.headers, body)
            return e.code, body

    def warm_up(self):
        """Open the home page once so the session holds the same cookies a visitor would."""
        req = urllib.request.Request(HOME, headers=self._headers({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1"}))
        status, _ = self._open(req)
        if status in (403, 405, 429):
            raise Blocked(f"home page HTTP {status}")
        self.warm = True

    def search(self):
        if not self.warm:
            self.warm_up()
            time.sleep(random.uniform(1.0, 2.5))
        body = json.dumps({"facets": [], "sortAttribute": "", "query": QUERY, "search_results_offset": "0",
                           "page_type": "INSTAMART_SEARCH_PAGE", "is_pre_search_tag": False}).encode()
        extra = {"Accept": "*/*", "Content-Type": "application/json"}
        if self.variant != "minimal":
            extra.update({"Origin": "https://instamart.in",
                          "Referer": "https://instamart.in/search?custom_back=true&query=" + urllib.parse.quote(QUERY),
                          "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"})
        if self.variant == "cookies":
            extra["x-build-version"] = BUILD
        req = urllib.request.Request(SEARCH_URL, data=body, method="POST", headers=self._headers(extra))
        status, text = self._open(req)
        if status in (403, 405, 429) or status >= 500:
            raise Blocked(f"HTTP {status}")
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            raise Blocked(f"HTTP {status}, not JSON (bot check page?): {text[:80]!r}")
        if isinstance(doc, dict) and doc.get("statusCode") == 429:
            raise Blocked("rate limited (429 inside JSON)")
        return doc


VARIANTS = ["bare", "minimal", "cookies"]
PROVEN = set()  # request styles that have returned real data in this run


def search_any(prefer):
    """Try the preferred request style first, then the others, a few seconds apart (max 3 requests)."""
    if prefer in PROVEN:  # a style already worked: a 429 now is a plain rate limit, so just back off
        return Session(prefer).search(), prefer
    i = VARIANTS.index(prefer)
    last = None
    for v in VARIANTS[i:] + VARIANTS[:i]:
        try:
            doc = Session(v).search()
            PROVEN.add(v)
            return doc, v
        except Blocked as e:
            log(f"Request style '{v}' refused: {e}")
            last = e
            time.sleep(random.uniform(3, 6))
    raise last


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


def parse_items(doc):
    """Return {sku: {...}} for items whose name+brand contain every TRACK_WORDS entry."""
    found = {}
    for it in walk(doc):
        name, brand = it.get("displayName", ""), it.get("brand", "")
        for var in it["variations"]:
            price = var.get("price") or {}
            offer = to_int((price.get("offerPrice") or {}).get("units"))
            mrp = to_int((price.get("mrp") or {}).get("units"))
            sku = var.get("skuId")
            if not sku or offer is None:
                continue
            stock = bool(it.get("inStock")) and bool((var.get("inventory") or {}).get("inStock", True))
            found[sku] = {"name": var.get("displayName") or name, "brand": brand or var.get("brandName", ""),
                          "qty": var.get("quantityDescription", ""), "price": offer, "mrp": mrp,
                          "in_stock": stock}
    return {s: d for s, d in found.items() if all(w in (d["name"] + " " + d["brand"]).lower() for w in WORDS)}


# ---------- state + alerts ----------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log(f"Could not save state ({e}). Alerts still work until restart.")


def inr(n):
    return f"₹{n:,}"


def link():
    return "https://instamart.in/search?query=" + urllib.parse.quote(QUERY)


def short(name):
    return name.replace("Kalyan Jewellers ", "").strip()


def compare(state, current):
    """Update state. Return ONE batched message (or None) for price changes and new SKUs only."""
    had_state = bool(state)  # decide once: the first scan must stay silent, not flag every item as new
    changed, new = [], []
    for sku, cur in current.items():
        old = state.get(sku)
        if old is None:
            if had_state:
                new.append(f"🆕 NEW: {short(cur['name'])}\n    {inr(cur['price'])} (MRP {inr(cur['mrp'] or 0)}) · SKU {sku}")
        elif cur["price"] != old["price"]:
            diff = cur["price"] - old["price"]
            pct = diff / old["price"] * 100 if old["price"] else 0
            icon = "🔺" if diff > 0 else "🔻"
            sign = "+" if diff > 0 else "-"
            changed.append(f"{icon} {short(cur['name'])}\n    {inr(old['price'])} → {inr(cur['price'])}  "
                           f"({sign}{inr(abs(diff))} | {pct:+.1f}%)")
        state[sku] = {**cur, "misses": 0}
    for sku, old in state.items():
        if sku not in current:
            old["misses"] = old.get("misses", 0) + 1  # kept only in the state file; no alert
    if not changed and not new:
        return None
    parts = ["💰 Kalyan gold coin update"] + changed + new + [link()]
    return "\n".join(parts)


def send_long(text):
    """Telegram allows 4096 characters per message: split on line breaks if needed."""
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800:
            send(chunk)
            chunk = ""
        chunk += ("\n" if chunk else "") + line
    if chunk:
        send(chunk)


def main():
    if "--get-chat-id" in sys.argv:
        return get_chat_id()
    log(f"Tracking '{QUERY}' (match: {WORDS}) every ~{INTERVAL}s. Plain HTTP, no browser. State: {STATE_FILE}")
    state = load_state()
    prefer = VARIANTS[0]
    fails, warned, scans = 0, False, 0
    while True:
        wait = INTERVAL
        try:
            doc, used = search_any(prefer)
            if used != prefer or fails or scans == 0:
                log(f"Request style that works: '{used}'")
            prefer = used
            current = parse_items(doc)
            scans += 1
            if not current:
                log("Search worked but no matching items this scan (items gone, or a different store).")
            else:
                message = compare(state, current)
                save_state(state)
                if message:
                    send_long(message)  # the ONLY thing sent to Telegram: price change or new SKU
                if scans == 1 or message:
                    log(f"OK: {len(current)} matching item(s)." + (" Change sent." if message else " Baseline saved."))
            if warned and NOTIFY_ERRORS:
                send("✅ Tracker recovered.")
            fails, warned = 0, False
        except Exception as e:  # noqa: BLE001
            fails += 1
            wait = min(INTERVAL * (2 ** (fails - 1)), 600)  # gentle: 1x, 2x, 4x ... capped at 10 min
            log(f"Check failed ({fails}): {e}. Next try in ~{wait}s")
            if fails == 5 and not warned:
                warned = True
                if NOTIFY_ERRORS:
                    send(f"⚠️ Tracker failing 5x in a row: {e}")
        time.sleep(wait * random.uniform(0.9, 1.1))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped.")
