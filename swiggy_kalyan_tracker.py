#!/usr/bin/env python3
"""Kalyan gold coin tracker for Swiggy Instamart: users pick their own pincode in Telegram.

Pure Python standard library: no Node.js, no browser, no pip packages. Low RAM.

How people use the bot (private chat, group, or channel the bot is in):
  422005            (or /pin 422005)   -> bot looks the pincode up on Instamart, replies with every Kalyan
                                         gold coin: in stock or out, price, and starts tracking that pincode
  /list                                -> pincodes this chat tracks
  /remove 422005                       -> stop tracking a pincode (/remove all)
  /help
After the first reply the bot stays quiet and only writes when a tracked pincode has a price change
(🔺 up / 🔻 down, with the amount) or a new coin listing (🆕).

Admin only: /status (health, pincodes watched, request speed).

Test without Telegram, prints what a user would get:   python swiggy_kalyan_tracker.py --pin 422005 411045
Find a chat id:                                        python swiggy_kalyan_tracker.py --get-chat-id

Env vars (or a .env file next to this script):
  TELEGRAM_BOT_TOKEN   token from @BotFather
  TELEGRAM_ADMIN_ID    your numeric Telegram user id(s), comma separated. Admins can use /status and skip the limits
  TELEGRAM_CHAT_ID     optional: a chat or @channel that is subscribed to DEFAULT_PINCODES at every start
  DEFAULT_PINCODES     optional: comma list, tracked for TELEGRAM_CHAT_ID (keeps working after a Voroa restart)
  ALLOWED_CHAT_IDS     optional: comma list of chat ids allowed to use the bot. Empty = everybody
  MAX_PINS_PER_CHAT    default 5 (admins unlimited)
  MAX_TOTAL_PINS       default 40: all pincodes together, protects against Instamart rate limits
  CHECK_INTERVAL_SEC   seconds between rounds over all tracked pincodes, default 120, minimum 30
  REQUEST_GAP_SEC      pause between single Instamart calls, default 1.5, minimum 1 (auto-slows on 429)
  SEARCH_QUERY         default "kalyan gold coin"
  TRACK_WORDS          comma list, ALL must appear in item name+brand. default "kalyan,gold coin"
  STATE_FILE           default kalyan_tracker_data.json (pincodes + prices. Lost on restart on hosts without a disk)
  ENABLE_COMMANDS      default 1. Use 0 on a second copy (PC + Voroa) to avoid Telegram 409 clashes.
"""
import gzip
import html
import http.cookiejar
import json
import os
import queue
import random
import re
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


def env_list(name):
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


def env_num(name, default, low, cast=int):
    try:
        return max(low, cast(os.environ.get(name, str(default))))
    except ValueError:
        return default


TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
ADMIN_IDS = set(env_list("TELEGRAM_ADMIN_ID"))
ALLOWED = set(env_list("ALLOWED_CHAT_IDS"))
DEFAULT_PINS = [p for p in env_list("DEFAULT_PINCODES") if re.fullmatch(r"\d{6}", p)]
QUERY = os.environ.get("SEARCH_QUERY", "kalyan gold coin")
WORDS = [w.strip().lower() for w in os.environ.get("TRACK_WORDS", "kalyan,gold coin").split(",") if w.strip()]
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(HERE, "kalyan_tracker_data.json"))
BUILD = os.environ.get("X_BUILD_VERSION", "2.392.0").strip()
INTERVAL = env_num("CHECK_INTERVAL_SEC", 120, 30)
BASE_GAP = env_num("REQUEST_GAP_SEC", 1.5, 1.0, float)
MAX_PER_CHAT = env_num("MAX_PINS_PER_CHAT", 5, 1)
MAX_TOTAL = env_num("MAX_TOTAL_PINS", 40, 1)
MAX_SCALE = 16.0  # slowest the tracker will ever go: 16x its normal pace
LINK = BASE + "/search?query=" + urllib.parse.quote(QUERY)  # opens the Swiggy app on a phone
DRY = "--pin" in sys.argv  # test mode: print only, send nothing, save nothing


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------- Telegram ----------
def tg_api(method, payload=None, timeout=20):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def send(chat, text):
    if DRY or not TOKEN:
        log(f"MESSAGE to {chat} (not sent):\n{text}")
        return
    for _ in range(2):
        try:
            tg_api("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": True})
            log(f"sent to {chat}: " + text.split("\n")[0][:70])
            return
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:150]
            log(f"Telegram send to {chat} failed: HTTP {e.code} {body}")
            if e.code == 403:  # bot blocked, kicked or chat gone: stop tracking for it
                drop_chat(str(chat))
            if e.code in (400, 403):
                return
            time.sleep(3)
        except Exception as e:  # noqa: BLE001
            log(f"Telegram send to {chat} failed ({e})")
            time.sleep(3)


def send_blocks(chat, header, blocks, footer=""):
    """Header + blocks, split between blocks so no message passes Telegram's 4096 limit."""
    msg = header
    for blk in blocks + ([footer] if footer else []):
        if len(msg) + len(blk) + 2 > 3800:
            send(chat, msg)
            msg = ""
        msg += ("\n\n" if msg else "") + blk
    if msg:
        send(chat, msg)


def get_chat_id():
    if not TOKEN:
        sys.exit("Set TELEGRAM_BOT_TOKEN first. Then message your bot once, then run this again.")
    seen = {}
    for u in tg_api("getUpdates").get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        if msg.get("chat"):
            c = msg["chat"]
            seen[c["id"]] = c.get("username") or c.get("title") or c.get("first_name")
        if msg.get("from"):
            seen[msg["from"]["id"]] = "user: " + str(msg["from"].get("username") or msg["from"].get("first_name"))
    if not seen:
        sys.exit("No messages found. Open your bot in Telegram, press Start, send 'hi', run again.")
    for cid, name in seen.items():
        print(f"id: {cid}  ({name})")


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
    """Spaces every Instamart call. A 429 doubles the pause (up to 16x); every good call eases it back."""

    def __init__(self):
        self.scale, self.last = 1.0, 0.0

    def wait(self):
        gap = BASE_GAP * self.scale * random.uniform(0.9, 1.2)
        time.sleep(max(0.0, self.last + gap - time.time()))
        self.last = time.time()

    def hit(self):
        old, self.scale = self.scale, min(self.scale * 2, MAX_SCALE)
        if self.scale != old:
            log(f"429 too many requests -> slowing down: {BASE_GAP * self.scale:.1f}s between calls, "
                f"{INTERVAL * self.scale:.0f}s between rounds")

    def ok(self):
        old, self.scale = self.scale, max(1.0, self.scale * 0.92)
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

    def __init__(self, pin):
        self.text = self.label = pin
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
            found = self._call("GET", BASE + "/api/instamart/maps/suggestions?input=" + urllib.parse.quote(self.text))
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


# ---------- helpers, data ----------
LOCK = threading.RLock()
SUBS = {}      # chat id (str) -> [pincode, ...]
STATE = {}     # pincode -> {coin name: {name, sku, price, mrp, in_stock}}
META = {}      # pincode -> {"addr": str, "ok": ts, "error": str}
WAITING = {}   # pincode -> chats waiting for their first full listing
QUEUE = queue.Queue()
SESS = {}      # pincode -> City (kept between rounds)
LAST = {"round": 0.0}


def save_data():
    if DRY:
        return
    with LOCK:
        blob = {"subs": SUBS, "state": STATE, "addr": {p: m.get("addr", "") for p, m in META.items()}}
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(blob, f)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log(f"Could not save data ({e}). Tracking still works until restart.")


def load_data():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    SUBS.update(blob.get("subs", {}))
    STATE.update(blob.get("state", {}))
    for p, a in blob.get("addr", {}).items():
        META[p] = {"addr": a, "ok": None, "error": ""}


def watched():
    with LOCK:
        return sorted({p for pins in SUBS.values() for p in pins})


def drop_chat(chat):
    with LOCK:
        SUBS.pop(chat, None)
    save_data()


def inr(n):
    return f"₹{n:,}"


def esc(t):
    return html.escape(str(t), quote=False)


def short(name):
    """'Kalyan Jewellers Flower 24 Karat Gold Coin (1 g)' -> 'Flower 24K 1 g'"""
    n = name.replace("Kalyan Jewellers ", "")
    n = re.sub(r"(\d+) Karat Gold Coin \(([^)]*)\)", r"\1K \2", n)
    return n.strip()


def link_line():
    return f'🔗 <a href="{LINK}">Open in Swiggy</a>'


def place_name(addr):
    """'Nashik, Maharashtra 422005, India' -> 'Nashik, Maharashtra'"""
    return re.sub(r"\s*\b\d{6}\b", "", addr or "").replace(", India", "").strip(" ,")


def compare(old, current, had_state):
    """Update `old` (one pincode's saved coins). Return alert lines for changed prices and new coins."""
    lines = []
    for name, cur in current.items():
        prev = old.get(name)
        if prev is None:
            if had_state:
                lines.append(f"🆕 <b>{esc(short(name))}</b>  {inr(cur['price'])}  new listing")
        elif cur["price"] != prev["price"]:
            diff = cur["price"] - prev["price"]
            pct = diff / prev["price"] * 100 if prev["price"] else 0
            lines.append(f"{'🔺' if diff > 0 else '🔻'} <b>{esc(short(name))}</b>\n"
                         f"      {inr(prev['price'])} → {inr(cur['price'])}   "
                         f"<b>{'+' if diff > 0 else '-'}{inr(abs(diff))}</b> ({pct:+.1f}%)")
        old[name] = cur
    return lines


def pin_title(pin):
    place = place_name(META.get(pin, {}).get("addr", ""))
    return f"📍 <b>{pin}</b>" + (f" · {esc(place)}" if place else "")


def pin_report(pin):
    """Every Kalyan coin at this pincode: in stock first, cheapest first, out of stock on one line."""
    items = STATE.get(pin, {})
    if not items:
        return pin_title(pin) + "\nNo Kalyan coins listed here (area may not be served)."
    have = sorted(((n, d) for n, d in items.items() if d["in_stock"]), key=lambda kv: (kv[1]["price"], kv[0]))
    out = sorted(short(n) for n, d in items.items() if not d["in_stock"])
    rows = [f"✅ {esc(short(n))} — {inr(d['price'])}" for n, d in have]
    if out:
        rows.append("⛔ Out: " + esc(", ".join(out)))
    return pin_title(pin) + f" · {len(have)}/{len(items)} in stock\n" + "\n".join(rows) + "\n" + link_line()


def stamp():
    return datetime.now(IST).strftime("%d %b, %I:%M %p IST")


def ago(ts):
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 120 else (f"{s // 60}m ago" if s < 7200 else f"{s // 3600}h ago")


# ---------- tracker (one thread: all Instamart calls) ----------
def process_pin(pin):
    """Check one pincode. First time: save baseline and send the full listing. After: return alert lines."""
    sess = SESS.get(pin) or SESS.setdefault(pin, City(pin))
    try:
        for attempt in (1, 2):
            try:
                doc = sess.search()
                break
            except Blocked as e:
                if not (e.limited and attempt == 1):
                    raise
                time.sleep(5 * PACER.scale)  # 429: cool off once, then try the same pincode again
    except NoPlace:
        with LOCK:
            chats = [c for c, pins in SUBS.items() if pin in pins and pin not in STATE]
            for c in chats:
                SUBS[c].remove(pin)
            WAITING.pop(pin, None)
        for c in chats:
            send(c, f"❌ Instamart does not know pincode <b>{pin}</b>. Check it and send again.")
        save_data()
        return []
    except Exception as e:  # noqa: BLE001
        sess.reset()  # start this pincode from scratch next time
        msg = str(e) if isinstance(e, Blocked) else f"{type(e).__name__}: {e}"
        META.setdefault(pin, {})["error"] = msg
        log(f"{pin}: failed ({msg})")
        return []
    current = parse_items(doc)
    n_in = sum(1 for d in current.values() if d["in_stock"])
    with LOCK:
        META[pin] = {"addr": sess.address, "ok": time.time(), "error": ""}
        first = pin not in STATE
        lines = compare(STATE.setdefault(pin, {}), current, had_state=not first)
        waiting = WAITING.pop(pin, set())
    log(f"{pin}: {place_name(sess.address)} · {n_in}/{len(current)} in stock · store {first_pod(doc) or '?'}"
        + (f" · {len(lines)} change(s)" if lines else ""))
    if waiting or DRY:
        for c in (waiting or {"test"}):
            send(c, pin_report(pin) + f"\n\n✅ Tracking <b>{pin}</b>. I will only message you on a price change "
                 "🔺🔻 or a new listing 🆕.\n/list · /remove " + pin)
    save_data()
    return [] if first else lines


def run_round():
    alerts = {}
    for pin in watched():
        while not QUEUE.empty():  # new pincodes asked for by users go first
            lookup_now(QUEUE.get())
        lines = process_pin(pin)
        if lines:
            alerts[pin] = lines
    with LOCK:
        live = set(watched())
        for pin in list(STATE):  # pincodes nobody tracks any more
            if pin not in live:
                STATE.pop(pin, None)
                META.pop(pin, None)
                SESS.pop(pin, None)
        subs = {c: list(pins) for c, pins in SUBS.items()}
    for chat, pins in subs.items():
        blocks = [pin_title(p) + "\n" + "\n".join(alerts[p]) + "\n" + link_line() for p in pins if p in alerts]
        if blocks:
            send_blocks(chat, f"💰 <b>Kalyan gold coin update</b> · {stamp()}", blocks)
    LAST["round"] = time.time()


def lookup_now(pin):
    if pin in watched() and pin not in STATE:
        process_pin(pin)


def tracker_loop():
    next_round = 0.0
    while True:
        try:
            try:
                lookup_now(QUEUE.get(timeout=2))
                continue
            except queue.Empty:
                pass
            if time.time() >= next_round:
                run_round()
                next_round = time.time() + INTERVAL * PACER.scale * random.uniform(0.95, 1.05)
        except Exception as e:  # noqa: BLE001
            log(f"Tracker loop error ({type(e).__name__}: {e}). Continuing.")
            time.sleep(10)


# ---------- Telegram commands ----------
HELP = ("🪙 <b>Kalyan gold coin tracker</b> (Swiggy Instamart)\n\n"
        "Send a 6-digit <b>pincode</b>, for example <code>422005</code>.\n"
        "I show every Kalyan gold coin there, in stock or out, with prices. Then I track it and message you "
        "only when a price goes 🔺 up or 🔻 down, or a new coin appears 🆕.\n\n"
        "/list · your pincodes\n/remove 422005 · stop tracking one\n/remove all · stop everything")


def allowed(chat, user):
    ids = {str(chat.get("id")), str((user or {}).get("id", ""))}
    return not ALLOWED or bool(ids & (ALLOWED | ADMIN_IDS))


def is_admin(chat, user):
    return bool({str(chat.get("id")), str((user or {}).get("id", ""))} & ADMIN_IDS)


def add_pins(chat_id, pins, admin):
    for pin in pins:
        with LOCK:
            mine = SUBS.setdefault(chat_id, [])
            if pin in mine:
                note = pin_report(pin) if pin in STATE else f"⏳ Still fetching <b>{pin}</b>. Hold on."
                msg = note + ("\n\n(You already track this pincode.)" if pin in STATE else "")
            elif len(mine) >= MAX_PER_CHAT and not admin:
                msg = f"⚠️ You can track {MAX_PER_CHAT} pincodes. /remove one first. /list shows them."
            elif pin not in watched() and len(watched()) >= MAX_TOTAL and not admin:
                msg = "⚠️ The bot is full right now. Try again later."
            else:
                mine.append(pin)
                if pin in STATE:
                    msg = pin_report(pin) + f"\n\n✅ Tracking <b>{pin}</b>. Quiet from now on: only price changes 🔺🔻 and new coins 🆕."
                else:
                    WAITING.setdefault(pin, set()).add(chat_id)
                    QUEUE.put(pin)
                    msg = f"🔎 Checking <b>{pin}</b> on Instamart. Takes up to a minute."
        send(chat_id, msg)
    save_data()


def handle(msg):
    chat, user = msg.get("chat") or {}, msg.get("from")
    text = (msg.get("text") or "").strip()
    if not text or not allowed(chat, user):
        return
    cid, admin = str(chat["id"]), is_admin(chat, user)
    words = text.split()
    cmd = words[0].split("@")[0].lower() if words[0].startswith("/") else ""
    args = " ".join(words[1:]) if cmd else text
    pins = list(dict.fromkeys(re.findall(r"\b\d{6}\b", args)))
    if cmd in ("/start", "/help"):
        send(cid, HELP)
    elif cmd in ("/pin", "/add", "/track", "/check") or (not cmd and re.fullmatch(r"[\d\s,]+", text) and pins):
        if pins:
            add_pins(cid, pins[:5], admin)
        else:
            send(cid, "Send a 6-digit pincode, for example <code>422005</code>.")
    elif cmd == "/list":
        mine = SUBS.get(cid, [])
        if not mine:
            send(cid, "You track nothing yet. Send a 6-digit pincode.")
        else:
            rows = []
            for p in mine:
                items = STATE.get(p)
                stock = f" · {sum(1 for d in items.values() if d['in_stock'])}/{len(items)} in stock" if items else " · fetching"
                rows.append(pin_title(p) + stock)
            send(cid, "📋 <b>Your pincodes</b>\n" + "\n".join(rows) + "\n\n/remove 422005 to stop one")
    elif cmd in ("/remove", "/stop", "/untrack"):
        with LOCK:
            mine = SUBS.get(cid, [])
            gone = list(mine) if args.strip().lower() == "all" else [p for p in pins if p in mine]
            for p in gone:
                mine.remove(p)
            if not mine:
                SUBS.pop(cid, None)
        save_data()
        send(cid, ("🗑 Stopped tracking: " + ", ".join(gone)) if gone else "Nothing removed. /list shows your pincodes.")
    elif cmd == "/status" and admin:
        pins_all = watched()
        errs = [f"{p}: {m['error'][:40]}" for p, m in META.items() if m.get("error")]
        send(cid, "📊 <b>Status</b>\n"
             f"Chats: {len(SUBS)} · pincodes watched: {len(pins_all)}/{MAX_TOTAL}\n"
             f"Last round: {ago(LAST['round']) if LAST['round'] else 'not yet'}\n"
             f"Speed: a round every {INTERVAL * PACER.scale:.0f}s" + (" (slowed by Instamart rate limit)" if PACER.scale > 1.05 else "")
             + ("\n⚠️ " + esc("; ".join(errs[:5])) if errs else ""))
    elif cmd:
        send(cid, HELP)


def command_loop():
    offset, warned = None, False
    while True:
        try:
            payload = {"timeout": 25, "allowed_updates": ["message", "channel_post"]}
            if offset is not None:
                payload["offset"] = offset
            for u in tg_api("getUpdates", payload, timeout=40).get("result", []):
                offset = u["update_id"] + 1
                try:
                    handle(u.get("message") or u.get("channel_post") or {})
                except Exception as e:  # noqa: BLE001
                    log(f"Command error ({type(e).__name__}: {e})")
            warned = False
        except urllib.error.HTTPError as e:
            if e.code == 409 and not warned:
                log("Another copy of this bot is polling Telegram (PC and Voroa both running?). Retrying.")
                warned = True
            time.sleep(30)
        except Exception as e:  # noqa: BLE001
            if not warned:
                log(f"Telegram polling problem ({e}). Retrying.")
                warned = True
            time.sleep(15)


# ---------- main ----------
def main():
    if "--get-chat-id" in sys.argv:
        return get_chat_id()
    if DRY:  # --pin 422005 411045: print what a user would see
        pins = re.findall(r"\b\d{6}\b", " ".join(sys.argv[1:]))
        if not pins:
            sys.exit("Usage: python swiggy_kalyan_tracker.py --pin 422005 411045")
        SUBS["test"] = pins
        for pin in pins:
            process_pin(pin)
        return
    if not TOKEN:
        sys.exit("Set TELEGRAM_BOT_TOKEN first (in .env next to this script).")
    load_data()
    if CHAT_ID and DEFAULT_PINS:
        with LOCK:
            mine = SUBS.setdefault(CHAT_ID, [])
            mine.extend(p for p in DEFAULT_PINS if p not in mine)
    log(f"Tracking '{QUERY}'. {len(watched())} pincode(s) from {len(SUBS)} chat(s). Round every {INTERVAL}s, "
        f"{BASE_GAP}s between calls (auto-slows on 429). Plain HTTP, no browser. Data: {STATE_FILE}")
    threading.Thread(target=tracker_loop, daemon=True).start()
    if os.environ.get("ENABLE_COMMANDS", "1") != "0":
        command_loop()
    else:
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped.")
