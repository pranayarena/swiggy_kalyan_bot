#!/usr/bin/env python3
"""Kalyan gold coin price tracker for Swiggy Instamart, many cities -> Telegram alerts.

Pure Python standard library: no Node.js, no browser, no pip packages. Low RAM.
For every city it picks the delivery spot the way the Instamart website does (place search ->
coordinates -> select location), then reads that city's store search. Prices and stock differ
per store, so each city is tracked on its own.

Telegram gets ONLY: price changes (with the amount, up/down) and new coin listings.
One combined message per round of checks. Send /status to the bot for a health summary.

Test first, nothing is sent:   python swiggy_kalyan_tracker.py --once
Find your chat id:             python swiggy_kalyan_tracker.py --get-chat-id

Env vars (or a .env file next to this script):
  TELEGRAM_BOT_TOKEN   token from @BotFather
  TELEGRAM_CHAT_ID     chat id, or @channelusername (the bot must be channel admin)
  TELEGRAM_ADMIN_ID    optional: your numeric Telegram user id(s), comma separated, allowed to use /status
  LOCATIONS            optional: "Label=search text;Label2=search text2". Search text can be an area,
                       "pincode city" or an address. Default list: see DEFAULT_LOCATIONS below.
  CHECK_INTERVAL_SEC   seconds for one full round over all cities, default 300, minimum 60
  SEARCH_QUERY         default "kalyan gold coin"
  TRACK_WORDS          comma list, ALL must appear in item name+brand. default "kalyan,gold coin"
  STATE_FILE           default kalyan_state_cities.json (lost on restart on hosts without a disk)
  ENABLE_COMMANDS      default 1 = answer /status. Use 0 on a second copy (PC + Voroa) to avoid clashes.
  NOTIFY_ERRORS        default 0 = Telegram gets ONLY price changes and new SKUs. 1 = also failure warnings.
"""
import gzip
import http.cookiejar
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://instamart.in"
SEARCH_URL = (BASE + "/api/instamart/search/v2?offset=0&ageConsent=false&voiceSearchTrackingId="
              "&storeId=&primaryStoreId=&secondaryStoreId=")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/157.0.0.0 Safari/537.36")
IST = timezone(timedelta(hours=5, minutes=30))

# Tier 1 and tier 2 cities. Instamart does not serve every one of them; a city with no store just shows
# "0 coins" in /status. Replace with your own areas or pincodes through the LOCATIONS variable.
DEFAULT_LOCATIONS = (
    "Mumbai=Andheri West, Mumbai;Pune=Koregaon Park, Pune;Nashik=Nashik Road;"
    "Bengaluru=Koramangala, Bengaluru;Chennai=T Nagar, Chennai;Hyderabad=Banjara Hills, Hyderabad;"
    "Delhi=Connaught Place, New Delhi;Kolkata=Salt Lake, Kolkata;Ahmedabad=Satellite, Ahmedabad;"
    "Surat=Adajan, Surat;Vadodara=Alkapuri, Vadodara;Rajkot=Kalawad Road, Rajkot;"
    "Jaipur=Malviya Nagar, Jaipur;Kochi=Edappally, Kochi"
)


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
ADMIN_IDS = {x.strip() for x in os.environ.get("TELEGRAM_ADMIN_ID", "").split(",") if x.strip()}
QUERY = os.environ.get("SEARCH_QUERY", "kalyan gold coin")
WORDS = [w.strip().lower() for w in os.environ.get("TRACK_WORDS", "kalyan,gold coin").split(",") if w.strip()]
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(HERE, "kalyan_state_cities.json"))
BUILD = os.environ.get("X_BUILD_VERSION", "2.392.0").strip()
NOTIFY_ERRORS = os.environ.get("NOTIFY_ERRORS", "0").strip() == "1"
try:
    INTERVAL = max(60, int(os.environ.get("CHECK_INTERVAL_SEC", "300")))
except ValueError:
    INTERVAL = 300
DRY = "--once" in sys.argv  # dry run: print only, send nothing, save nothing


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def parse_locations():
    raw = os.environ.get("LOCATIONS", "").strip() or DEFAULT_LOCATIONS
    out = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        label, _, text = part.partition("=")
        out.append((label.strip(), (text or label).strip()))
    return out


# ---------- Telegram ----------
def tg_api(method, payload=None, timeout=20):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def send(text):
    if DRY or not TOKEN or not CHAT_ID:
        log("ALERT (not sent): " + text.replace("\n", " | "))
        return
    log("ALERT sent: " + text.split("\n")[0])
    for _ in range(2):
        try:
            tg_api("sendMessage", {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True})
            return
        except Exception as e:  # noqa: BLE001
            log(f"Telegram send failed ({e})")
            time.sleep(3)


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


def get_chat_id():
    if not TOKEN:
        sys.exit("Set TELEGRAM_BOT_TOKEN first. Then message your bot once, then run this again.")
    seen = {}
    for u in tg_api("getUpdates").get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        if msg.get("chat"):
            c = msg["chat"]
            seen[c["id"]] = c.get("username") or c.get("title") or c.get("first_name")
    if not seen:
        sys.exit("No messages found. Open your bot in Telegram, press Start, send 'hi', run again.")
    for cid, name in seen.items():
        print(f"chat id: {cid}  ({name})")


# ---------- Instamart ----------
class Blocked(Exception):
    """Instamart refused: 429 rate limit, 403/405 bot check, empty or non-JSON reply."""


class NoPlace(Exception):
    """The place search found nothing for this location text."""


ALPHA = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _rand5():
    """Same number the site's JavaScript makes: a random value above 10000."""
    e = int(random.random() * 1e5 + 0.5)
    for _ in range(11):
        if e < 1e4:
            e *= 10
        if e > 1e4:
            return e
    return 99999


def make_matcher():
    """The site signs every API call with a 'matcher' header: random + time + random, letters shifted by 7."""
    raw = f"{_rand5()}{int(time.time() * 1000)}{_rand5()}"
    return "".join(ALPHA[(ALPHA.index(c) + 7) % len(ALPHA)] if c in ALPHA else c for c in raw)


class City:
    """One delivery spot: its own cookies, its own store. Rebuilt from scratch after any failure."""

    def __init__(self, label, text):
        self.label, self.text = label, text
        self.reset()

    def reset(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.ready = False
        self.address = ""

    def _device_id(self):
        for c in self.jar:
            if c.name == "deviceId":
                return c.value
        return ""

    def _fetch(self, req):
        try:
            with self.opener.open(req, timeout=30) as r:
                status, raw, enc = r.status, r.read(), r.headers.get("Content-Encoding", "")
        except urllib.error.HTTPError as e:
            status, raw, enc = e.code, e.read(), e.headers.get("Content-Encoding", "")
        if enc.lower() == "gzip":
            raw = gzip.decompress(raw)
        return status, raw.decode("utf-8", errors="replace")

    def _call(self, method, url, payload=None):
        headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip", "Accept-Language": "en-IN,en;q=0.9",
                   "Origin": BASE, "Referer": BASE + "/", "x-build-version": BUILD, "matcher": make_matcher()}
        device = self._device_id()
        if device:
            headers["x-device-id"] = device
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        status, text = self._fetch(urllib.request.Request(url, data=data, method=method, headers=headers))
        if status in (403, 405, 429) or status >= 500:
            raise Blocked(f"HTTP {status}")
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            raise Blocked(f"HTTP {status}, " + ("empty reply" if not text.strip() else f"not JSON: {text[:60]!r}"))
        if isinstance(doc, dict) and doc.get("statusCode") == 429:
            raise Blocked("rate limited (429)")
        return doc

    def setup(self):
        """Do what the website does: open the site, search the place, read its coordinates, select it."""
        status, _ = self._fetch(urllib.request.Request(BASE + "/", headers={
            "User-Agent": UA, "Accept": "text/html,*/*;q=0.8", "Accept-Encoding": "gzip"}))
        if status in (403, 405, 429):
            raise Blocked(f"home page HTTP {status}")
        time.sleep(random.uniform(1.0, 2.0))
        found = self._call("GET", BASE + "/api/instamart/maps/suggestions?input=" + urllib.parse.quote(self.text))
        places = found.get("data") or []
        if not places:
            raise NoPlace(f"no place found for '{self.text}'")
        time.sleep(random.uniform(1.0, 2.0))
        info = self._call("GET", BASE + "/api/instamart/maps/address-widgets/v2?place_id="
                          + urllib.parse.quote(places[0]["place_id"]))
        addr = (info.get("data") or {}).get("address") or {}
        loc = addr.get("location") or {}
        if "latitude" not in loc:
            raise Blocked("place details had no coordinates")
        time.sleep(random.uniform(1.0, 2.0))
        self._call("POST", BASE + "/api/instamart/home/select-location/v2", {"data": {
            "lat": loc["latitude"], "lng": loc["longitude"], "address": addr.get("subtitle", ""), "addressId": "",
            "annotation": addr.get("subtitle", ""), "clientId": "INSTAMART-APP"}})
        self.address = addr.get("subtitle", "")
        self.ready = True
        time.sleep(random.uniform(2.0, 4.0))

    def search(self):
        if not self.ready:
            self.setup()
        return self._call("POST", SEARCH_URL, {
            "facets": [], "sortAttribute": "", "query": QUERY, "search_results_offset": "0",
            "page_type": "INSTAMART_SEARCH_PAGE", "is_pre_search_tag": False})


# ---------- parsing ----------
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


def first_pod(doc):
    def find(o):
        if isinstance(o, dict):
            if o.get("podId"):
                return str(o["podId"])
            for v in o.values():
                r = find(v)
                if r:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = find(v)
                if r:
                    return r
        return ""
    return find(doc)


def parse_items(doc):
    """Return {coin name: {...}} for items whose name+brand contain every TRACK_WORDS entry.

    The key is the coin NAME, not the SKU id: each store has its own SKU ids for the same coin."""
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
            full = var.get("displayName") or name
            if not all(w in (full + " " + (brand or var.get("brandName", ""))).lower() for w in WORDS):
                continue
            stock = bool(it.get("inStock")) and bool((var.get("inventory") or {}).get("inStock", True))
            found.setdefault(full, {"name": full, "sku": sku, "price": offer, "mrp": mrp, "in_stock": stock})
    return found


# ---------- state, alerts ----------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    if DRY:
        return
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log(f"Could not save state ({e}). Alerts still work until restart.")


def inr(n):
    return f"₹{n:,}"


def short(name):
    return name.replace("Kalyan Jewellers ", "").strip()


def compare_city(old, current):
    """Update `old` (this city's saved coins). Return (changed lines, new-coin lines)."""
    had_state = bool(old)  # the first scan of a city only saves a baseline
    changed, new = [], []
    for name, cur in current.items():
        prev = old.get(name)
        if prev is None:
            if had_state:
                new.append(f"🆕 NEW: {short(name)}\n    {inr(cur['price'])} (MRP {inr(cur['mrp'] or 0)})")
        elif cur["price"] != prev["price"]:
            diff = cur["price"] - prev["price"]
            pct = diff / prev["price"] * 100 if prev["price"] else 0
            sign = "+" if diff > 0 else "-"
            changed.append(f"{'🔺' if diff > 0 else '🔻'} {short(name)}\n    {inr(prev['price'])} → {inr(cur['price'])}  "
                           f"({sign}{inr(abs(diff))} | {pct:+.1f}%)")
        old[name] = cur
    return changed, new


# ---------- /status command ----------
STATUS = {}  # label -> {"ok": ts, "pod": str, "error": str}
STATE = {}


def command_allowed(chat, user):
    ids = {str(chat.get("id")), str((user or {}).get("id", ""))}
    if ids & ADMIN_IDS:
        return True
    want = CHAT_ID.lstrip("@").lower()
    return bool(want) and want in (str(chat.get("id")), (chat.get("username") or "").lower())


def ago(ts):
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 120 else (f"{s // 60}m ago" if s < 7200 else f"{s // 3600}h ago")


def status_text(arg=""):
    arg = arg.strip().lower()
    labels = [l for l in STATUS if not arg or arg in l.lower()]
    if arg and not labels:
        return f"No city matches '{arg}'. Cities: " + ", ".join(STATUS)
    lines = ["📊 Kalyan gold coin tracker" + (f" — {labels[0]}" if arg and len(labels) == 1 else "")]
    for label in labels:
        st, items = STATUS[label], STATE.get(label, {})
        when = f" · {ago(st['ok'])}" if st.get("ok") else " · not checked yet"
        err = f" · ⚠️ {st['error'][:60]}" if st.get("error") else ""
        if arg and len(labels) == 1:
            lines.append(f"📍 {label}{when}{err}")
            for name, d in sorted(items.items()):
                lines.append(f"{'✅' if d['in_stock'] else '⛔'} {short(name)} — {inr(d['price'])}")
            if not items:
                lines.append("No Kalyan coins listed here.")
        elif items:
            prices = [d["price"] for d in items.values()]
            stock = sum(1 for d in items.values() if d["in_stock"])
            lines.append(f"📍 {label}: {len(items)} coins, {stock} in stock, from {inr(min(prices))}{when}{err}")
        else:
            lines.append(f"📍 {label}: no coins listed{when}{err}")
    if not arg:
        lines.append("Details: /status <city>")
    return "\n".join(lines)


def command_loop():
    offset, warned = None, False
    while True:
        try:
            payload = {"timeout": 25, "allowed_updates": ["message", "channel_post"]}
            if offset is None:
                payload.update(offset=-1, timeout=0)  # first call: skip old messages
            else:
                payload["offset"] = offset
            updates = tg_api("getUpdates", payload, timeout=40).get("result", [])
            warned = False
            if offset is None:
                offset = (updates[-1]["update_id"] + 1) if updates else 0
                continue
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("channel_post") or {}
                words = (msg.get("text") or "").split()
                cmd = words[0].split("@")[0].lower() if words else ""
                chat = msg.get("chat") or {}
                if cmd in ("/status", "/prices", "/start", "/help") and command_allowed(chat, msg.get("from")):
                    text = status_text(" ".join(words[1:])) if cmd in ("/status", "/prices") \
                        else "Commands: /status (all cities) or /status <city>"
                    tg_api("sendMessage", {"chat_id": chat["id"], "text": text})
        except urllib.error.HTTPError as e:
            if e.code == 409 and not warned:
                log("Commands: another copy of this bot is polling Telegram (PC and Voroa both running?). Retrying.")
                warned = True
            time.sleep(30)
        except Exception as e:  # noqa: BLE001
            if not warned:
                log(f"Commands: Telegram polling problem ({e}). Retrying.")
                warned = True
            time.sleep(15)


# ---------- main loop ----------
def check_city(city, state):
    """One search for one city. Returns that city's alert section, or None."""
    doc = city.search()
    current = parse_items(doc)
    pod = first_pod(doc)
    STATUS[city.label] = {"ok": time.time(), "pod": pod, "error": ""}
    old = state.setdefault(city.label, {})
    if DRY:
        coins = ", ".join(f"{short(n)} {inr(d['price'])}{'' if d['in_stock'] else ' (out)'}" for n, d in sorted(current.items()))
        log(f"{city.label}: store {pod or '?'} · {city.address} · {len(current)} coins" + (f": {coins}" if coins else ""))
        return None
    if not current:
        log(f"{city.label}: store {pod or '?'} · no Kalyan coins listed")
        return None
    changed, new = compare_city(old, current)
    log(f"{city.label}: store {pod or '?'} · {len(current)} coins" + (f" · {len(changed)} price change(s), {len(new)} new" if changed or new else ""))
    return f"📍 {city.label}\n" + "\n".join(changed + new) if (changed or new) else None


def main():
    if "--get-chat-id" in sys.argv:
        return get_chat_id()
    locations = parse_locations()
    cities = [City(label, text) for label, text in locations]
    state = {} if DRY else load_state()
    STATE.update(state)
    STATE.clear()
    STATE.update(state)
    for c in cities:
        STATUS[c.label] = {"ok": None, "pod": "", "error": ""}
    log(f"{'TEST (nothing sent or saved)' if DRY else 'Tracking'}: '{QUERY}' in {len(cities)} cities, one full round about every "
        f"{INTERVAL}s. Plain HTTP, no browser. State: {STATE_FILE}")
    if TOKEN and not DRY and os.environ.get("ENABLE_COMMANDS", "1") != "0":
        threading.Thread(target=command_loop, daemon=True).start()
    gap = max(20, INTERVAL // max(1, len(cities)))
    streak = 0
    while True:
        sections = []
        for city in cities:
            try:
                section = check_city(city, state)
                if section:
                    sections.append(section)
                streak = 0
            except (Blocked, NoPlace) as e:
                city.reset()  # start this city from scratch next round
                streak += 1
                STATUS[city.label]["error"] = str(e)
                log(f"{city.label}: failed ({e})")
            except Exception as e:  # noqa: BLE001
                city.reset()
                streak += 1
                STATUS[city.label]["error"] = f"{type(e).__name__}: {e}"
                log(f"{city.label}: failed ({type(e).__name__}: {e})")
            save_state(state)
            STATE.clear()
            STATE.update(state)
            extra = min(30 * 2 ** (streak - 1), 600) if streak >= 3 else 0  # several failures in a row: slow down
            if streak >= 3 and streak % 6 == 3 and NOTIFY_ERRORS:
                send(f"⚠️ Tracker: {streak} failed checks in a row. Last: {STATUS[city.label]['error']}")
            time.sleep((gap + extra) * random.uniform(0.9, 1.1))
        if sections:
            send_long("💰 Kalyan gold coin update\n" + "\n".join(sections) + "\n" + BASE + "/search?query="
                      + urllib.parse.quote(QUERY))
        if DRY:
            log("Test round finished.")
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped.")
