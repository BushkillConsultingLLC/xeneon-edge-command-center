#!/usr/bin/env python3
"""Command Center local server for the Xeneon Edge.

Serves the dashboard at http://127.0.0.1:8888 (only reachable from this Mac),
fetches your calendar feeds, reads recent iMessages, and serves Claude's
status file. Uses only the Python standard library.
"""
import glob
import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8888
ALLOWED_HOSTS = {"127.0.0.1:%d" % PORT, "localhost:%d" % PORT}
CONFIG_PATH = os.path.join(ROOT, "config.json")
HOME = os.path.expanduser("~")
CHAT_DB = os.path.join(HOME, "Library", "Messages", "chat.db")
AB_GLOB = os.path.join(HOME, "Library", "Application Support", "AddressBook", "Sources", "*", "AddressBook-v22.abcddb")
AB_MAIN = os.path.join(HOME, "Library", "Application Support", "AddressBook", "AddressBook-v22.abcddb")

_ics_cache = {}        # index -> (time, text)
_contacts = {"t": 0, "map": {}}


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {"calendars": []}


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


# ---------------- contacts ----------------
def norm_handle(h):
    h = (h or "").strip().lower()
    if "@" in h:
        return h
    digits = re.sub(r"\D", "", h)
    return digits[-10:] if len(digits) >= 10 else digits


def load_contacts():
    if time.time() - _contacts["t"] < 600:
        return _contacts["map"]
    m = {}
    for path in glob.glob(AB_GLOB) + [AB_MAIN]:
        if not os.path.exists(path):
            continue
        try:
            con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
            names = {}
            for pk, first, last, org in con.execute(
                    "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION FROM ZABCDRECORD"):
                n = " ".join(x for x in (first, last) if x) or org
                if n:
                    names[pk] = n
            for owner, num in con.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                if owner in names and num:
                    m[norm_handle(num)] = names[owner]
            for owner, addr in con.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                if owner in names and addr:
                    m[norm_handle(addr)] = names[owner]
            con.close()
        except Exception:
            continue
    _contacts["t"] = time.time()
    _contacts["map"] = m
    return m


# ---------------- messages ----------------
def text_from_attributed(blob):
    """Pull the plain text out of an attributedBody typedstream blob."""
    if not blob:
        return ""
    try:
        b = bytes(blob)
        i = b.find(b"NSString")
        if i < 0:
            return ""
        j = b.find(b"+", i)          # length marker follows a '+' byte
        if j < 0:
            return ""
        j += 1
        n = b[j]
        if n == 0x81:                 # 2-byte length
            n = int.from_bytes(b[j + 1:j + 3], "little"); j += 3
        elif n == 0x82:               # 4-byte length
            n = int.from_bytes(b[j + 1:j + 5], "little"); j += 5
        else:
            j += 1
        return b[j:j + n].decode("utf-8", "replace")
    except Exception:
        return ""


def recent_messages(limit=12):
    if not os.path.exists(CHAT_DB):
        return {"ok": False, "error": "no_db"}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % CHAT_DB, uri=True, timeout=2)
    except Exception as e:
        return {"ok": False, "error": "no_access", "detail": str(e)}
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # Deleted messages move to "Recently Deleted": they lose their row in
        # chat_message_join and gain one in chat_recoverable_message_join.
        not_deleted = ""
        if "chat_recoverable_message_join" in tables:
            not_deleted = "AND m.ROWID NOT IN (SELECT message_id FROM chat_recoverable_message_join)"
        rows = con.execute("""
            SELECT m.ROWID, m.date, m.text, m.attributedBody, h.id,
                   c.display_name, c.chat_identifier, m.is_read,
                   m.cache_has_attachments
            FROM message m
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            JOIN chat c ON c.ROWID = cmj.chat_id
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            WHERE m.is_from_me = 0
              AND m.associated_message_type = 0
              AND m.item_type = 0
              %s
            ORDER BY m.date DESC
            LIMIT ?""" % not_deleted, (limit * 2,)).fetchall()
    except sqlite3.DatabaseError as e:
        con.close()
        return {"ok": False, "error": "no_access", "detail": str(e)}
    con.close()
    contacts = load_contacts()
    out, seen = [], set()
    for rowid, date, text, attr, handle, group_name, chat_id, is_read, has_att in rows:
        if rowid in seen:
            continue
        seen.add(rowid)
        body = text or text_from_attributed(attr)
        body = (body or "").replace("￼", "").replace("�", "").strip()
        if not body and has_att:
            body = "Attachment"
        if not body:
            continue
        # Apple stores nanoseconds since 2001-01-01 (older DBs: seconds)
        secs = date / 1e9 if date and date > 1e12 else (date or 0)
        unix = secs + 978307200
        sender = contacts.get(norm_handle(handle), handle or "Unknown")
        out.append({
            "id": rowid,
            "from": sender,
            "group": group_name or None,
            "text": body[:400],
            "time": int(unix * 1000),
            "unread": not is_read,
        })
        if len(out) >= limit:
            break
    return {"ok": True, "messages": out}


# ---------------- opening things on the Mac ----------------
MEETING_HOSTS = ("zoom.us", "teams.microsoft.com", "teams.live.com", "meet.google.com",
                 "webex.com", "gotomeeting.com", "meet.goto.com", "whereby.com")


def mac_open(args):
    """Run macOS `open` without a shell. Returns True if it launched."""
    try:
        subprocess.Popen(["/usr/bin/open"] + args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def open_message(rowid):
    """Open Messages to the conversation that message belongs to."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % CHAT_DB, uri=True, timeout=2)
        row = con.execute("""
            SELECT c.chat_identifier, c.style, h.id
            FROM message m
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            JOIN chat c ON c.ROWID = cmj.chat_id
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            WHERE m.ROWID = ?""", (int(rowid),)).fetchone()
        con.close()
    except Exception:
        row = None
    if not row:
        return mac_open(["-a", "Messages"])
    ident, style, handle = row
    target = handle or ident or ""
    # style 43 = group chat: there's no URL for a group, so just bring Messages up
    if style == 43 or not re.fullmatch(r"[+\d][\d\s().-]{5,}|[^@\s]+@[^@\s]+\.[^@\s]+", target):
        return mac_open(["-a", "Messages"])
    scheme = "imessage:" if "@" in target else "sms:"
    return mac_open([scheme + re.sub(r"[\s().-]", "", target)])


def open_calendar_day(index, ymd):
    """Open the calendar that feed belongs to, at that day."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", ymd or "")
    cals = load_config().get("calendars", [])
    if not m or index < 0 or index >= len(cals):
        return mac_open(["-a", "Calendar"])
    y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
    host = (urlparse(cals[index]["url"].replace("webcal://", "https://")).hostname or "").lower()
    if host.endswith("calendar.google.com") or host.endswith("google.com"):
        return mac_open(["https://calendar.google.com/calendar/r/day/%s/%d/%d" % (y, mo, d)])
    if host.endswith("outlook.office365.com") or host.endswith("outlook.office.com"):
        return mac_open(["https://outlook.office.com/calendar/view/day/%s/%d/%d" % (y, mo, d)])
    if host.endswith("outlook.live.com"):
        return mac_open(["https://outlook.live.com/calendar/0/view/day/%s/%d/%d" % (y, mo, d)])
    return mac_open(["-a", "Calendar"])          # iCloud and others: Apple Calendar


def open_meeting(url):
    """Open a video-meeting link, but only for known meeting services."""
    u = urlparse(url or "")
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not any(host == h or host.endswith("." + h) for h in MEETING_HOSTS):
        return False
    return mac_open([url])


# ---------------- calendars ----------------
def fetch_ics(index):
    cfg = load_config()
    cals = cfg.get("calendars", [])
    if index < 0 or index >= len(cals):
        return None, 404
    hit = _ics_cache.get(index)
    if hit and time.time() - hit[0] < 300 and hit[2] == cals[index]["url"]:
        return hit[1], 200
    url = cals[index]["url"].strip()
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if not url.startswith("https://"):
        return None, 400
    req = urllib.request.Request(url, headers={"User-Agent": "CommandCenter/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception:
        if hit:
            return hit[1], 200
        return None, 502
    _ics_cache[index] = (time.time(), text, cals[index]["url"])
    return text, 200


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _host_ok(self):
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            self.send_error(403)
            return False
        return True

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return
        u = urlparse(self.path)
        p = u.path
        if p in ("/", "/callback", "/index.html"):
            self.path = "/index.html"
            return super().do_GET()
        if p == "/health":
            return self._json({"ok": True})
        if p == "/api/config":
            cfg = load_config()
            cals = [{"name": c.get("name", ""), "color": c.get("color", ""),
                     "hasUrl": bool(c.get("url"))} for c in cfg.get("calendars", [])]
            return self._json({"calendars": cals})
        if p == "/api/ics":
            try:
                idx = int(parse_qs(u.query).get("i", ["-1"])[0])
            except ValueError:
                idx = -1
            text, code = fetch_ics(idx)
            if text is None:
                return self._json({"error": "fetch_failed"}, code)
            data = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if p == "/api/messages":
            return self._json(recent_messages())
        if p == "/api/claude":
            try:
                with open(os.path.join(ROOT, "claude-status.json")) as f:
                    return self._json(json.load(f))
            except Exception:
                return self._json({"state": "idle"})
        if p.endswith((".py", ".json", ".log", ".command", ".plist")):
            self.send_error(404)
            return
        return super().do_GET()

    def _same_origin(self):
        """Refuse writes from other websites open in the browser (CSRF)."""
        origin = self.headers.get("Origin")
        if origin and origin.rstrip("/") not in ("http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT):
            return False
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            return False
        # A JSON content type forces a CORS preflight for cross-site requests
        return self.headers.get("Content-Type", "").split(";")[0].strip() == "application/json"

    def do_POST(self):
        if not self._host_ok():
            return
        if not self._same_origin():
            self.send_error(403)
            return
        p = urlparse(self.path).path
        if p == "/api/open":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(min(n, 10000)) or b"{}")
                kind = body.get("kind")
                if kind == "message":
                    ok = open_message(body.get("id", 0))
                elif kind == "calendar":
                    ok = open_calendar_day(int(body.get("cal", -1)), str(body.get("date", "")))
                elif kind == "meeting":
                    ok = open_meeting(str(body.get("url", "")))
                elif kind == "messages-app":
                    ok = mac_open(["-a", "Messages"])
                else:
                    ok = False
                return self._json({"ok": bool(ok)})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if p != "/api/config":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(min(n, 100000)) or b"{}")
            cals = []
            for c in body.get("calendars", [])[:12]:
                url = str(c.get("url", "")).strip()
                if not url:
                    continue
                cals.append({"name": str(c.get("name", "Calendar"))[:40],
                             "color": str(c.get("color", ""))[:9],
                             "url": url[:2000]})
            cfg = load_config()
            # keep existing URLs when the page sends "__keep__"
            old = cfg.get("calendars", [])
            for i, c in enumerate(cals):
                if c["url"] == "__keep__" and i < len(old):
                    c["url"] = old[i]["url"]
            cfg["calendars"] = [c for c in cals if c["url"] != "__keep__"]
            save_config(cfg)
            _ics_cache.clear()
            return self._json({"ok": True})
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 400)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("Command Center running at http://127.0.0.1:%d" % PORT)
    srv.serve_forever()
