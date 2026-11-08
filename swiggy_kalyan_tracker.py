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
  CHECK_INTERVAL_SEC   seconds between rounds over all cities, default 300, minimum 30
  REQUEST_GAP_SEC      pause between single Instamart calls, default 3, minimum 1
  INITIAL_REPORT       default 1 = one stock-check message on a start with no saved state. Set 0 on Voroa after the first deploy (no disk, state resets)
  SEARCH_QUERY         default "kalyan gold coin"
  TRACK_WORDS          comma list, ALL must appear in item name+brand. default "kalyan,gold coin"
  STATE_FILE           default kalyan_state_pins.json (lost on restart on hosts without a disk)
  ENABLE_COMMANDS      default 1 = answer /status. Use 0 on a second copy (PC + Voroa) to avoid clashes.
  NOTIFY_ERRORS        default 0 = Telegram gets ONLY price changes and new SKUs. 1 = also failure warnings.
"""
import gzip
import html
import http.cookiejar
import json
import os
import re
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

# Tier 1 and tier 2 cities, a few pincodes each. A pincode can map to a store that has nothing in stock while the
# next one has plenty, so each city gets several. Pincodes that map to the SAME store are detected on the first round
# and dropped automatically. Instamart does not serve every city; that city just shows "no coins listed".
# Override with LOCATIONS="City=pin1,pin2;City2=pin"  (a pincode, area or address, anything the site search accepts).
DEFAULT_LOCATIONS = (
    "Mumbai=400053,400050,400076,400601;Pune=411045,411001,411014,411057;Nashik=422005,422101,422002,422009;"
    "Bengaluru=560034,560066,560102,560038;Chennai=600017,600042,600040,600020;"
    "Hyderabad=500034,500081,500032,500016;Delhi=110017,110001,110075,110092;"
    "Kolkata=700091,700019,700156;Ahmedabad=380015,380054,380009;Surat=395009,395007;"
    "Vadodara=390007;Rajkot=360005;Jaipur=302017;Kochi=682024"
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
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(HERE, "kalyan_state_pins.json"))
BUILD = os.environ.get("X_BUILD_VERSION", "2.392.0").strip()
NOTIFY_ERRORS = os.environ.get("NOTIFY_ERRORS", "0").strip() == "1"
try:
    INTERVAL = max(30, int(os.environ.get("CHECK_INTERVAL_SEC", "300")))
except ValueError:
    INTERVAL = 300
try:
    BASE_GAP = max(1.0, float(os.environ.get("REQUEST_GAP_SEC", "3")))
except ValueError:
    BASE_GAP = 3.0
MAX_SCALE = 6.0  # slowest the tracker will ever go: 6x its normal pace
INITIAL_REPORT = os.environ.get("INITIAL_REPORT", "1").strip() != "0"
LINK = BASE + "/search?query=" + urllib.parse.quote(QUERY)  # opens the Swiggy app on a phone
DRY = "--once" in sys.argv  # dry run: print only, send nothing, save nothing


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def parse_locations():
    """Return [(city, search text)], one entry per pincode."""
    raw = os.environ.get("LOCATIONS", "").strip() or DEFAULT_LOCATIONS
    out = []
    for part in raw.split(";"):
        city, _, texts = part.strip().partition("=")
        for t in (texts or city).split(","):
            if t.strip():
                out.append((city.strip(), t.strip()))
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
        log("MESSAGE (not sent):\n" + text)
        return
    log("MESSAGE sent: " + text.split("\n")[0])
    for _ in range(2):
        try:
            tg_api("sendMessage", {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": True})
            return
        except Exception as e:  # noqa: BLE001
            log(f"Telegram send failed ({e})")
            time.sleep(3)


def send_blocks(header, blocks, footer=""):
    """Send header + city blocks, splitting between blocks so no message passes Telegram's 4096 limit."""
    msg = header
    for blk in blocks + ([footer] if footer else []):
        if len(msg) + len(blk) + 2 > 3800:
            send(msg)
            msg = ""
        msg += ("\n\n" if msg else "") + blk
    if msg:
        send(msg)


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
PLACES = {}
PLACES_FILE = os.path.join(HERE, "kalyan_places.json")


def save_places():
    if DRY:
        return
    try:
        with open(PLACES_FILE, "w", encoding="utf-8") as f:
            json.dump(PLACES, f)
    except OSError:
        pass


try:
    with open(PLACES_FILE, encoding="utf-8") as _f:
        PLACES.update(json.load(_f))
except (OSError, json.JSONDecodeError):
    pass


class Blocked(Exception):
    """Instamart refused: 429 rate limit, 403/405 bot check, empty or non-JSON reply."""
    limited = False


class Pacer:
    """Spaces every Instamart call. A 429 raises the pause 1.5x (up to 6x); every good call eases it back."""

    def __init__(self):
        self.scale, self.last = 1.0, 0.0

    def wait(self):
        gap = BASE_GAP * self.scale * random.uniform(0.9, 1.2)
        time.sleep(max(0.0, self.last + gap - time.time()))
        self.last = time.time()

    def hit(self):
        old, self.scale = self.scale, min(self.scale * 1.5, MAX_SCALE)
        if self.scale != old:
            log(f"429 too many requests -> slowing down: {BASE_GAP * self.scale:.1f}s between calls, "
                f"{INTERVAL * self.scale:.0f}s between rounds")

    def ok(self):
        old, self.scale = self.scale, max(1.0, self.scale * 0.97)
        if old > 1.0 and self.scale == 1.0:
            log("Instamart is happy again -> back to normal speed")


PACER = Pacer()


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

    def __init__(self, city, text):
        self.city, self.text, self.label = city, text, f"{city} {text}"
        self.pod, self.dup = "", False
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
        PACER.wait()
        status, text = self._fetch(urllib.request.Request(url, data=data, method=method, headers=headers))
        if status in (403, 405, 429) or status >= 500:
            e = Blocked(f"HTTP {status}")
            e.limited = status == 429
            if e.limited:
                PACER.hit()
            raise e
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            raise Blocked(f"HTTP {status}, " + ("empty reply" if not text.strip() else f"not JSON: {text[:60]!r}"))
        if isinstance(doc, dict) and doc.get("statusCode") == 429:
            e = Blocked("rate limited (429)")
            e.limited = True
            PACER.hit()
            raise e
        PACER.ok()
        return doc

    def setup(self):
        """Do what the website does: open the site, search the place, read its coordinates, select it."""
        PACER.wait()
        status, _ = self._fetch(urllib.request.Request(BASE + "/", headers={
            "User-Agent": UA, "Accept": "text/html,*/*;q=0.8", "Accept-Encoding": "gzip"}))
        if status in (403, 405, 429):
            e = Blocked(f"home page HTTP {status}")
            e.limited = status == 429
            if e.limited:
                PACER.hit()
            raise e
        hit = PLACES.get(self.label)  # coordinates found on an earlier run: skips two calls
        if hit:
            addr = hit
            loc = hit["location"]
        else:
            found = self._call("GET", BASE + "/api/instamart/maps/suggestions?input=" + urllib.parse.quote(f"{self.text} {self.city}" if self.text.isdigit() else self.text))
            places = found.get("data") or []
            if not places:
                raise NoPlace(f"no place found for '{self.text}'")
            info = self._call("GET", BASE + "/api/instamart/maps/address-widgets/v2?place_id="
                              + urllib.parse.quote(places[0]["place_id"]))
            addr = (info.get("data") or {}).get("address") or {}
            loc = addr.get("location") or {}
            if "latitude" not in loc:
                raise Blocked("place details had no coordinates")
            PLACES[self.label] = {"subtitle": addr.get("subtitle", ""), "location": loc}
            save_places()
        self._call("POST", BASE + "/api/instamart/home/select-location/v2", {"data": {
            "lat": loc["latitude"], "lng": loc["longitude"], "address": addr.get("subtitle", ""), "addressId": "",
            "annotation": addr.get("subtitle", ""), "clientId": "INSTAMART-APP"}})
        self.address = addr.get("subtitle", "")
        self.ready = True

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
    """'Kalyan Jewellers Flower 24 Karat Gold Coin (1 g)' -> 'Flower 24K 1 g'"""
    n = name.replace("Kalyan Jewellers ", "")
    n = re.sub(r"(\d+) Karat Gold Coin \(([^)]*)\)", r"\1K \2", n)
    return n.strip()


def esc(t):
    return html.escape(str(t), quote=False)


def link_line():
    return f'🔗 <a href="{LINK}">Open in Swiggy</a>'


def compare_city(old, current, tag=""):
    """Update `old` (one pincode's saved coins). Return alert lines for changed prices and new coins."""
    had_state = bool(old)  # the first scan of a pincode only saves a baseline
    where = f"  · {tag}" if tag else ""
    lines = []
    for name, cur in current.items():
        prev = old.get(name)
        if prev is None:
            if had_state:
                lines.append(f"🆕 <b>{esc(short(name))}</b>  {inr(cur['price'])}  new listing{where}")
        elif cur["price"] != prev["price"]:
            diff = cur["price"] - prev["price"]
            pct = diff / prev["price"] * 100 if prev["price"] else 0
            lines.append(f"{'🔺' if diff > 0 else '🔻'} <b>{esc(short(name))}</b>{where}\n"
                         f"      {inr(prev['price'])} → {inr(cur['price'])}   "
                         f"<b>{'+' if diff > 0 else '-'}{inr(abs(diff))}</b> ({pct:+.1f}%)")
        old[name] = cur
    return lines


def stamp():
    return datetime.now(IST).strftime("%d %b, %I:%M %p IST")


def merged(city):
    """One city = all its pincodes together. A coin counts as in stock if ANY pincode has it."""
    out = {}
    for label, items in STATE.items():
        if not label.startswith(city + " "):
            continue
        pin = label[len(city) + 1:]
        for name, d in items.items():
            m = out.setdefault(name, {"price": d["price"], "in_stock": False, "pins": []})
            if d["in_stock"]:
                if not m["in_stock"] or d["price"] < m["price"]:
                    m["price"] = d["price"]
                m["in_stock"] = True
                m["pins"].append(pin)
            elif not m["in_stock"]:
                m["price"] = min(m["price"], d["price"])
    return out


def city_names(spots):
    seen = []
    for sp in spots:
        if sp.city not in seen:
            seen.append(sp.city)
    return seen


def inventory_report(spots):
    """First message: what is in stock in every city, and at which pincodes."""
    blocks = []
    for city in city_names(spots):
        items = merged(city)
        mine = [sp for sp in spots if sp.city == city]
        if items:
            have = sorted(((n, d) for n, d in items.items() if d["in_stock"]), key=lambda kv: (kv[1]["price"], kv[0]))
            out = sorted(short(n) for n, d in items.items() if not d["in_stock"])
            rows = [f"✅ {esc(short(n))} — {inr(d['price'])}  <i>({', '.join(d['pins'][:3])})</i>" for n, d in have]
            if out:
                rows.append("⛔ Out: " + esc(", ".join(out)))
            blocks.append(f"📍 <b>{esc(city)}</b> · {len(have)}/{len(items)} in stock\n" + "\n".join(rows) + "\n" + link_line())
        elif all(STATUS[sp.label].get("error") and not STATUS[sp.label].get("ok") for sp in mine):
            blocks.append(f"📍 <b>{esc(city)}</b>\n⚠️ could not check ({esc(STATUS[mine[0].label]['error'][:50])})")
        else:
            blocks.append(f"📍 <b>{esc(city)}</b>\nNo Kalyan coins listed here")
    return blocks


# ---------- /status command ----------
STATUS = {}  # label -> {"ok": ts, "pod": str, "error": str}
STATE = {}
CITIES = []


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
    cities = [c for c in CITIES if not arg or arg in c.lower()]
    if not cities:
        return f"No city matches '{arg}'. Cities: " + ", ".join(CITIES)
    one = bool(arg) and len(cities) == 1
    lines = ["📊 Kalyan gold coin tracker" + (f" — {cities[0]}" if one else "")]
    for city in cities:
        items = merged(city)
        mine = [l for l in STATUS if l.startswith(city + " ")]
        oks = [STATUS[l]["ok"] for l in mine if STATUS[l].get("ok")]
        errs = [STATUS[l]["error"] for l in mine if STATUS[l].get("error")]
        when = f" · {ago(max(oks))}" if oks else " · not checked yet"
        err = f" · ⚠️ {errs[0][:50]}" if errs and not oks else ""
        if one:
            lines.append(f"📍 {city}{when}{err} · pincodes: {', '.join(l[len(city) + 1:] for l in mine)}")
            for name, d in sorted(items.items(), key=lambda kv: (not kv[1]["in_stock"], kv[1]["price"])):
                pins = f"  ({', '.join(d['pins'][:3])})" if d["in_stock"] else ""
                lines.append(f"{'✅' if d['in_stock'] else '⛔'} {short(name)} — {inr(d['price'])}{pins}")
            if not items:
                lines.append("No Kalyan coins listed here.")
        elif items:
            n_in = sum(1 for d in items.values() if d["in_stock"])
            lines.append(f"📍 {city}: {n_in}/{len(items)} in stock{when}{err}")
        else:
            lines.append(f"📍 {city}: no coins listed{when}{err}")
    if not arg:
        lines.append(f"⏱ every {INTERVAL * PACER.scale:.0f}s" + (" (slowed: Instamart rate limit)" if PACER.scale > 1.05 else ""))
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
def check_spot(sp, state, pods):
    """One search for one pincode. Returns alert lines (possibly empty)."""
    for attempt in (1, 2):
        try:
            doc = sp.search()
            break
        except Blocked as e:
            if not (e.limited and attempt == 1):
                raise
            time.sleep(5 * PACER.scale)  # 429: cool off once, then try the same pincode again
    current = parse_items(doc)
    sp.pod = first_pod(doc)
    STATUS[sp.label] = {"ok": time.time(), "pod": sp.pod, "error": ""}
    # two pincodes of one city served by the same store show the same data: keep only the first
    if sp.pod and sp.pod in pods.setdefault(sp.city, {}) and pods[sp.city][sp.pod] != sp.label:
        sp.dup = True
        state.pop(sp.label, None)
        STATE.pop(sp.label, None)
        log(f"{sp.label}: same store {sp.pod} as {pods[sp.city][sp.pod]} -> skipped from now on")
        return []
    if sp.pod:
        pods[sp.city][sp.pod] = sp.label
    lines = compare_city(state.setdefault(sp.label, {}), current, sp.text)
    n_in = sum(1 for d in current.values() if d["in_stock"])
    log(f"{sp.label}: store {sp.pod or '?'} · {n_in}/{len(current)} in stock" + (f" · {len(lines)} change(s)" if lines else "")
        + (f" · {sp.address}" if DRY else ""))
    return lines


def main():
    if "--get-chat-id" in sys.argv:
        return get_chat_id()
    spots = [City(c, t) for c, t in parse_locations()]
    CITIES[:] = city_names(spots)
    state = {} if DRY else load_state()
    STATE.update(state)
    for sp in spots:
        STATUS[sp.label] = {"ok": None, "pod": "", "error": ""}
    log(f"{'TEST (nothing sent or saved)' if DRY else 'Tracking'}: '{QUERY}' in {len(CITIES)} cities / {len(spots)} pincodes. "
        f"Plain HTTP, no browser. Round every {INTERVAL}s (auto-slows on 429). State: {STATE_FILE}")
    if TOKEN and not DRY and os.environ.get("ENABLE_COMMANDS", "1") != "0":
        threading.Thread(target=command_loop, daemon=True).start()
    send_report = INITIAL_REPORT and not state  # a restart WITH saved state stays quiet
    pods, streak = {}, 0
    while True:
        started, blocks = time.time(), {}
        for sp in [x for x in spots if not x.dup]:
            try:
                lines = check_spot(sp, state, pods)
                if lines:
                    blocks.setdefault(sp.city, []).extend(lines)
                streak = 0
            except Exception as e:  # noqa: BLE001
                sp.reset()  # start this pincode from scratch next round
                streak += 1
                msg = str(e) if isinstance(e, (Blocked, NoPlace)) else f"{type(e).__name__}: {e}"
                STATUS[sp.label]["error"] = msg
                log(f"{sp.label}: failed ({msg})")
                if streak >= 3 and streak % 6 == 3 and NOTIFY_ERRORS:
                    send(f"⚠️ Tracker: {streak} failed checks in a row. Last: {esc(msg)}")
            STATE.clear()
            STATE.update(state)
        save_state(state)
        if send_report:
            send_blocks(f"📦 <b>Kalyan gold coins · stock check</b>\n🕒 {stamp()} · {len(CITIES)} cities",
                        inventory_report(spots),
                        "That was the one-time overview. From now on: quiet checks, a message only for a price change 🔺🔻 or a new listing 🆕.")
            send_report = False
        elif blocks:
            send_blocks(f"💰 <b>Kalyan gold coin update</b> · {stamp()}",
                        [f"📍 <b>{esc(c)}</b>\n" + "\n".join(ls) + "\n" + link_line() for c, ls in blocks.items()])
        if DRY:
            log("Test round finished.")
            return
        time.sleep(max(5.0, INTERVAL * PACER.scale - (time.time() - started)) * random.uniform(0.95, 1.05))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped.")
