#!/usr/bin/env python3
"""Kalyan gold coin price tracker for Swiggy Instamart, many cities -> Telegram alerts.

Pure Python standard library: no Node.js, no browser, no pip packages. Low RAM.
For every city it picks the delivery spot the way the Instamart website does (place search ->
coordinates -> select location), then reads that city's store search. Prices and stock differ
per store, so each city is tracked on its own.

Telegram gets one initial in-stock inventory snapshot, then only price changes (with the amount,
up/down), plus the latest Goodreturns Mumbai 24K/999 reference rate. Send /status for a health summary.

Test first, nothing is sent:   python swiggy_kalyan_tracker.py --once
Find your chat id:             python swiggy_kalyan_tracker.py --get-chat-id

Env vars (or a .env file next to this script):
  TELEGRAM_BOT_TOKEN   token from @BotFather
  TELEGRAM_CHAT_ID     chat id, or @channelusername (the bot must be channel admin)
  TELEGRAM_ADMIN_ID    optional: your numeric Telegram user id(s), comma separated, allowed to use /status
  LOCATIONS            optional: "Label=search text;Label2=search text2". Search text can be an area,
                       "pincode city" or an address. Default list: see DEFAULT_LOCATIONS below.
  CHECK_INTERVAL_SEC   seconds between full rounds, default 900, minimum 600
  SEARCH_QUERY         default "kalyan gold coin"
  TRACK_WORDS          comma list, ALL must appear in item name+brand. default "kalyan,gold coin"
  STATE_FILE           default kalyan_state_cities.json (lost on restart on hosts without a disk)
  ENABLE_COMMANDS      default 1 = answer /status. Use 0 on a second copy (PC + Voroa) to avoid clashes.
  NOTIFY_ERRORS        retained for compatibility; alerts stay limited to inventory and price changes.
"""
import gzip
import html
import http.cookiejar
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://instamart.in"
SWIGGY_WEB = "https://www.swiggy.com/instamart/search"
GOODRETURNS_URL = "https://www.goodreturns.in/gold-rates/mumbai.html"
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
    INTERVAL = max(600, int(os.environ.get("CHECK_INTERVAL_SEC", "900")))
except ValueError:
    INTERVAL = 900
DRY = "--once" in sys.argv  # dry run: print only, send nothing, save nothing
RATE_LIMIT_UNTIL = 0.0
RATE_LIMIT_STREAK = 0


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def note_rate_limit(headers=None):
    """Set a shared cooldown, using Retry-After when the service sends it."""
    global RATE_LIMIT_UNTIL, RATE_LIMIT_STREAK
    RATE_LIMIT_STREAK += 1
    delay = min(60 * (2 ** min(RATE_LIMIT_STREAK - 1, 4)), 900)
    retry_after = (headers or {}).get("Retry-After", "").strip()
    if retry_after:
        try:
            delay = max(delay, float(retry_after))
        except ValueError:
            try:
                delay = max(delay, parsedate_to_datetime(retry_after).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    delay = max(1, min(delay, 3600))
    RATE_LIMIT_UNTIL = max(RATE_LIMIT_UNTIL, time.time() + delay)
    return delay


def wait_out_rate_limit():
    remaining = RATE_LIMIT_UNTIL - time.time()
    if remaining > 0:
        log(f"Pausing city checks for about {int(remaining)}s after 429")
        time.sleep(remaining)


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
        return False
    log("ALERT sent: " + text.split("\n")[0])
    for _ in range(2):
        try:
            tg_api("sendMessage", {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True})
            return True
        except Exception as e:  # noqa: BLE001
            log(f"Telegram send failed ({e})")
            time.sleep(3)
    return False


def send_long(text):
    """Telegram allows 4096 characters per message: split on line breaks if needed."""
    chunk = ""
    all_sent = True
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800:
            all_sent = send(chunk) and all_sent
            chunk = ""
        chunk += ("\n" if chunk else "") + line
    if chunk:
        all_sent = send(chunk) and all_sent
    return all_sent


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


class RateLimited(Blocked):
    """HTTP or JSON rate-limit response; pause the whole city round."""


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
                status, raw, headers = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            status, raw, headers = e.code, e.read(), e.headers
        enc = headers.get("Content-Encoding", "")
        if enc.lower() == "gzip":
            raw = gzip.decompress(raw)
        if status == 429:
            note_rate_limit(headers)
        return status, raw.decode("utf-8", errors="replace"), headers

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
        status, text, _ = self._fetch(urllib.request.Request(url, data=data, method=method, headers=headers))
        if status == 429:
            raise RateLimited("rate limited (429)")
        if status in (403, 405) or status >= 500:
            raise Blocked(f"HTTP {status}")
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            raise Blocked(f"HTTP {status}, " + ("empty reply" if not text.strip() else f"not JSON: {text[:60]!r}"))
        if isinstance(doc, dict) and doc.get("statusCode") == 429:
            note_rate_limit()
            raise RateLimited("rate limited (429 inside JSON)")
        global RATE_LIMIT_STREAK
        RATE_LIMIT_STREAK = 0
        return doc

    def setup(self):
        """Do what the website does: open the site, search the place, read its coordinates, select it."""
        status, _, _ = self._fetch(urllib.request.Request(BASE + "/", headers={
            "User-Agent": UA, "Accept": "text/html,*/*;q=0.8", "Accept-Encoding": "gzip"}))
        if status == 429:
            raise RateLimited("home page HTTP 429")
        if status in (403, 405):
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


def goodreturns_24k_rate():
    """Fetch the current Mumbai 24K (99.9%) rate from Goodreturns, in rupees per gram."""
    req = urllib.request.Request(GOODRETURNS_URL, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-IN,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            page = response.read().decode("utf-8", errors="replace")
        plain = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())
        patterns = (
            r"₹\s*([\d,]+)\s*per gram for 24 karat gold\s*\(99\.9% purity\)",
            r"24K\s*Gold\s*/g\s*₹?\s*([\d,]+)",
        )
        for pattern in patterns:
            match = re.search(pattern, plain, re.IGNORECASE)
            if match:
                return int(match.group(1).replace(",", ""))
    except (OSError, TimeoutError, urllib.error.URLError) as e:
        log(f"Goodreturns rate fetch failed ({e})")
    except Exception as e:  # noqa: BLE001
        log(f"Goodreturns rate parse failed ({e})")
    return None


def instamart_links(pod):
    fields = {"query": QUERY}
    if pod:
        fields.update({"storeId": pod, "primaryStoreId": pod})
    params = urllib.parse.urlencode(fields)
    return (f"App: swiggy://instamart/search?{params}\n"
            f"Web: {SWIGGY_WEB}?{params}")


def benchmark_comparison(item, gold_rate):
    name = item["name"].lower()
    if not gold_rate or "24 karat" not in name:
        return ""
    match = re.search(r"(\d+(?:\.\d+)?)\s*g\b", name)
    if not match:
        return ""
    grams = float(match.group(1))
    benchmark = round(gold_rate * grams)
    difference = item["price"] - benchmark
    sign = "+" if difference >= 0 else "−"
    percent = (difference / benchmark * 100) if benchmark else 0
    return f" (vs 24K/999 gold {inr(benchmark)}: {sign}{inr(abs(difference))}, {percent:+.1f}%)"


def compare_city(old, current):
    """Update this city's saved prices. Return price-change lines and new listing names."""
    had_state = bool(old)  # the first scan of a city only saves a baseline
    changed, new = [], []
    for name, cur in current.items():
        prev = old.get(name)
        if prev is None:
            if had_state:
                new.append(name)
        elif cur["price"] != prev["price"]:
            changed.append((name, prev["price"], cur["price"]))
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
def check_city(city, state, inventory_report=None):
    """One search for one city. Returns that city's alert section, or None."""
    doc = city.search()
    current = parse_items(doc)
    pod = first_pod(doc)
    STATUS[city.label] = {"ok": time.time(), "pod": pod, "error": ""}
    if inventory_report is not None:
        inventory_report.append({"label": city.label, "pod": pod,
                                 "items": {n: d for n, d in current.items() if d["in_stock"]}})
    old = state.setdefault(city.label, {})
    if DRY:
        coins = ", ".join(f"{short(n)} {inr(d['price'])}{'' if d['in_stock'] else ' (out)'}" for n, d in sorted(current.items()))
        log(f"{city.label}: store {pod or '?'} · {city.address} · {len(current)} coins" + (f": {coins}" if coins else ""))
        return None
    if not current:
        log(f"{city.label}: store {pod or '?'} · no Kalyan coins listed")
        return None
    changed, new = compare_city(old, current)
    log(f"{city.label}: store {pod or '?'} · {len(current)} coins" +
        (f" · {len(changed)} price change(s)" if changed else ""))
    return {"label": city.label, "pod": pod, "current": current, "changed": changed} if changed else None


def gold_reference_line(rate):
    if rate is None:
        return f"Goodreturns Mumbai 24K/999 rate unavailable · {GOODRETURNS_URL}"
    return (f"Goodreturns Mumbai 24K/999: {inr(rate)}/g (indicative; excludes taxes)\n"
            f"{GOODRETURNS_URL}")


def format_inventory(report, gold_rate):
    blocks = []
    for city in report:
        label, pod, items = city["label"], city.get("pod", ""), city.get("items", {})
        if city.get("error"):
            lines = [f"📍 {label} · scan failed: {city['error']}"]
        else:
            lines = [f"📍 {label} · {len(items)} available coin(s)"]
        if items and not city.get("error"):
            for name, item in sorted(items.items()):
                lines.append(f"• {short(name)} — {inr(item['price'])}{benchmark_comparison(item, gold_rate)}")
        elif not city.get("error"):
            lines.append("No in-stock Kalyan coins")
        lines.append(instamart_links(pod))
        blocks.append("\n".join(lines))
    return ("📋 Kalyan gold coin inventory · initial check\n" + gold_reference_line(gold_rate)
            + "\n\n" + "\n\n".join(blocks))


def format_price_updates(changes, gold_rate):
    blocks = []
    for city in changes:
        lines = [f"📍 {city['label']}"]
        for name, previous, current_price in city["changed"]:
            diff = current_price - previous
            pct = (diff / previous * 100) if previous else 0
            icon = "🔺" if diff > 0 else "🔻"
            sign = "+" if diff > 0 else "−"
            item = city["current"].get(name, {"name": name, "price": current_price})
            lines.append(f"{icon} {short(name)}\n   {inr(previous)} → {inr(current_price)} "
                         f"({sign}{inr(abs(diff))} | {pct:+.1f}%){benchmark_comparison(item, gold_rate)}")
        lines.append(instamart_links(city.get("pod", "")))
        blocks.append("\n".join(lines))
    return ("💰 Kalyan coin price update\n" + gold_reference_line(gold_rate)
            + "\n\n" + "\n\n".join(blocks))


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
    # Versioned key lets the new clean snapshot send once even if the previous
    # buggy version already wrote its older marker into the shared state file.
    inventory_pending = not (state.get("_tracker_meta") or {}).get("initial_inventory_v2_sent", False)
    log(f"{'TEST (nothing sent or saved)' if DRY else 'Tracking'}: '{QUERY}' in {len(cities)} cities, one full round about every "
        f"{INTERVAL}s. Plain HTTP, no browser. State: {STATE_FILE}")
    if TOKEN and not DRY and os.environ.get("ENABLE_COMMANDS", "1") != "0":
        threading.Thread(target=command_loop, daemon=True).start()
    gap = max(60, INTERVAL // max(1, len(cities)))
    streak = 0
    while True:
        sections = []
        inventory_report = [] if inventory_pending else None
        rate_limited = False
        for city_index, city in enumerate(cities):
            try:
                section = check_city(city, state, inventory_report)
                if section:
                    sections.append(section)
                streak = 0
            except RateLimited as e:
                # Do not rebuild the session or continue querying other cities after a 429.
                streak += 1
                STATUS[city.label]["error"] = str(e)
                log(f"{city.label}: failed ({e})")
                if inventory_report is not None:
                    inventory_report.append({"label": city.label, "pod": STATUS[city.label].get("pod", ""),
                                             "error": str(e)})
                rate_limited = True
            except (Blocked, NoPlace) as e:
                city.reset()  # start this city from scratch next round
                streak += 1
                STATUS[city.label]["error"] = str(e)
                log(f"{city.label}: failed ({e})")
                if inventory_report is not None:
                    inventory_report.append({"label": city.label, "error": str(e)})
            except Exception as e:  # noqa: BLE001
                city.reset()
                streak += 1
                STATUS[city.label]["error"] = f"{type(e).__name__}: {e}"
                log(f"{city.label}: failed ({type(e).__name__}: {e})")
                if inventory_report is not None:
                    inventory_report.append({"label": city.label, "error": f"{type(e).__name__}: {e}"})
            save_state(state)
            STATE.clear()
            STATE.update(state)
            if rate_limited:
                break
            extra = min(30 * 2 ** (streak - 1), 600) if streak >= 3 else 0  # several failures in a row: slow down
            if city_index < len(cities) - 1 or not DRY:
                time.sleep((gap + extra) * random.uniform(0.9, 1.1))
        if inventory_report is not None and not rate_limited:
            rate = goodreturns_24k_rate()
            sent = send_long(format_inventory(inventory_report, rate))
            # Disarm this process after one attempt so a failed Telegram send cannot spam each round.
            inventory_report = None
            inventory_pending = False
            if sent and not DRY:
                state.setdefault("_tracker_meta", {})["initial_inventory_v2_sent"] = True
                save_state(state)
        if sections:
            rate = goodreturns_24k_rate()
            send_long(format_price_updates(sections, rate))
        if DRY:
            log("Test round finished.")
            return
        if rate_limited:
            wait_out_rate_limit()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped.")
