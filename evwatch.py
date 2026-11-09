#!/usr/bin/env python3
"""
EVWatch - get a Telegram alert when an EV charger frees up in Singapore.

Data sources: the Charge+ app API (public and private sites) and LTA DataMall.
Notifications: Telegram, plus optional ntfy or a generic webhook.
Everything is configured through environment variables; see .env.example.

Pure standard library - no pip installs needed.

Usage:
  python evwatch.py                  # run the bot
  python evwatch.py discover 123456  # list every LTA connector at a postal code
  python evwatch.py once             # one check of every location, print the result, send nothing
"""
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SGT = timezone(timedelta(hours=8))
NO_LIMIT = datetime(9999, 1, 1, tzinfo=SGT)  # armed with -1: runs until /disarm
API = "https://datamall2.mytransport.sg/ltaodataservice/EVChargingPoints"
LTA_BATCH_API = "https://datamall2.mytransport.sg/ltaodataservice/EVCBatch"
LTA_CACHE_SECONDS = 300  # the full LTA list is a 3 MB download, so reuse it between searches

log = logging.getLogger("evwatch")


# ---------------------------------------------------------------- config
def _env(name, default=""):
    return os.environ.get(name, default).strip()


def _csv(name):
    return [x.strip() for x in _env(name).split(",") if x.strip()]


class Config:
    def __init__(self):
        self.key = _env("LTA_ACCOUNT_KEY")
        self.postal_codes = _csv("POSTAL_CODES")
        self.operator = _env("OPERATOR").lower()                    # "" = any operator (LTA only)
        self.use_lta = _env("USE_LTA", "true").lower() == "true"
        self.use_chargeplus = _env("USE_CHARGEPLUS", "true").lower() == "true"
        self.plug_types = [p.lower() for p in _csv("PLUG_TYPES")]   # e.g. "Type 2,CCS2"
        self.current = _env("CURRENT").upper()                      # "AC", "DC" or ""
        self.min_kw = float(_env("MIN_KW", "0") or 0)
        self.station_filter = _env("STATION_FILTER").lower()        # substring of station/position
        self.poll_seconds = max(MIN_POLL_SECONDS, int(_env("POLL_SECONDS", "120")))
        self.mode = _env("MODE", "manual").lower()                  # "manual" (arm via Telegram) or "always"
        self.arm_minutes = int(_env("ARM_DEFAULT_MINUTES", "180"))
        self.active_hours = _env("ACTIVE_HOURS")                    # e.g. "07-23" (SGT), "always" mode only
        self.notify_taken = _env("NOTIFY_ON_TAKEN", "false").lower() == "true"
        self.tg_token = _env("TELEGRAM_BOT_TOKEN")
        self.tg_chat = _env("TELEGRAM_CHAT_ID")
        self.ntfy_url = _env("NTFY_URL")
        self.webhook_url = _env("WEBHOOK_URL")
        self.state_file = _env("STATE_FILE", "/data/evwatch_state.json")
        self.locations_file = _env("LOCATIONS_FILE", "/data/evwatch_locations.json")
        # Charge+ app API (for private chargers not in LTA's feed)
        self.cp_url = _env("CHARGEPLUS_URL")
        self.cp_method = _env("CHARGEPLUS_METHOD", "GET").upper()
        self.cp_headers_file = _env("CHARGEPLUS_HEADERS_FILE", "/data/chargeplus_headers.json")
        self.cp_body = _env("CHARGEPLUS_BODY")                      # raw JSON body, if the call is a POST
        # Auto-login (keeps the 48h access token fresh). Leave password empty to use a pasted token.
        self.cp_login_url = _env("CHARGEPLUS_LOGIN_URL", "https://api.chargeplus.co/api/v1/auth/login")
        self.cp_idd = _env("CHARGEPLUS_PHONE_IDD", "+65")
        self.cp_phone = _env("CHARGEPLUS_PHONE")
        self.cp_password = _env("CHARGEPLUS_PASSWORD")
        pw_file = _env("CHARGEPLUS_PASSWORD_FILE")
        if pw_file and not self.cp_password:
            with open(pw_file) as f:
                self.cp_password = f.read().strip()
        self.cp_token_file = _env("CHARGEPLUS_TOKEN_FILE", "/data/chargeplus_token.json")
        self.cp_auth = _env("CHARGEPLUS_AUTH", "login").lower()     # login | header | none
        self.notify_finishing = _env("NOTIFY_ON_FINISHING", "true").lower() == "true"


# ---------------------------------------------------------------- data
def http_json(url, headers=None, data=None, timeout=30):
    body = json.dumps(data).encode() if data is not None else None
    h = {"accept": "application/json", **(headers or {})}
    if body is not None:
        h["content-type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def fetch_postal(key, postal):
    url = f"{API}?{urllib.parse.urlencode({'PostalCode': postal})}"
    return extract_locations(http_json(url, {"AccountKey": key}))


def extract_locations(data):
    """Find the list of locations whatever the envelope looks like (evLocationsData / value / bare)."""
    if isinstance(data, list):
        if all(isinstance(x, dict) and "chargingPoints" in x for x in data):
            return data
        out = []
        for x in data:
            out.extend(extract_locations(x))
        return out
    if isinstance(data, dict):
        if "chargingPoints" in data:
            return [data]
        for k in ("evLocationsData", "value", "data"):
            if k in data:
                return extract_locations(data[k])
    return []


def lta_all_locations(cfg):
    """LTA's search list: every charging location in Singapore, with name, address and postal code."""
    link = http_json(LTA_BATCH_API, {"AccountKey": cfg.key})["value"][0]["Link"]
    return extract_locations(http_json(link, timeout=60))


def norm_status(s):
    s = "" if s is None else str(s).strip()
    if s == "1":
        return "available"
    if s == "0":
        return "occupied"
    return "unavailable"


def connectors(locations, postal):
    for loc in locations:
        for cp in loc.get("chargingPoints") or []:
            for pt in cp.get("plugTypes") or []:
                ids = pt.get("evIds") or [{
                    "evCpId": f"{cp.get('id') or cp.get('name')}|{cp.get('position')}|{pt.get('plugType')}",
                    "status": cp.get("status"),
                }]
                for e in ids:
                    try:
                        kw = float(pt.get("chargingSpeed") or 0)
                    except (TypeError, ValueError):
                        kw = 0.0
                    yield {
                        "id": e.get("evCpId") or e.get("id"),
                        "status": norm_status(e.get("status")),
                        "operator": cp.get("operator") or "",
                        "station": loc.get("name") or cp.get("name") or "",
                        "address": loc.get("address") or "",
                        "position": cp.get("position") or "",
                        "hours": cp.get("operatingHours") or "",
                        "plug": pt.get("plugType") or "",
                        "current": (pt.get("powerRating") or "").upper(),
                        "kw": kw,
                        "price": pt.get("price"),
                        "price_type": pt.get("priceType") or "",
                        "lat": loc.get("latitude"),
                        "lon": loc.get("longitude"),
                        "postal": postal,
                    }


# ---------------------------------------------------------------- Charge+ app source
# OCPP 1.6 connector statuses -> our three states. Anything not listed (CHARGING, PREPARING,
# SUSPENDEDEV, SUSPENDEDEVSE, FINISHING, RESERVED, ...) means a car is on the lot.
_CP_FREE = {"AVAILABLE"}
_CP_DOWN = {"FAULTED", "UNAVAILABLE", "OFFLINE", "UNKNOWN", "INOPERATIVE", "OUTOFORDER"}


CP_STATUS_URL = "https://api.chargeplus.co/api/v1/network/getChargerStatus_v2/{}?group=true"
CP_SEARCH_URL = "https://api.chargeplus.co/api/v1/network/getChargingStationList"
SG_CENTRE = {"latitude": 1.3521, "longitude": 103.8198}  # the search API expects a position
MAX_SEARCH_RESULTS = 15


class AuthExpired(Exception):
    pass


def cp_status(s):
    s = (s or "").upper().replace("_", "")
    if s in _CP_FREE:
        return "available"
    if s == "FINISHING":  # session ended, car still plugged in -> likely leaving soon
        return "finishing"
    if s in _CP_DOWN or not s:
        return "unavailable"
    return "occupied"


def _cp_base_headers(cfg):
    try:
        with open(cfg.cp_headers_file) as f:
            headers = json.load(f)
    except FileNotFoundError:
        headers = {}
    # urllib can't decode gzip/br; drop it so the server sends plain JSON
    return {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}


def _cp_set_auth(headers, token):
    for k in [k for k in headers if k.lower() == "authorization"]:
        del headers[k]
    headers["authorization"] = f"Bearer {token}"


def cp_login(cfg, headers):
    h = {k: v for k, v in headers.items() if k.lower() != "authorization"}
    body = {"mobile_phone_idd": cfg.cp_idd, "mobile_phone": cfg.cp_phone, "password": cfg.cp_password}
    try:
        res = (http_json(cfg.cp_login_url, h, data=body) or {}).get("result") or {}
    except urllib.error.HTTPError as e:
        raise AuthExpired(f"Charge+ login failed ({e.code}) — check phone/password.") from e
    token = res.get("token")
    if not token:
        raise AuthExpired("Charge+ login returned no token.")
    expiry = int(res.get("expiry") or time.time() + 47 * 3600)
    try:
        os.makedirs(os.path.dirname(cfg.cp_token_file) or ".", exist_ok=True)
        with open(cfg.cp_token_file, "w") as f:
            json.dump({"token": token, "expiry": expiry}, f)
    except OSError as e:
        log.warning("could not cache Charge+ token: %s", e)
    log.info("Charge+ login ok; token valid until %s", datetime.fromtimestamp(expiry, SGT))
    return token


def _cp_cached_token(cfg):
    try:
        with open(cfg.cp_token_file) as f:
            t = json.load(f)
        if t.get("token") and int(t.get("expiry", 0)) - time.time() > 3600:  # renew 1h early
            return t["token"]
    except (FileNotFoundError, ValueError, OSError):
        pass
    return None


def cp_forget_token(cfg):
    try:
        os.remove(cfg.cp_token_file)
    except OSError:
        pass


def _cp_request_headers(cfg):
    headers = _cp_base_headers(cfg)
    if cfg.cp_auth == "none":
        for k in [k for k in headers if k.lower() == "authorization"]:
            del headers[k]
    auto = cfg.cp_auth == "login" and bool(cfg.cp_phone and cfg.cp_password)
    if auto:
        _cp_set_auth(headers, _cp_cached_token(cfg) or cp_login(cfg, headers))
    return headers, auto


def cp_search(cfg, term):
    """Search Charge+ locations by name, including private sites this account can see."""
    headers, _ = _cp_request_headers(cfg)
    try:
        data = http_json(CP_SEARCH_URL, headers, data={**SG_CENTRE, "search": term})
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthExpired(f"Charge+ returned {e.code} to the search.") from e
        raise
    return ((data or {}).get("result") or {}).get("list") or []


def fetch_chargeplus(cfg, url):
    headers, auto = _cp_request_headers(cfg)
    body = cfg.cp_body.encode() if cfg.cp_body else None
    req = urllib.request.Request(url, data=body, headers=headers, method=cfg.cp_method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            # Charge+ allows one session per account. A 401 on a still-valid token means the
            # phone app signed in and took the session. Don't log straight back in (that would
            # kick the phone out again) - wait for /arm, which starts a fresh login.
            if auto:
                raise AuthExpired("Charge+ session was taken over (the phone app probably signed in). "
                                  "Send /arm to let evwatch sign back in.") from e
            raise AuthExpired(f"Charge+ returned {e.code} — token expired or app headers changed?") from e
        if e.code == 429:
            log.warning("Charge+ rate-limited us (429); backing off")
            time.sleep(300)
        raise


def chargeplus_connectors(data):
    groups = ((data or {}).get("result") or {}).get("list") or []
    for g in groups:
        kw = float(g.get("power") or 0)
        current = (g.get("connector_type_label") or "").upper()
        for c in g.get("connector") or []:
            price = None
            for t in c.get("tariff") or g.get("tariff") or []:
                if t.get("fee_type") == "CHARGE":
                    price = (t.get("current_price") or {}).get("final_price_included_tax")
            yield {
                "id": c.get("connector_id") or c.get("charger_connector_id"),
                "status": cp_status(c.get("ocpp_status_id") or c.get("status_label")),
                "raw_status": c.get("ocpp_status_label") or c.get("status_label") or "",
                "operator": "CHARGE+ (app)",
                "station": c.get("station_name") or "",
                "address": "",
                "position": c.get("park_no") or c.get("direction") or "",
                "hours": "",
                "plug": g.get("charger_model") or "",
                "current": current,
                "kw": kw,
                "price": price,
                "price_type": "kWh",
                "lat": None,
                "lon": None,
                "postal": "",
            }


def matches(c, cfg):
    if c["operator"] == "CHARGE+ (app)":
        cfg_op = ""  # app results are Charge+ by definition
    else:
        cfg_op = cfg.operator
    if cfg_op and cfg_op not in c["operator"].lower():
        return False
    if cfg.plug_types and not any(p in c["plug"].lower() for p in cfg.plug_types):
        return False
    if cfg.current and c["current"] != cfg.current:
        return False
    if c["kw"] < cfg.min_kw:
        return False
    if cfg.station_filter and cfg.station_filter not in f"{c['station']} {c['position']}".lower():
        return False
    return True


def describe(c):
    price = ""
    if c["price"] not in (None, ""):
        price = f" · ${float(c['price']):.2f}/{c['price_type'] or 'kWh'}"
    pos = f" — {c['position']}" if c["position"] else ""
    return f"{c['station']}{pos} · {c['plug']} {c['current']} {c['kw']:g}kW{price}"


def maps_link(c):
    if c["lat"] and c["lon"]:
        return f"https://maps.google.com/?q={c['lat']},{c['lon']}"
    return ""


# ---------------------------------------------------------------- notify
class Notifier:
    def __init__(self, cfg):
        self.cfg = cfg

    def send(self, text, payload=None):
        log.info("NOTIFY: %s", text.replace("\n", " | "))
        cfg = self.cfg
        if cfg.tg_token and cfg.tg_chat:
            self._try(lambda: http_json(
                f"https://api.telegram.org/bot{cfg.tg_token}/sendMessage",
                data={"chat_id": cfg.tg_chat, "text": text, "disable_web_page_preview": True}))
        if cfg.ntfy_url:
            def _ntfy():
                req = urllib.request.Request(cfg.ntfy_url, data=text.encode(),
                                             headers={"Title": "EV charger", "Tags": "zap"})
                urllib.request.urlopen(req, timeout=15).read()
            self._try(_ntfy)
        if cfg.webhook_url:
            self._try(lambda: http_json(cfg.webhook_url, data={"message": text, **(payload or {})}))

    @staticmethod
    def _try(fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            log.warning("notification failed: %s", e)


# ---------------------------------------------------------------- locations
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
LOCATION_GAP_SECONDS = 3  # pause between locations; Charge+ rate-limits with 429s
MIN_POLL_SECONDS = 120  # hard floor: polling faster risks getting the account banned
MAX_POLL_SECONDS = 24 * 3600
_POLL_VALUE_RE = re.compile(r"default|\d+(\.\d+)?", re.I)


def cp_location(url, name=""):
    m = _UUID_RE.search(url)
    return {"key": f"cp:{m.group(0).lower() if m else url}", "kind": "cp", "url": url, "name": name}


def lta_location(postal, name="", env=False):
    loc = {"key": f"lta:{postal}", "kind": "lta", "postal": postal, "name": name or f"Postal {postal}"}
    if env:
        loc["env"] = True
    return loc


def load_locations(cfg):
    """POSTAL_CODES come from the environment while USE_LTA is on. Everything added with /add lives
    in LOCATIONS_FILE, which is seeded from CHARGEPLUS_URL (comma-separated) the first time only."""
    locs = [lta_location(p, env=True) for p in cfg.postal_codes] if cfg.use_lta else []
    try:
        with open(cfg.locations_file) as f:
            saved = json.load(f)
    except FileNotFoundError:
        saved = [cp_location(url) for url in _csv("CHARGEPLUS_URL")] if cfg.use_chargeplus else []
    keys = {l["key"] for l in locs}
    for loc in saved:
        if loc["key"] not in keys:
            keys.add(loc["key"])
            locs.append(loc)
    return locs


def save_locations(cfg, locations):
    try:
        os.makedirs(os.path.dirname(cfg.locations_file) or ".", exist_ok=True)
        tmp = cfg.locations_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump([l for l in locations if not l.get("env")], f, indent=2)
        os.replace(tmp, cfg.locations_file)
    except OSError as e:
        log.warning("could not save locations: %s", e)


def interval_seconds(value):
    """Turn a "<minutes>" or "default" value into (seconds, error). "default" gives (None, None)."""
    if value.lower() == "default":
        return None, None
    seconds = round(float(value) * 60)
    if not MIN_POLL_SECONDS <= seconds <= MAX_POLL_SECONDS:
        return None, f"Use a value from {MIN_POLL_SECONDS / 60:g} to {MAX_POLL_SECONDS // 60} minutes."
    return seconds, None


def until_text(until):
    return "with no time limit" if until == NO_LIMIT else f"until {until:%H:%M}"


def minutes_text(seconds):
    return f"{seconds / 60:g} min"


def label(loc):
    if loc.get("name"):
        return loc["name"]
    return f"Location {loc['key'].split(':', 1)[1][:8]}"


# ---------------------------------------------------------------- watcher
class Watcher:
    def __init__(self, cfg):
        self.cfg = cfg
        self.notifier = Notifier(cfg)
        self.lock = threading.Lock()
        self.fetch_lock = threading.Lock()  # one API call at a time, so two logins can't collide
        self.wake = threading.Event()
        self.prev = {}            # connector id -> status
        self.locations = load_locations(cfg)
        self.armed = {}           # location key -> datetime (manual mode)
        self.last_search = []     # results of the latest /find
        self.lta_cache = (0, [])  # (fetched at, every LTA location)
        self.next_poll = {}       # location key -> time.monotonic() of its next check
        self.fails = {}           # location key -> consecutive failed checks
        self.fail_alerts = set()  # location keys we've already warned about
        self.auth_alerted = False
        self._load()

    # ---- persistence
    def _load(self):
        try:
            with open(self.cfg.state_file) as f:
                s = json.load(f)
            self.prev = s.get("prev", {})
            keys = {l["key"] for l in self.locations}
            self.armed = {k: datetime.fromisoformat(v) for k, v in (s.get("armed") or {}).items() if k in keys}
            if s.get("armed_until"):  # state written by the single-location version
                self.armed = {k: datetime.fromisoformat(s["armed_until"]) for k in keys}
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("could not load state: %s", e)

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.cfg.state_file) or ".", exist_ok=True)
            tmp = self.cfg.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"prev": self.prev,
                           "armed": {k: v.isoformat() for k, v in self.armed.items()}}, f)
            os.replace(tmp, self.cfg.state_file)
        except Exception as e:  # noqa: BLE001
            log.warning("could not save state: %s", e)

    # ---- arming
    def _in_active_hours(self, now):
        if not self.cfg.active_hours:
            return True
        try:
            a, b = (int(x) for x in self.cfg.active_hours.split("-"))
        except ValueError:
            return True
        return a <= now.hour < b if a <= b else (now.hour >= a or now.hour < b)

    def interval(self, loc):
        return max(MIN_POLL_SECONDS, loc.get("poll_seconds") or self.cfg.poll_seconds)

    def set_interval(self, loc, seconds):
        """Set a location's polling interval; None returns it to the default."""
        with self.lock:
            if seconds is None:
                loc.pop("poll_seconds", None)
            else:
                loc["poll_seconds"] = seconds
            save_locations(self.cfg, self.locations)
            self.next_poll[loc["key"]] = time.monotonic() + self.interval(loc)
        self.wake.set()

    def is_active(self, loc, now=None):
        now = now or datetime.now(SGT)
        if self.cfg.mode == "always":
            return self._in_active_hours(now)
        until = self.armed.get(loc["key"])
        return bool(until and now < until)

    def active_locations(self):
        now = datetime.now(SGT)
        with self.lock:
            return [l for l in self.locations if self.is_active(l, now)]

    def arm(self, locs, minutes):
        until = NO_LIMIT if minutes == -1 else datetime.now(SGT) + timedelta(minutes=minutes)
        with self.lock:
            for loc in locs:
                self.armed[loc["key"]] = until
                self.next_poll[loc["key"]] = time.monotonic() + self.interval(loc)  # /arm checks it now
            self._save()
        self.wake.set()
        return until

    def disarm(self, locs=None):
        with self.lock:
            for key in ([l["key"] for l in locs] if locs is not None else list(self.armed)):
                self.armed.pop(key, None)
            self._save()

    def _expire(self):
        if self.cfg.mode != "manual":
            return
        now = datetime.now(SGT)
        with self.lock:
            done = [l for l in self.locations if l["key"] in self.armed and now >= self.armed[l["key"]]]
            for loc in done:
                del self.armed[loc["key"]]
            if done:
                self._save()
        for loc in done:
            self.notifier.send(f"⏹ EV watch expired for {label(loc)} — send /arm to watch again.")

    def watch_text(self, loc):
        until = self.armed.get(loc["key"])
        if self.cfg.mode == "always":
            return "Always-on mode."
        return f"Watching {until_text(until)}." if until else "Not watching (send /arm)."

    # ---- location management
    def resolve(self, text):
        """Match a location by list number or part of its name. Returns (locations, error)."""
        text = text.strip().lower()
        if text.isdigit():
            i = int(text)
            if 1 <= i <= len(self.locations):
                return [self.locations[i - 1]], None
            return [], f"There's no location {i}."
        found = [l for l in self.locations if text in label(l).lower()]
        if not found:
            return [], f"No location matches \"{text}\"."
        if len(found) > 1:
            return [], f"\"{text}\" matches {len(found)} locations — be more specific or use the number."
        return found, None

    def locations_text(self):
        lines = ["📍 Locations:"]
        for i, loc in enumerate(self.locations, 1):
            until = self.armed.get(loc["key"])
            state = "always on" if self.cfg.mode == "always" else (
                f"👀 watching {until_text(until)}" if until else "not watching")
            lines.append(f"{i}. {label(loc)} — {state} · every {minutes_text(self.interval(loc))}\n")
        lines.append("Use /arm <number or name> [minutes] [poll minutes], or /arm all.")
        return "\n".join(lines)

    def add_location(self, loc):
        with self.lock:
            self.locations.append(loc)
            save_locations(self.cfg, self.locations)
            return len(self.locations)

    def remove_location(self, loc):
        with self.lock:
            self.locations.remove(loc)
            self.armed.pop(loc["key"], None)
            self.next_poll.pop(loc["key"], None)
            self.fails.pop(loc["key"], None)
            self.fail_alerts.discard(loc["key"])
            save_locations(self.cfg, self.locations)
            self._save()

    # ---- polling
    def fetch(self, loc):
        """Every connector at a location, before filters."""
        with self.fetch_lock:
            if loc["kind"] == "lta":
                return list(connectors(fetch_postal(self.cfg.key, loc["postal"]), loc["postal"]))
            return list(chargeplus_connectors(fetch_chargeplus(self.cfg, loc["url"])))

    def snapshot(self, loc):
        conns = self.fetch(loc)
        if not loc.get("name"):
            name = next((c["station"] for c in conns if c["station"]), "")
            if name:
                with self.lock:
                    loc["name"] = name
                    save_locations(self.cfg, self.locations)
        return [c for c in conns if matches(c, self.cfg)]

    def summary(self, conns):
        if not conns:
            return "No matching connectors found — check the location and filters."
        free = [c for c in conns if c["status"] == "available"]
        fin = [c for c in conns if c["status"] == "finishing"]
        occ = sum(1 for c in conns if c["status"] == "occupied") + len(fin)
        down = sum(1 for c in conns if c["status"] == "unavailable")
        fin_txt = f" ({len(fin)} finishing)" if fin else ""
        lines = [f"{len(free)} free · {occ} occupied{fin_txt} · {down} out of service ({len(conns)} matching)"]
        for c in free[:8]:
            lines.append(f"✅ {describe(c)}")
        for c in fin[:8]:
            lines.append(f"🟡 {describe(c)} — done charging, still plugged in")
        return "\n".join(lines)

    def poll_location(self, loc, baseline=False):
        """Poll one location. With baseline=True, record the current state without alerting."""
        conns = self.snapshot(loc)
        now = datetime.now(SGT)
        with self.lock:
            active = self.is_active(loc, now)
            newly_free, newly_taken, newly_finishing = [], [], []
            for c in conns:
                before = self.prev.get(c["id"])
                if c["status"] == "available" and before is not None and before != "available":
                    newly_free.append(c)
                if c["status"] == "finishing" and before is not None and before not in ("finishing", "available"):
                    newly_finishing.append(c)
                if before == "available" and c["status"] != "available":
                    newly_taken.append(c)
                self.prev[c["id"]] = c["status"]
            self._save()

        free_total = sum(1 for c in conns if c["status"] == "available")
        log.info("poll %s: %d matching, %d free, %d newly free, active=%s",
                 label(loc), len(conns), free_total, len(newly_free), active)

        if baseline or not active:
            return conns
        name = label(loc)
        if newly_free:
            head = "⚡ Charger free!" if len(newly_free) == 1 else f"⚡ {len(newly_free)} chargers free!"
            body = "\n".join(f"• {describe(c)}" for c in newly_free)
            link = maps_link(newly_free[0])
            text = f"{head}\n{body}\n({free_total} of {len(conns)} free now)" + (f"\n{link}" if link else "")
            self.notifier.send(text, {"event": "free", "location": name, "free": free_total,
                                      "total": len(conns), "connectors": newly_free})
        if self.cfg.notify_finishing and newly_finishing and free_total == 0:
            body = "\n".join(f"• {describe(c)}" for c in newly_finishing)
            self.notifier.send(f"🟡 About to free up — car done charging, still plugged in:\n{body}",
                               {"event": "finishing", "location": name, "connectors": newly_finishing})
        if self.cfg.notify_taken and newly_taken and free_total == 0:
            self.notifier.send(f"🔴 All {len(conns)} matching chargers at {name} are taken again.",
                               {"event": "all_taken", "location": name, "free": 0, "total": len(conns)})
        return conns

    def clear_failures(self, loc):
        self.fails.pop(loc["key"], None)
        self.fail_alerts.discard(loc["key"])
        if loc["kind"] == "cp":
            self.auth_alerted = False

    def _poll_succeeded(self, loc):
        recovered = loc["key"] in self.fail_alerts or (loc["kind"] == "cp" and self.auth_alerted)
        self.clear_failures(loc)
        if recovered:
            self.notifier.send(f"✅ EV watch: {label(loc)} is reachable again.")

    def _poll_failed(self, loc, e):
        key = loc["key"]
        self.fails[key] = self.fails.get(key, 0) + 1
        log.warning("poll failed for %s (%d in a row): %s", label(loc), self.fails[key], e)
        if self.fails[key] >= 5 and key not in self.fail_alerts:
            self.notifier.send(f"⚠️ EV watch can't check {label(loc)}: {e}. Will keep retrying.")
            self.fail_alerts.add(key)

    def poll_cycle(self, due):
        now = time.monotonic()
        for loc in due:  # scheduled up front, so a failed check can't be retried in a tight loop
            self.next_poll[loc["key"]] = now + self.interval(loc)
        for i, loc in enumerate(due):
            if i:
                time.sleep(LOCATION_GAP_SECONDS)
            if not self.is_active(loc):  # disarmed or removed during this cycle
                continue
            try:
                self.poll_location(loc)
            except AuthExpired as e:  # the session is account-wide, so stop this cycle
                log.warning("%s", e)
                if not self.auth_alerted:
                    self.notifier.send(f"🔑 {e} Re-capture the request headers from the Charge+ app "
                                       f"and update {self.cfg.cp_headers_file}.")
                    self.auth_alerted = True
                return
            except Exception as e:  # noqa: BLE001
                self._poll_failed(loc, e)
            else:
                self._poll_succeeded(loc)

    def tick(self):
        """Check whichever active locations are due. Returns the seconds to wait before the next tick."""
        self._expire()
        # Only hit the APIs for armed locations / inside active hours (Charge+ rate-limits with 429s).
        now = time.monotonic()
        due = [l for l in self.active_locations() if self.next_poll.get(l["key"], 0) <= now]
        if due:
            self.poll_cycle(due)
        now = time.monotonic()
        waits = [self.next_poll[l["key"]] - now for l in self.active_locations() if l["key"] in self.next_poll]
        return max(1, min(waits + [self.cfg.poll_seconds]))  # capped, so a long interval can't delay expiry

    def run(self):
        log.info("tracking %d location(s): %s (operator=%r, mode=%s, default every %ss)",
                 len(self.locations), ", ".join(label(l) for l in self.locations),
                 self.cfg.operator or "any", self.cfg.mode, self.cfg.poll_seconds)
        while True:
            self.wake.wait(self.tick())
            self.wake.clear()


# ---------------------------------------------------------------- telegram commands
HELP = ("⚡ EV Watch\n\n"
        "Watches the EV charging locations you choose, from Charge+ and LTA DataMall, "
        "and alerts you here when a charger frees up.\n\n"
        "To start: /find a location, /add it, then /arm it.\n\n"
        "Commands\n\n"
        "📍 /locations — list all locations\n\n"
        "👀 /arm <number or name> [minutes] [poll minutes] — alert me when a charger frees up there (default {d} min, -1 for no limit)\n\n"
        "🌐 /arm all [minutes] [poll minutes] — watch every location\n\n"
        "⏹ /disarm [number or name] — stop alerts (everywhere if none given)\n\n"
        "⏱ /setpoll <number or name> <minutes or default> — how often to check a location (default {p}, minimum {m})\n\n"
        "📊 /status [number or name] — what's free right now\n\n"
        "🔌 /find cp <name> — search Charge+, including private sites\n\n"
        "🔍 /find lta <name, address or postal code> — search LTA DataMall\n\n"
        "➕ /add <result number> — track a location from /find (or /add <ID or URL> [name])\n\n"
        "➖ /rmlocation <number or name> — stop tracking a location")


def help_text(cfg):
    return HELP.format(d=cfg.arm_minutes, p=minutes_text(cfg.poll_seconds), m=minutes_text(MIN_POLL_SECONDS))


def telegram_loop(w: Watcher):
    cfg = w.cfg
    base = f"https://api.telegram.org/bot{cfg.tg_token}"
    offset = None
    while True:
        try:
            q = {"timeout": 50, "allowed_updates": json.dumps(["message"])}
            if offset is not None:
                q["offset"] = offset
            res = http_json(f"{base}/getUpdates?{urllib.parse.urlencode(q)}", timeout=65)
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                if str(msg.get("chat", {}).get("id")) != str(cfg.tg_chat):
                    continue  # ignore anyone else who finds the bot
                handle_command(w, (msg.get("text") or "").strip())
        except Exception as e:  # noqa: BLE001
            log.warning("telegram poll error: %s", e)
            time.sleep(10)


def _is_duration(text):
    return text.isdigit() or text == "-1"


def parse_arm(w: Watcher, args):
    """Returns (locations, minutes, poll value or None, error).
    With one location, a lone number means minutes."""
    mins = w.cfg.arm_minutes
    args = list(args)
    if len(w.locations) == 1 and len(args) == 1 and _is_duration(args[0]):
        return w.locations[:], int(args[0]), None, None
    if not args:
        if len(w.locations) == 1:
            return w.locations[:], mins, None, None
        return [], mins, None, "Which location?"
    poll = None
    if len(args) > 2 and _is_duration(args[-2]) and _POLL_VALUE_RE.fullmatch(args[-1]):
        poll = args.pop()
    if len(args) > 1 and _is_duration(args[-1]):
        mins = int(args.pop())
    target = " ".join(args)
    if target.lower() == "all":
        return w.locations[:], mins, poll, None
    locs, err = w.resolve(target)
    return locs, mins, poll, err


def poll_with_relogin(w: Watcher, loc):
    try:
        return w.poll_location(loc, baseline=True)
    except AuthExpired:
        if loc["kind"] != "cp" or w.cfg.cp_auth != "login":
            raise
        cp_forget_token(w.cfg)  # session was taken over by the phone: sign in again
        return w.poll_location(loc, baseline=True)


def handle_arm(w: Watcher, args):
    n = w.notifier
    locs, mins, poll, err = parse_arm(w, args)
    if err:
        n.send(f"{err}\n\n{w.locations_text()}")
        return
    if poll is not None:
        seconds, err = interval_seconds(poll)
        if err:
            n.send(f"Nothing armed. {err}")
            return
        for loc in locs:
            if not loc.get("env"):
                w.set_interval(loc, seconds)
    until = w.arm(locs, mins)
    for i, loc in enumerate(locs):
        if i:
            time.sleep(LOCATION_GAP_SECONDS)
        try:
            conns = poll_with_relogin(w, loc)
            w.clear_failures(loc)
            n.send(f"👀 Watching {label(loc)} {until_text(until)}, checking every {minutes_text(w.interval(loc))}.\n"
                   f"{w.summary(conns)}\nI'll ping you when one frees up.")
        except Exception as e:  # noqa: BLE001
            n.send(f"👀 Watching {label(loc)} {until_text(until)}, but the first check failed: {e}")
    n.send(w.locations_text())


def handle_status(w: Watcher, args):
    n = w.notifier
    locs = w.locations[:]
    if args:
        locs, err = w.resolve(" ".join(args))
        if err:
            n.send(f"{err}\n\n{w.locations_text()}")
            return
    for i, loc in enumerate(locs):
        if i:
            time.sleep(LOCATION_GAP_SECONDS)
        try:
            # snapshot, not a poll: a status check must not use up a pending "charger free" alert
            n.send(f"📍 {label(loc)}\n{w.summary(w.snapshot(loc))}\n{w.watch_text(loc)}")
        except Exception as e:  # noqa: BLE001
            n.send(f"Couldn't check {label(loc)}: {e}")


SOURCES = {"cp": "cp", "charge+": "cp", "chargeplus": "cp", "lta": "lta", "datamall": "lta"}


def search_chargeplus(w: Watcher, term):
    with w.fetch_lock:
        try:
            results = cp_search(w.cfg, term)
        except AuthExpired:
            if w.cfg.cp_auth != "login":
                raise
            cp_forget_token(w.cfg)  # session was taken over by the phone: sign in again
            results = cp_search(w.cfg, term)
    found = []
    for r in results:
        stat = r.get("charging_station_stat") or {}
        found.append({"source": "cp", "id": str(r.get("charging_station_id") or ""),
                      "name": r.get("station_name") or "Unnamed", "address": r.get("address") or "No address",
                      "free": stat.get("available_charger", "?"), "total": stat.get("charger_count", "?"),
                      "private": (r.get("station_type_label") or "").lower() == "private"})
    return found


def search_lta(w: Watcher, term):
    with w.fetch_lock:
        fetched, locs = w.lta_cache
        if time.time() - fetched > LTA_CACHE_SECONDS:
            locs = lta_all_locations(w.cfg)
            w.lta_cache = (time.time(), locs)
    term = term.lower()
    found = []
    for loc in locs:
        postal = str(loc.get("postalCode") or "")
        if postal and term in f"{loc.get('name')} {loc.get('address')} {postal}".lower():
            conns = list(connectors([loc], postal))
            found.append({"source": "lta", "id": postal, "name": loc.get("name") or "Unnamed",
                          "address": loc.get("address") or "No address", "private": False,
                          "free": sum(1 for c in conns if c["status"] == "available"), "total": len(conns)})
    return found


def handle_find(w: Watcher, args):
    n = w.notifier
    source = SOURCES.get(args[0].lower()) if args else None
    term = " ".join(args[1:])
    if not source or not term:
        n.send("Choose where to search:\n"
               "/find cp <name> — Charge+, including private sites\n"
               "/find lta <name, address or postal code> — LTA DataMall")
        return
    if source == "cp" and not w.cfg.use_chargeplus:
        n.send("Charge+ is turned off (USE_CHARGEPLUS).")
        return
    if source == "lta" and not w.cfg.key:
        n.send("LTA search needs LTA_ACCOUNT_KEY.")
        return
    name = "Charge+" if source == "cp" else "LTA"
    try:
        results = search_chargeplus(w, term) if source == "cp" else search_lta(w, term)
    except Exception as e:  # noqa: BLE001
        n.send(f"{name} search failed: {e}")
        return
    w.last_search = results[:MAX_SEARCH_RESULTS]
    if not results:
        n.send(f"No {name} locations match \"{term}\".")
        return
    tracked = {l["key"] for l in w.locations}
    lines = [f"🔍 {name} results for \"{term}\":"]
    for i, r in enumerate(w.last_search, 1):
        mark = " · already tracked" if f"{r['source']}:{r['id'].lower()}" in tracked else ""
        lines.append(f"{i}. {'🔒 ' if r['private'] else ''}{r['name']}\n"
                     f"    {r['address']} · {r['free']} of {r['total']} free{mark}\n")
    if len(results) > len(w.last_search):
        lines.append(f"Showing the first {len(w.last_search)} of {len(results)} — narrow your search to see more.")
    lines.append("Send /add <number> to track one.")
    n.send("\n".join(lines))


def handle_add(w: Watcher, args):
    n = w.notifier
    name = " ".join(args[1:]).strip()
    source, ident = "cp", args[0] if args else ""
    if args and args[0].isdigit():
        i = int(args[0])
        if not 1 <= i <= len(w.last_search):
            n.send(f"There's no result {i}. Run /find first.")
            return
        picked = w.last_search[i - 1]
        source, ident, name = picked["source"], picked["id"], name or picked["name"]
    if source == "lta":
        loc = lta_location(ident, name)
    else:
        if not w.cfg.use_chargeplus:
            n.send("Charge+ is turned off (USE_CHARGEPLUS).")
            return
        m = _UUID_RE.search(ident)
        if not m:
            n.send("Use /find, then /add <number>. You can also send /add <Charge+ ID or URL> [name].")
            return
        # Always build the URL ourselves, so the Charge+ login can't be sent to a pasted address.
        loc = cp_location(CP_STATUS_URL.format(m.group(0).lower()), name)
    for i, existing in enumerate(w.locations, 1):
        if existing["key"] == loc["key"]:
            n.send(f"Already tracking that as {i}. {label(existing)}.")
            return
    try:
        conns = w.fetch(loc)
    except Exception as e:  # noqa: BLE001
        n.send(f"Couldn't load that location, so I didn't add it: {e}")
        return
    if not conns:
        n.send("That source returned no chargers for the location, so I didn't add it.")
        return
    if not loc["name"]:
        loc["name"] = next((c["station"] for c in conns if c["station"]), "")
    i = w.add_location(loc)
    n.send(f"➕ Added {i}. {label(loc)} ({len(conns)} chargers). Send /arm {i} to watch it.")


def handle_remove(w: Watcher, args):
    n = w.notifier
    if not args:
        n.send(f"Which one?\n/rmlocation <number or name>\n\n{w.locations_text()}")
        return
    locs, err = w.resolve(" ".join(args))
    if err:
        n.send(f"{err}\n\n{w.locations_text()}")
        return
    loc = locs[0]
    if loc.get("env"):
        n.send(f"{label(loc)} comes from POSTAL_CODES, so remove it in docker-compose.yml.")
    elif len(w.locations) == 1:
        n.send("That's your only location, so I've kept it.")
    else:
        w.remove_location(loc)
        n.send(f"➖ Removed {label(loc)}.")


def handle_setpoll(w: Watcher, args):
    n = w.notifier
    usage = "/setpoll <number or name> <minutes or default>"
    if not args:
        n.send(f"{usage}\n\n{w.locations_text()}")
        return
    args = list(args)
    value = None
    if len(args) > 1 and _POLL_VALUE_RE.fullmatch(args[-1]):
        value = args.pop().lower()
    locs, err = w.resolve(" ".join(args))
    if err:
        n.send(f"{err}\n\n{w.locations_text()}")
        return
    loc = locs[0]
    default = minutes_text(w.cfg.poll_seconds)
    if value is None:
        n.send(f"⏱ {label(loc)} is checked every {minutes_text(w.interval(loc))} (default {default}).\n"
               f"To change it: {usage}")
    elif loc.get("env"):
        n.send(f"{label(loc)} comes from POSTAL_CODES, so it uses POLL_SECONDS from docker-compose.yml.")
    elif value == "default":
        w.set_interval(loc, None)
        n.send(f"⏱ {label(loc)} is back to the default: every {default}.")
    else:
        seconds, err = interval_seconds(value)
        if err:
            n.send(err)
            return
        w.set_interval(loc, seconds)
        n.send(f"⏱ {label(loc)} will be checked every {minutes_text(seconds)}.")


def handle_command(w: Watcher, text):
    parts = text.split()
    if not parts:
        return
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]
    n = w.notifier
    if cmd in ("/arm", "/watch", "/start_watch"):
        handle_arm(w, args)
    elif cmd in ("/disarm", "/stop"):
        if not args or " ".join(args).lower() == "all":
            w.disarm()
            n.send("⏹ Stopped watching everywhere.")
            n.send(w.locations_text())
            return
        locs, err = w.resolve(" ".join(args))
        if err:
            n.send(f"{err}\n\n{w.locations_text()}")
            return
        w.disarm(locs)
        n.send(f"⏹ Stopped watching {label(locs[0])}.")
        n.send(w.locations_text())
    elif cmd in ("/status", "/now"):
        handle_status(w, args)
    elif cmd in ("/locations", "/list"):
        n.send(w.locations_text())
    elif cmd == "/find":
        handle_find(w, args)
    elif cmd in ("/add", "/addlocation"):
        handle_add(w, args)
    elif cmd == "/rmlocation":
        handle_remove(w, args)
    elif cmd == "/setpoll":
        handle_setpoll(w, args)
    elif cmd in ("/help", "/start"):
        n.send(help_text(w.cfg))


# ---------------------------------------------------------------- CLI
def cmd_discover(cfg, postal):
    locs = fetch_postal(cfg.key, postal)
    conns = list(connectors(locs, postal))
    if not conns:
        print(f"No chargers returned for postal code {postal}.")
        return
    print(f"{len(conns)} connectors at {postal}:\n")
    print(f"{'STATUS':<12} {'OPERATOR':<16} {'PLUG':<10} {'AC/DC':<5} {'KW':>6}  {'POSITION':<18} STATION")
    for c in sorted(conns, key=lambda c: (c["operator"], c["position"])):
        print(f"{c['status']:<12} {c['operator'][:16]:<16} {c['plug'][:10]:<10} {c['current']:<5} "
              f"{c['kw']:>6g}  {c['position'][:18]:<18} {c['station']}")
    ops = sorted({c["operator"] for c in conns})
    print(f"\nOperators seen: {', '.join(ops)}")


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config()
    args = sys.argv[1:]
    if args and args[0] == "discover":
        if not cfg.key:
            sys.exit("Set LTA_ACCOUNT_KEY (free from https://datamall.lta.gov.sg).")
        if len(args) < 2:
            sys.exit("usage: evwatch.py discover <postal code>")
        return cmd_discover(cfg, args[1])
    lta_on = cfg.use_lta and bool(cfg.postal_codes)
    cp_on = (cfg.use_chargeplus and bool(cfg.cp_url)) or os.path.exists(cfg.locations_file)
    if lta_on and not cfg.key:
        sys.exit("USE_LTA needs LTA_ACCOUNT_KEY (free from https://datamall.lta.gov.sg).")
    if cfg.use_lta and not cfg.postal_codes:
        log.warning("USE_LTA=true but POSTAL_CODES is empty; LTA source disabled")
    if not (lta_on or cp_on or (cfg.tg_token and cfg.tg_chat)):  # with Telegram, locations can be added by /add
        sys.exit("Nothing to watch: set up Telegram, or USE_LTA + POSTAL_CODES, or USE_CHARGEPLUS + CHARGEPLUS_URL.")
    log.info("sources: POSTAL_CODES=%s saved locations=%s", "on" if lta_on else "off", "on" if cp_on else "off")
    w = Watcher(cfg)
    if args and args[0] == "once":
        for loc in w.locations:
            print(f"{label(loc)}\n{w.summary(w.snapshot(loc))}\n")
        return
    if cfg.mode == "manual" and not (cfg.tg_token and cfg.tg_chat):
        log.warning("MODE=manual needs Telegram to /arm; falling back to MODE=always")
        cfg.mode = "always"
    if cfg.tg_token and cfg.tg_chat:
        threading.Thread(target=telegram_loop, args=(w,), daemon=True).start()
        w.notifier.send(help_text(cfg) if cfg.mode == "manual"
                        else "⚡ EV watch started (always-on).")
    w.run()


if __name__ == "__main__":
    main()
