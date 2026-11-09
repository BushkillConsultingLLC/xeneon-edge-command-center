#!/usr/bin/env python3
"""Command Center local server for the Xeneon Edge.

Serves the dashboard at http://127.0.0.1:8888 (only reachable from this Mac),
fetches your calendar feeds, reads recent iMessages, and serves Claude's
status file. Uses only the Python standard library.
"""
import glob
import json
import os
import plistlib
import re
import sqlite3
import subprocess
import time
import uuid
import urllib.error
import urllib.parse
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
            cols = {r[1] for r in con.execute("PRAGMA table_info(ZABCDRECORD)")}
            nick_col = "ZNICKNAME" if "ZNICKNAME" in cols else "NULL"
            for pk, first, last, org, nick in con.execute(
                    "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION, %s FROM ZABCDRECORD" % nick_col):
                full = " ".join(x.strip() for x in (first, last) if x and x.strip()) or (org or "").strip()
                if full:
                    # Messages shows a contact's nickname when the card has one; so do we
                    names[pk] = {"full": full, "nick": (nick or "").strip(),
                                 "first": (first or "").strip().split(" ")[0] if first else full}
            def keep(key, card):
                # Several cards (from different accounts) can share a number or email.
                # Prefer one with a nickname, then the shortest name, so a stray
                # duplicate card with a long or joke name doesn't win by accident.
                old = m.get(key)
                rank = lambda c: (0 if c["nick"] else 1, len(c["full"]))
                if old is None or rank(card) < rank(old):
                    m[key] = card
            for owner, num in con.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                if owner in names and num:
                    keep(norm_handle(num), names[owner])
            for owner, addr in con.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                if owner in names and addr:
                    keep(norm_handle(addr), names[owner])
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


_blocked = {"t": 0, "set": set(), "n": 0}
_mdebug = {"t": 0}


def _msg_debug(con, chat_cols):
    """Local-only troubleshooting note: which senders are recent and how Messages flags their chats."""
    if time.time() - _mdebug["t"] < 60:
        return
    _mdebug["t"] = time.time()
    try:
        flags = [c for c in ("service_name", "is_blackholed", "is_filtered", "is_archived", "is_recovered") if c in chat_cols]
        sel = ", ".join("c." + c for c in flags) or "''"
        rows = con.execute("""SELECT DISTINCT h.id, '', %s FROM message m
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID JOIN chat c ON c.ROWID = cmj.chat_id
            LEFT JOIN handle h ON h.ROWID = m.handle_id WHERE m.is_from_me = 0
            ORDER BY m.date DESC LIMIT 25""" % sel).fetchall()
        b = blocked_handles()
        with open(os.path.join(ROOT, "messages-debug.txt"), "w") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\nblock list entries: %d (short ones: %s)\nflags: %s\n"
                    % (len(b), sorted(k for k in b if len(k) < 7), flags))
            for r in rows:
                k = norm_handle(r[0])
                f.write("%s\t%s\t%s\tblocked=%s\n" % (k if len(k) < 7 else "..." + k[-4:], r[1], r[2:], k in b))
    except Exception as e:
        try:
            with open(os.path.join(ROOT, "messages-debug.txt"), "w") as f:
                f.write("debug error: %s\n" % e)
        except Exception:
            pass


def blocked_handles():
    """Numbers and emails on the Mac's block list (Messages/FaceTime/Phone share it).
    macOS keeps it in the com.apple.cmfsyncagent preferences."""
    if time.time() - _blocked["t"] < 120:
        return _blocked["set"]
    found = set()
    data = None
    try:
        r = subprocess.run(["/usr/bin/defaults", "export", "com.apple.cmfsyncagent", "-"],
                           capture_output=True, timeout=4)
        if r.returncode == 0 and r.stdout:
            data = plistlib.loads(r.stdout)
    except Exception:
        data = None
    if data is None:
        try:
            with open(os.path.expanduser("~/Library/Preferences/com.apple.cmfsyncagent.plist"), "rb") as f:
                data = plistlib.load(f)
        except Exception:
            data = None

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, str):
            t = v.strip()
            if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", t) or re.fullmatch(r"\+?[\d\s().-]{5,}", t):
                k = norm_handle(t)
                if k:
                    found.add(k)
    walk(data)
    _blocked["t"], _blocked["set"] = time.time(), found
    return found


def recent_messages(limit=12, include_hidden=False):
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
        # Blocked senders' chats are "blackholed"; spam/junk is flagged too (column names vary by macOS)
        chat_cols = {r[1] for r in con.execute("PRAGMA table_info(chat)")}
        msg_cols = {r[1] for r in con.execute("PRAGMA table_info(message)")}
        if "is_blackholed" in chat_cols:
            not_deleted += " AND COALESCE(c.is_blackholed, 0) = 0"
        if "is_filtered" in chat_cols:      # 2 = Messages put the chat in Junk
            not_deleted += " AND COALESCE(c.is_filtered, 0) != 2"
        if "is_spam" in msg_cols:
            not_deleted += " AND COALESCE(m.is_spam, 0) = 0"
        _msg_debug(con, chat_cols)
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
            LIMIT ?""" % not_deleted, (limit * 4,)).fetchall()
    except sqlite3.DatabaseError as e:
        con.close()
        return {"ok": False, "error": "no_access", "detail": str(e)}
    con.close()
    contacts = load_contacts()
    name_style = get_prefs().get("msgNames", "full")
    overrides = load_config().get("nameOverrides") or {}
    blocked = blocked_handles()
    hidden = set(load_config().get("hiddenSenders") or [])
    out, seen = [], set()
    for rowid, date, text, attr, handle, group_name, chat_id, is_read, has_att in rows:
        if rowid in seen:
            continue
        if norm_handle(handle) in blocked or (not group_name and norm_handle(chat_id) in blocked):
            continue
        if not include_hidden and norm_handle(handle) in hidden:
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
        key = norm_handle(handle)
        c = contacts.get(key)
        if overrides.get(key):
            sender = overrides[key]
        elif c:
            sender = c["nick"] or (c["first"] if name_style == "first" else c["full"])
        else:
            sender = handle or "Unknown"
        out.append({
            "id": rowid,
            "from": sender,
            "key": key,
            "card": (c["nick"] or c["full"]) if c else "",
            "group": group_name or None,
            "text": body[:400],
            "time": int(unix * 1000),
            "unread": not is_read,
            "hidden": norm_handle(handle) in hidden,
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


# ---------------- layout (which panels, which services) ----------------
PANEL_IDS = ("calendar", "messages", "notifications", "music", "ai", "reminders", "weather", "actions", "lights")
# Older versions split the Edge into 8 zones: 1-4 across the top, 5-8 across the bottom.
GRID_W, GRID_H = 8, 4
# Each panel gets a box on an 8 x 4 grid: [x, y, width, height] in cells.
DEFAULT_LAYOUT = {
    "boxes": {"calendar": [0, 0, 2, 4], "messages": [2, 0, 2, 4], "music": [4, 0, 2, 4],
              "ai": [6, 0, 2, 2], "reminders": [6, 2, 2, 2]},
    "music": "spotify",      # spotify | apple
    "ai": "claude",          # claude | openai | github | none
    "aiTask": True,          # show the "what it's working on" card
}


def clean_zones(zones):
    """8 entries; each panel must fill a rectangle of zones, else it's dropped."""
    z = [(x if x in PANEL_IDS else "") for x in (list(zones) + [""] * 8)[:8]]
    for pid in set(x for x in z if x):
        idx = [i for i, x in enumerate(z) if x == pid]
        rows = [i // 4 for i in idx]
        cols = [i % 4 for i in idx]
        area = (max(rows) - min(rows) + 1) * (max(cols) - min(cols) + 1)
        if area != len(idx):
            z = [("" if x == pid else x) for x in z]
    return z


def clean_boxes(boxes):
    """Keep boxes that are inside the grid and don't overlap an earlier one."""
    out, taken = {}, set()
    for pid in PANEL_IDS:
        b = boxes.get(pid)
        if not isinstance(b, (list, tuple)) or len(b) != 4:
            continue
        try:
            x, y, w, h = (int(v) for v in b)
        except (TypeError, ValueError):
            continue
        if w < 1 or h < 1 or x < 0 or y < 0 or x + w > GRID_W or y + h > GRID_H:
            continue
        cells = {(cx, cy) for cx in range(x, x + w) for cy in range(y, y + h)}
        if cells & taken:
            continue
        taken |= cells
        out[pid] = [x, y, w, h]
    return out


def boxes_from_zones(zones):
    """The old layout had 4 x 2 zones; each zone is 2 x 2 cells on the new grid."""
    z = clean_zones(zones)
    out = {}
    for pid in set(x for x in z if x):
        idx = [i for i, x in enumerate(z) if x == pid]
        c0, c1 = min(i % 4 for i in idx), max(i % 4 for i in idx)
        r0, r1 = min(i // 4 for i in idx), max(i // 4 for i in idx)
        out[pid] = [c0 * 2, r0 * 2, (c1 - c0 + 1) * 2, (r1 - r0 + 1) * 2]
    return clean_boxes(out)


def zones_from_panels(panels):
    """Older versions kept a list of panels side by side; turn that into zones."""
    on = [p["id"] for p in panels if isinstance(p, dict) and p.get("id") in PANEL_IDS and p.get("on", True)]
    on = list(dict.fromkeys(on))[:8]
    if not on:
        return list(DEFAULT_LAYOUT["zones"])
    cols = 4
    full = max(0, min(len(on), cols) - max(0, len(on) - cols))   # panels that get a whole column
    top, bottom = [""] * 4, [""] * 4
    i = 0
    for c in range(cols):
        if i >= len(on):
            break
        if c < full:
            top[c] = bottom[c] = on[i]; i += 1
        else:
            top[c] = on[i]; i += 1
            if i < len(on):
                bottom[c] = on[i]; i += 1
    return clean_zones(top + bottom)


DEFAULT_BADGE_APPS = ["Messages", "Microsoft Outlook", "Mail", "Slack", "Microsoft Teams", "Gmail", "LinkedIn"]
# Quick Actions: buttons on the Edge. type is app | url | shortcut | folder | system
DEFAULT_ACTIONS = [
    {"icon": "📁", "label": "Downloads", "type": "folder", "target": "~/Downloads"},
    {"icon": "📅", "label": "Calendar", "type": "app", "target": "Calendar"},
    {"icon": "🔍", "label": "Google", "type": "url", "target": "https://www.google.com"},
    {"icon": "🔒", "label": "Lock screen", "type": "system", "target": "lock"},
]
ACTION_TYPES = ("app", "url", "shortcut", "folder", "system", "light")
SYSTEM_ACTIONS = ("lock", "screensaver")


def clean_action(a):
    if not isinstance(a, dict):
        return None
    t = a.get("type")
    target = str(a.get("target", "")).strip()[:500]
    label = str(a.get("label", "")).strip()[:24]
    icon = str(a.get("icon", "")).strip()[:8]
    if t not in ACTION_TYPES or not target:
        return None
    if t == "app" and not re.fullmatch(r"[\w .&+'()-]{1,40}", target):
        return None
    if t == "url" and not re.match(r"https://[^\s]+$", target):
        return None
    if t == "shortcut" and (len(target) > 80 or re.search(r"[\r\n]", target)):
        return None
    if t == "folder":
        full = os.path.realpath(os.path.expanduser(target))
        if not (full == HOME or full.startswith(HOME + os.sep) or full.startswith("/Volumes/")):
            return None
    if t == "system" and target not in SYSTEM_ACTIONS:
        return None
    if t == "light" and len(target) > 30:
        return None
    return {"icon": icon, "label": label or target[:24], "type": t, "target": target}


# Real icons for Quick Actions: the Mac's own icon for apps and folders, the site's logo for links
ICON_CACHE = os.path.join(HOME, "Library", "Caches", "CommandCenter-icons")
ICON_JXA = r'''
ObjC.import("AppKit");
function run(argv) {
  var img = $.NSWorkspace.sharedWorkspace.iconForFile(argv[0]);
  img.setSize($.NSMakeSize(256, 256));
  var rep = $.NSBitmapImageRep.imageRepWithData(img.TIFFRepresentation);
  var png = rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $());
  return png.writeToFileAtomically(argv[1], true) ? "ok" : "fail";
}'''


def app_path(name):
    for d in APP_DIRS + ["/System/Applications/Utilities", "/Applications/Utilities"]:
        p = os.path.join(d, name + ".app")
        if os.path.exists(p):
            return p
    # close-enough names: "Outlook" finds Microsoft Outlook, "Settings" finds System Settings
    for d in APP_DIRS + ["/System/Applications/Utilities", "/Applications/Utilities"]:
        try:
            hits = sorted(f for f in os.listdir(d) if f.endswith(".app") and name.lower() in f[:-4].lower())
        except OSError:
            continue
        if hits:
            return os.path.join(d, min(hits, key=len))
    try:
        r = subprocess.run(["/usr/bin/mdfind", 'kMDItemContentType == "com.apple.application-bundle" && kMDItemDisplayName == "*%s*"c'
                            % name.replace('"', "").replace("*", "")], capture_output=True, text=True, timeout=4)
        hits = [x for x in r.stdout.splitlines() if x.endswith(".app")]
        return min(hits, key=len) if hits else None
    except Exception:
        return None


def _png_ok(path):
    try:
        with open(path, "rb") as f:
            head = f.read(8)
        return (head[:4] == b"\x89PNG" or head[:3] == b"GIF" or head[:2] == b"\xff\xd8" or head[:4] == b"\x00\x00\x01\x00"
                or head.lstrip()[:4] in (b"<svg", b"<?xm") or head[:4] == b"RIFF")
    except Exception:
        return False


def action_icon(a):
    """Path to a cached icon image for this action, or None."""
    t, target = a["type"], a["target"]
    if t not in ("app", "url", "folder"):
        return None
    os.makedirs(ICON_CACHE, exist_ok=True)
    key = re.sub(r"[^A-Za-z0-9]+", "_", t + "_" + target)[:120]
    out = os.path.join(ICON_CACHE, key + ".png")
    if os.path.exists(out) and time.time() - os.path.getmtime(out) < 7 * 86400 and _png_ok(out):
        return out
    if t in ("app", "folder"):
        src = app_path(target) if t == "app" else os.path.realpath(os.path.expanduser(target))
        if not src or not os.path.exists(src):
            return None
        try:
            subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", ICON_JXA, src, out],
                           capture_output=True, timeout=8)
            subprocess.run(["/usr/bin/sips", "-Z", "128", out], capture_output=True, timeout=8)
        except Exception:
            return None
        return out if _png_ok(out) else None
    host = urlparse(target).hostname or ""
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
        return None
    # 1) the biggest icon the page itself declares, 2) /apple-touch-icon.png, 3) Google's favicon service
    candidates = []
    try:
        req = urllib.request.Request(target, headers={"User-Agent": "Mozilla/5.0 (Macintosh) XeneonEdgeCommandCenter/1.0"})
        with urllib.request.urlopen(req, timeout=6) as r:
            base, html = r.geturl(), r.read(400_000).decode("utf-8", "replace")
        found = []
        for tag in re.findall(r"<link\b[^>]*>", html, re.I):
            rel = (re.search(r'rel\s*=\s*["\']([^"\']+)', tag, re.I) or [None, ""])[1].lower()
            href = (re.search(r'href\s*=\s*["\']([^"\']+)', tag, re.I) or [None, ""])[1]
            if not href or "icon" not in rel or "mask" in rel:
                continue
            sizes = re.search(r'sizes\s*=\s*["\'](\d+)', tag, re.I)
            score = (1000 if "apple-touch" in rel else 0) + (int(sizes.group(1)) if sizes else (500 if href.endswith(".svg") else 16))
            found.append((score, urllib.parse.urljoin(base, href.replace("&amp;", "&"))))
        candidates += [u for _, u in sorted(found, reverse=True) if u.startswith("https://")][:3]
    except Exception:
        pass
    candidates += ["https://%s/apple-touch-icon.png" % host,
                   "https://www.google.com/s2/favicons?" + urllib.parse.urlencode({"domain": host, "sz": 128})]
    for url in candidates:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) XeneonEdgeCommandCenter/1.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = r.read(1_000_000)
            if len(data) > 200:
                with open(out, "wb") as f:
                    f.write(data)
                if _png_ok(out):
                    return out
        except Exception:
            continue
    return None


def run_action(a):
    t, target = a["type"], a["target"]
    if t == "app":
        path = app_path(target)
        return mac_open([path] if path else ["-a", target])
    if t == "url":
        return mac_open([target])
    if t == "folder":
        full = os.path.realpath(os.path.expanduser(target))
        return os.path.isdir(full) and mac_open([full])
    if t == "shortcut":
        try:
            subprocess.Popen(["/usr/bin/shortcuts", "run", target], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            return True
        except Exception:
            return False
    if t == "system" and target == "lock":
        try:
            subprocess.Popen(["/usr/bin/pmset", "displaysleepnow"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False
    if t == "light":
        if target == "__all_off__":
            return lights_control("", "all-off").get("ok")
        return lights_control(target, "toggle").get("ok")
    if t == "system" and target == "screensaver":
        return mac_open(["-a", "ScreenSaverEngine"])
    return False


DEFAULT_PREFS = {"spotifyClientId": "", "speaker": "office speakers", "fullscreen": True, "spotifyReset": 0,
                 "msgNames": "full", "badges": True, "badgeApps": DEFAULT_BADGE_APPS,
                 "calSource": "links", "hiddenCals": [], "tiles": {}, "hiddenRemLists": [],
                 "weather": {"name": "", "lat": None, "lon": None, "units": "f"},
                 "actions": DEFAULT_ACTIONS, "lights": {"hidden": [], "rooms": {}}}


def get_prefs():
    p = dict(DEFAULT_PREFS)
    saved = load_config().get("prefs") or {}
    cid = str(saved.get("spotifyClientId", ""))
    if re.fullmatch(r"[A-Za-z0-9]{0,64}", cid):
        p["spotifyClientId"] = cid
    if isinstance(saved.get("speaker"), str) and saved["speaker"].strip():
        p["speaker"] = saved["speaker"].strip()[:60]
    if "fullscreen" in saved:
        p["fullscreen"] = bool(saved["fullscreen"])
    if "badges" in saved:
        p["badges"] = bool(saved["badges"])
    if isinstance(saved.get("badgeApps"), list):
        apps = [str(a).strip()[:40] for a in saved["badgeApps"] if str(a).strip()]
        p["badgeApps"] = [a for a in apps if re.fullmatch(r"[\w .&+'()-]{1,40}", a)][:12]
    if isinstance(saved.get("tiles"), dict):
        tiles = {}
        for app, t in list(saved["tiles"].items())[:20]:
            if not isinstance(t, dict):
                continue
            label = str(t.get("label", "")).strip()[:30]
            target = str(t.get("open", "")).strip()[:300]
            if target and not (target.startswith("https://") or re.fullmatch(r"[\w .&+'()-]{1,40}", target)):
                target = ""
            if label or target:
                tiles[str(app)[:40]] = {"label": label, "open": target}
        p["tiles"] = tiles
    w = saved.get("weather")
    if isinstance(w, dict):
        try:
            lat, lon = float(w.get("lat")), float(w.get("lon"))
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                p["weather"] = {"name": str(w.get("name", ""))[:60], "lat": round(lat, 4), "lon": round(lon, 4),
                                "units": "c" if w.get("units") == "c" else "f"}
        except (TypeError, ValueError):
            p["weather"] = dict(p["weather"], units="c" if w.get("units") == "c" else "f")
    lt = saved.get("lights")
    if isinstance(lt, dict):
        rooms = lt.get("rooms") if isinstance(lt.get("rooms"), dict) else {}
        p["lights"] = {"hidden": [str(x)[:80] for x in (lt.get("hidden") or [])][:100],
                       "rooms": {str(k)[:80]: str(v).strip()[:30] for k, v in list(rooms.items())[:100] if str(v).strip()}}
    if isinstance(saved.get("actions"), list):
        p["actions"] = [a for a in (clean_action(x) for x in saved["actions"][:16]) if a]
    if saved.get("calSource") in ("links", "apple", "both"):
        p["calSource"] = saved["calSource"]
    if isinstance(saved.get("hiddenRemLists"), list):
        p["hiddenRemLists"] = [str(x)[:100] for x in saved["hiddenRemLists"]][:50]
    if isinstance(saved.get("hiddenCals"), list):
        p["hiddenCals"] = [str(x)[:200] for x in saved["hiddenCals"]][:200]
    if saved.get("msgNames") in ("full", "first"):
        p["msgNames"] = saved["msgNames"]
    if isinstance(saved.get("spotifyReset"), (int, float)):
        p["spotifyReset"] = int(saved["spotifyReset"])
    return p


def get_layout():
    lay = load_config().get("layout") or {}
    out = json.loads(json.dumps(DEFAULT_LAYOUT))
    if isinstance(lay.get("boxes"), dict):
        out["boxes"] = clean_boxes(lay["boxes"])
    elif isinstance(lay.get("zones"), list):
        out["boxes"] = boxes_from_zones(lay["zones"])
    elif isinstance(lay.get("panels"), list):
        out["boxes"] = boxes_from_zones(zones_from_panels(lay["panels"]))
    if lay.get("music") in ("spotify", "apple"):
        out["music"] = lay["music"]
    if lay.get("ai") in ("claude", "openai", "github", "none"):
        out["ai"] = lay["ai"]
    if "aiTask" in lay:
        out["aiTask"] = bool(lay["aiTask"])
    return out


# ---------------- Apple Music (controlled locally, no account needed) ----------------
MUSIC_ART = os.path.join(HOME, "Library", "Caches", "CommandCenter-art")


def osa(script, timeout=4):
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return False, "", str(e)


def music_state():
    ok, out, _ = osa('application "Music" is running')
    if not ok:
        return {"ok": False, "error": "unavailable"}
    if out != "true":
        return {"ok": True, "running": False}
    script = r'''
tell application "Music"
  set st to player state as string
  if st is "stopped" then return "stopped"
  set t to current track
  set pos to player position
  set vol to sound volume
  return st & "\t" & (name of t) & "\t" & (artist of t) & "\t" & (album of t) & "\t" & (duration of t) & "\t" & pos & "\t" & vol & "\t" & (persistent ID of t)
end tell'''
    ok, out, err = osa(script)
    if not ok:
        # -1743 = the user hasn't allowed Terminal to control Music yet
        return {"ok": False, "error": "permission" if "-1743" in err else "script", "detail": err[:200]}
    if out == "stopped":
        return {"ok": True, "running": True, "state": "stopped"}
    parts = out.split("\t")
    if len(parts) < 8:
        return {"ok": True, "running": True, "state": "stopped"}
    st, name, artist, album, dur, pos, vol, pid = parts[:8]
    num = lambda s: float(s.replace(",", ".") or 0)
    return {"ok": True, "running": True, "state": st, "name": name, "artist": artist, "album": album,
            "duration_ms": int(num(dur) * 1000), "position_ms": int(num(pos) * 1000),
            "volume": int(num(vol)), "id": pid}


def music_art(track_id):
    """Write the current track's artwork to a cache file and return its path."""
    if not re.fullmatch(r"[0-9A-Fa-f]{6,32}", track_id or ""):
        return None
    os.makedirs(MUSIC_ART, exist_ok=True)
    path = os.path.join(MUSIC_ART, track_id + ".img")
    if os.path.exists(path):
        return path
    script = '''
tell application "Music"
  if (count of artworks of current track) is 0 then return "none"
  set d to raw data of artwork 1 of current track
end tell
set f to open for access (POSIX file "%s") with write permission
set eof f to 0
write d to f
close access f
return "ok"''' % path.replace('"', '')
    ok, out, _ = osa(script, timeout=6)
    return path if ok and out == "ok" and os.path.getsize(path) > 0 else None


def music_control(action):
    cmds = {"playpause": "playpause", "next": "next track", "previous": "previous track",
            "volup": "set sound volume to (sound volume + 8)", "voldown": "set sound volume to (sound volume - 8)",
            "open": None}
    if action not in cmds:
        return False
    if action == "open":
        return mac_open(["-a", "Music"])
    ok, _, _ = osa('tell application "Music" to %s' % cmds[action])
    return ok


# ---------------- Dock badges (unread counts apps show on their icons) ----------------
def _lsappinfo(args):
    try:
        r = subprocess.run(["/usr/bin/lsappinfo"] + args, capture_output=True, text=True, timeout=3)
        return r.stdout
    except Exception:
        return ""


KNOWN_BADGE_APPS = ["Messages", "Microsoft Outlook", "Mail", "Slack", "Microsoft Teams", "Gmail", "LinkedIn",
                    "WhatsApp", "Discord", "Telegram", "Signal"]
APP_DIRS = ["/Applications", "/System/Applications", os.path.join(HOME, "Applications"),
            os.path.join(HOME, "Applications", "Chrome Apps.localized")]


def app_installed(name):
    return any(os.path.exists(os.path.join(d, name + ".app")) for d in APP_DIRS)


def badge_catalog():
    """For settings: known apps plus any custom ones, with installed/selected flags."""
    chosen = get_prefs()["badgeApps"]
    names = KNOWN_BADGE_APPS + [a for a in chosen if a not in KNOWN_BADGE_APPS]
    return {"ok": True, "apps": [{"app": n, "installed": app_installed(n), "on": n in chosen,
                                  "known": n in KNOWN_BADGE_APPS} for n in names]}


DOCK_SCRIPT = r'''
set out to ""
tell application "System Events" to tell process "Dock"
  repeat with e in (UI elements of list 1)
    set lbl to ""
    try
      set v to value of attribute "AXStatusLabel" of e
      if v is not missing value then set lbl to v as text
    end try
    try
      set out to out & (name of e) & tab & lbl & linefeed
    end try
  end repeat
end tell
return out'''
_badge_debug = {"t": 0}


def dock_badges():
    """Badge labels exactly as drawn on the Dock (needs Accessibility for Terminal)."""
    ok, out, err = osa(DOCK_SCRIPT, timeout=5)
    if not ok:
        return None, err
    labels = {}
    for line in out.splitlines():
        if "\t" in line:
            name, lbl = line.split("\t", 1)
            labels[name.strip()] = lbl.strip()
    return labels, ""


_unread_memo = {}


_fallback_debug = {}


def unread_fallback(name, dock_names=()):
    """When an app shows no Dock badge, ask it another way.
    Mail: its own unread count for every inbox (asks once for Automation permission).
    Web apps like Gmail: the count in the window title, e.g. "Inbox (3) - ... - Gmail"."""
    if name == "Mail":
        ok, out, _ = osa('tell application "Mail" to return unread count of inbox', timeout=4)
        if ok and out.strip().isdigit():
            return "" if out.strip() == "0" else out.strip()
        return None
    procs = [name] + (["Google Chrome"] if name == "Gmail" else [])
    # minimized windows show up as Dock items named after the window, e.g. "Gmail - Inbox (3) - you@gmail.com - Gmail"
    out = "\n".join(n for n in dock_names if "Inbox" in n and (name == "Gmail" or name in n))
    for proc in procs:
        proc = proc.replace('"', '')
        ok, o, _ = osa('tell application "System Events" to if exists process "%s" then return name of every window of process "%s"'
                       % (proc, proc), timeout=3)
        if ok:
            out += "\n" + o
        _fallback_debug[name + "/" + proc] = (o if ok else "error")[:300]
    if name == "Gmail":
        # only personal Gmail windows ("Inbox (3) - you@gmail.com - Gmail"), not Workspace accounts
        out = "\n".join(re.findall(r"Inbox[^,\n]*?@(?:gmail|googlemail)\.com", out))
    m = re.search(r"\bInbox \((\d[\d,]*)\)", out)
    if m:
        _unread_memo[name] = (time.time(), m.group(1).replace(",", ""))
        return _unread_memo[name][1]
    if re.search(r"\bInbox\b", out):
        _unread_memo[name] = (time.time(), "")
        return ""
    # Window is showing an email, not the inbox: keep the last count for a while
    t, v = _unread_memo.get(name, (0, None))
    return v if time.time() - t < 900 else None


def badges():
    """Unread badges as drawn on the Dock. lsappinfo's StatusLabel comes back
    empty on recent macOS, so the Dock is read directly (needs Accessibility
    for Terminal); lsappinfo is only used to tell whether an app is running."""
    apps = get_prefs()["badgeApps"]
    labels, err = dock_badges()
    note = ""
    if labels is None:
        note = "dock_permission" if ("-1719" in err or "-25211" in err or "assistive" in err.lower()) else "dock_error"
    out = []
    for name in apps:
        running = bool(_lsappinfo(["find", "name=" + name]).strip()) or bool(labels and labels.get(name))
        label = (labels or {}).get(name, "") if running else ""
        if running and not label:
            alt = unread_fallback(name, list((labels or {}).keys()))
            if alt:
                label = alt
        title = (get_prefs()["tiles"].get(name) or {}).get("label") or ""
        out.append({"app": name, "running": running, "label": label, "title": title})
    if time.time() - _badge_debug["t"] > 30:
        _badge_debug["t"] = time.time()
        try:
            with open(os.path.join(ROOT, "badge-debug.txt"), "w") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + ("dock error: " + err[:300] if labels is None else "dock: " + repr(labels)[:1500])
                        + "\nresult: " + json.dumps(out) + "\nwindow checks: " + json.dumps(_fallback_debug)[:1500] + "\n")
        except Exception:
            pass
    return {"ok": True, "apps": out, "note": note}


# ---------------- Weather (Open-Meteo: free, no key, no sign-in) ----------------
_wx_cache = {"t": 0, "key": None, "data": None}


def _get_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "XeneonEdgeCommandCenter/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(2_000_000))


def weather(force=False):
    w = get_prefs()["weather"]
    if w.get("lat") is None:
        return {"ok": False, "error": "no_location"}
    key = (w["lat"], w["lon"], w["units"])
    if not force and _wx_cache["data"] and _wx_cache["key"] == key and time.time() - _wx_cache["t"] < 600:
        return _wx_cache["data"]
    q = urllib.parse.urlencode({
        "latitude": w["lat"], "longitude": w["lon"], "timezone": "auto", "forecast_days": 3,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,is_day,relative_humidity_2m",
        "hourly": "temperature_2m,precipitation_probability,weather_code,is_day",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code,sunrise,sunset",
        "temperature_unit": "celsius" if w["units"] == "c" else "fahrenheit",
        "wind_speed_unit": "kmh" if w["units"] == "c" else "mph",
    })
    try:
        d = _get_json("https://api.open-meteo.com/v1/forecast?" + q)
    except Exception as e:
        if _wx_cache["data"] and _wx_cache["key"] == key:
            return _wx_cache["data"]          # keep showing the last forecast while offline
        return {"ok": False, "error": "unreachable", "detail": str(e)[:200]}
    out = {"ok": True, "name": w["name"], "units": w["units"], "current": d.get("current", {}),
           "hourly": d.get("hourly", {}), "daily": d.get("daily", {}), "fetched": int(time.time() * 1000)}
    _wx_cache.update(t=time.time(), key=key, data=out)
    return out


def geocode(q):
    q = (q or "").strip()[:80]
    if len(q) < 2:
        return {"ok": True, "results": []}
    try:
        d = _get_json("https://geocoding-api.open-meteo.com/v1/search?" +
                      urllib.parse.urlencode({"name": q, "count": 6, "language": "en", "format": "json"}))
    except Exception:
        return {"ok": False, "results": []}
    res = []
    for r in d.get("results") or []:
        label = ", ".join(x for x in (r.get("name"), r.get("admin1"), r.get("country_code")) if x)
        res.append({"name": label, "lat": r.get("latitude"), "lon": r.get("longitude")})
    return {"ok": True, "results": res}


# ---------------- Govee lights (official Govee developer API; key from the Govee Home app) ----------------
GOVEE = os.environ.get("CC_GOVEE_API", "https://openapi.api.govee.com/router/api/v1")
_govee = {"devices": None, "dt": 0, "states": {}, "err": ""}


def _govee_call(method, path, payload=None, timeout=8):
    key = load_config().get("goveeKey")
    if not key:
        raise RuntimeError("no_key")
    data = None if payload is None else json.dumps({"requestId": str(uuid.uuid4()), "payload": payload}).encode()
    req = urllib.request.Request(GOVEE + path, data=data, method=method,
                                 headers={"Govee-API-Key": key, "Content-Type": "application/json",
                                          "User-Agent": "XeneonEdgeCommandCenter/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read(2_000_000))
    except urllib.error.HTTPError as e:
        raise RuntimeError({401: "bad_key", 403: "bad_key", 429: "rate"}.get(e.code, "http_%d" % e.code))


def govee_devices(force=False):
    if not force and _govee["devices"] is not None and time.time() - _govee["dt"] < 3600:
        return _govee["devices"]
    d = _govee_call("GET", "/user/devices")
    out = []
    for x in d.get("data") or []:
        caps = {c.get("instance") for c in x.get("capabilities") or []}
        if "powerSwitch" not in caps:
            continue                      # sensors, kettles etc.: only things that switch on and off
        out.append({"sku": x.get("sku", ""), "device": x.get("device", ""), "name": x.get("deviceName") or x.get("sku", "Light"),
                    "dim": "brightness" in caps})
    _govee["devices"], _govee["dt"] = out, time.time()
    return out


def govee_state(dev, max_age=180):
    st = _govee["states"].get(dev["device"])
    if st and time.time() - st["t"] < max_age:
        return st
    try:
        d = _govee_call("POST", "/device/state", {"sku": dev["sku"], "device": dev["device"]})
        st = {"t": time.time(), "online": None, "on": None, "bright": None}
        for c in (d.get("payload") or {}).get("capabilities") or []:
            v = (c.get("state") or {}).get("value")
            if c.get("instance") == "online":
                st["online"] = bool(v) if v != "" else None
            elif c.get("instance") == "powerSwitch" and v != "":
                st["on"] = v == 1
            elif c.get("instance") == "brightness" and isinstance(v, (int, float)):
                st["bright"] = int(v)
    except Exception:
        st = dict(st or {"online": None, "on": None, "bright": None}, t=time.time() - max_age + 30)
    _govee["states"][dev["device"]] = st
    return st


def _rooms():
    """Lights grouped into rooms: devices given the same room name in settings share one button."""
    lp = get_prefs()["lights"]
    hidden, names = set(lp["hidden"]), lp["rooms"]
    rooms = {}
    for dev in govee_devices():
        if dev["device"] in hidden:
            continue
        rooms.setdefault(names.get(dev["device"]) or dev["name"], []).append(dev)
    return rooms


def lights_view(show_all=False):
    try:
        if show_all:
            lp = get_prefs()["lights"]
            return {"ok": True, "devices": [dict(d, room=lp["rooms"].get(d["device"], ""), hidden=d["device"] in lp["hidden"])
                                            for d in govee_devices(force=True)]}
        rooms = _rooms()
    except Exception as e:
        return {"ok": False, "error": str(e)[:60]}
    # refresh stale states in parallel (Govee allows 30 state checks a minute per light)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(govee_state, [d for devs in rooms.values() for d in devs]))
    out = []
    for name, devs in rooms.items():
        sts = [govee_state(d) for d in devs]
        ons = [s["on"] for s in sts if s["on"] is not None]
        brs = [s["bright"] for s, d in zip(sts, devs) if s["bright"] is not None and s["on"]]
        out.append({"name": name, "count": len(devs), "on": (any(ons) if ons else None),
                    "bright": (round(sum(brs) / len(brs)) if brs else None), "dim": any(d["dim"] for d in devs),
                    "offline": all(s["online"] is False for s in sts)})
    return {"ok": True, "rooms": out}


def lights_control(room, action, value=None):
    try:
        rooms = _rooms()
    except Exception as e:
        return {"ok": False, "error": str(e)[:60]}
    if action == "all-off":
        targets = [d for devs in rooms.values() for d in devs]
        action = "off"
    else:
        targets = rooms.get(room) or []
    if not targets:
        return {"ok": False, "error": "unknown_room"}
    if action == "toggle":
        sts = [govee_state(d) for d in targets]
        action = "off" if any(s["on"] for s in sts) else "on"
    errs = 0
    for d in targets:
        if action in ("on", "off"):
            cap = {"type": "devices.capabilities.on_off", "instance": "powerSwitch", "value": 1 if action == "on" else 0}
        elif action == "brightness" and d["dim"]:
            cap = {"type": "devices.capabilities.range", "instance": "brightness", "value": max(1, min(100, int(value or 50)))}
        else:
            continue
        try:
            _govee_call("POST", "/device/control", {"sku": d["sku"], "device": d["device"], "capability": cap})
            st = _govee["states"].setdefault(d["device"], {"online": True, "on": None, "bright": None})
            st["t"] = time.time()            # trust what we just sent; re-check on the normal schedule
            if action == "brightness":
                st["on"], st["bright"] = True, cap["value"]
            else:
                st["on"] = action == "on"
        except Exception as e:
            errs += 1
            err = str(e)[:60]
    return {"ok": errs == 0, "error": err if errs else ""}


# ---------------- Apple Reminders (read with AppleScript, no account needed) ----------------
_rem_cache = {"t": 0, "data": None}
REM_SCRIPT = r'''
set out to ""
tell application "Reminders"
  repeat with L in lists
    set lname to name of L
    set out to out & "#LIST" & tab & lname & linefeed
    set rs to (reminders of L whose completed is false)
    repeat with r in rs
      set d to ""
      set allday to "0"
      try
        set dd to due date of r
        if dd is not missing value then set d to (dd as «class isot» as string)
        try
          if (allday due date of r) is not missing value then set allday to "1"
        end try
      end try
      set nm to name of r
      set out to out & (id of r) & tab & lname & tab & d & tab & allday & tab & (priority of r) & tab & nm & linefeed
    end repeat
  end repeat
end tell
return out'''


def reminders(force=False):
    if not force and _rem_cache["data"] and time.time() - _rem_cache["t"] < 30:
        return _rem_cache["data"]
    running = bool(_lsappinfo(["find", "name=Reminders"]).strip())
    if not running:
        # start it hidden in the background so no window pops up
        mac_open(["-g", "-j", "-a", "Reminders"])
        time.sleep(2)
    ok, out, err = osa(REM_SCRIPT, timeout=20)
    if not ok:
        data = {"ok": False, "error": "permission" if ("-1743" in err or "-1728" in err or "not allowed" in err.lower()) else "script",
                "detail": err[:200]}
    else:
        items, lists = [], []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[0] == "#LIST":
                lists.append(parts[1].strip())
                continue
            if len(parts) < 6:
                continue
            rid, lst, due, allday, prio, name = parts[0], parts[1], parts[2], parts[3], parts[4], "\t".join(parts[5:])
            items.append({"id": rid, "list": lst, "due": due, "allDay": allday == "1",
                          "priority": int(prio) if prio.isdigit() else 0, "title": name.strip()})
        data = {"ok": True, "items": items, "lists": lists}
    _rem_cache.update(t=time.time(), data=data)
    return data


def reminders_shown(force=False, show_all=False):
    """Reminders minus the lists switched off in settings (e.g. a grocery list)."""
    d = reminders(force)
    if not d.get("ok") or show_all:
        return d
    hidden = set(get_prefs()["hiddenRemLists"])
    return dict(d, items=[i for i in d["items"] if i["list"] not in hidden])


def complete_reminder(rid):
    if not re.fullmatch(r"x-apple-reminder://[A-Za-z0-9-]{8,80}", rid or ""):
        return False
    ok, _, _ = osa('tell application "Reminders" to set completed of (first reminder whose id is "%s") to true' % rid, timeout=10)
    _rem_cache["t"] = 0
    return ok


# ---------------- Apple Calendar (every calendar the Mac knows, via EventKit) ----------------
CAL_HELPER = os.path.join(ROOT, "cal-helper")
_apple_cache = {"t": 0, "data": None}


def apple_calendar(force=False, include_hidden=False):
    if not force and _apple_cache["data"] and time.time() - _apple_cache["t"] < 60:
        data = _apple_cache["data"]
    else:
        if not os.path.exists(CAL_HELPER):
            return {"ok": False, "error": "not_built"}
        try:
            # 130 s: the first run waits for you to answer the macOS permission prompt
            r = subprocess.run([CAL_HELPER, "2"], capture_output=True, text=True, timeout=130)
            data = json.loads(r.stdout or "{}")
            if "ok" not in data:
                data = {"ok": False, "error": "helper", "detail": (r.stderr or "")[:200]}
        except Exception as e:
            data = {"ok": False, "error": "helper", "detail": str(e)[:200]}
        if data.get("ok"):
            _apple_cache.update(t=time.time(), data=data)
    if not data.get("ok") or include_hidden:
        return data
    hidden = set(get_prefs()["hiddenCals"])
    return {"ok": True,
            "calendars": [c for c in data["calendars"] if c["id"] not in hidden],
            "events": [e for e in data["events"] if e["cal"] not in hidden]}


def open_apple_calendar_day(ymd):
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", ymd or "")
    if not m:
        return mac_open(["-a", "Calendar"])
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    script = """
tell application "Calendar"
  activate
  switch view to day view
  set d to current date
  set day of d to 1
  set year of d to %d
  set month of d to %d
  set day of d to %d
  view calendar at d
end tell""" % (y, mo, d)
    ok, _, _ = osa(script, timeout=8)
    return ok or mac_open(["-a", "Calendar"])


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
            return self._json({"calendars": cals, "layout": get_layout(), "prefs": get_prefs(),
                               "hasGoveeKey": bool(cfg.get("goveeKey")), "rev": int(cfg.get("rev", 0))})
        if p == "/api/music":
            return self._json(music_state())
        if p == "/api/icon":
            q = parse_qs(u.query)
            a = clean_action({"type": q.get("type", [""])[0], "target": q.get("target", [""])[0]})
            path = action_icon(a) if a else None
            if not path:
                self.send_error(404)
                return
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            ctype = ("image/png" if data[:4] == b"\x89PNG" else "image/x-icon" if data[:4] == b"\x00\x00\x01\x00"
                     else "image/jpeg" if data[:2] == b"\xff\xd8" else "image/webp" if data[:4] == b"RIFF"
                     else "image/svg+xml" if data.lstrip()[:4] in (b"<svg", b"<?xm") else "image/gif")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return
        if p == "/api/music/art":
            path = music_art(parse_qs(u.query).get("id", [""])[0])
            if not path:
                self.send_error(404)
                return
            with open(path, "rb") as f:
                data = f.read()
            ctype = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
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
        if p == "/api/apple-cal":
            q = parse_qs(u.query)
            return self._json(apple_calendar(force=q.get("force") == ["1"], include_hidden=q.get("all") == ["1"]))
        if p == "/api/badges":
            if parse_qs(u.query).get("catalog") == ["1"]:
                return self._json(badge_catalog())
            return self._json(badges())
        if p == "/api/reminders":
            q = parse_qs(u.query)
            return self._json(reminders_shown(force=q.get("force") == ["1"], show_all=q.get("all") == ["1"]))
        if p == "/api/lights":
            return self._json(lights_view(show_all=parse_qs(u.query).get("all") == ["1"]))
        if p == "/api/weather":
            return self._json(weather(force=parse_qs(u.query).get("force") == ["1"]))
        if p == "/api/geocode":
            return self._json(geocode(parse_qs(u.query).get("q", [""])[0]))
        if p == "/api/senders":
            d = recent_messages(limit=60, include_hidden=True)
            cfg = load_config()
            ov = cfg.get("nameOverrides") or {}
            hid = cfg.get("hiddenSenders") or []
            seen, out = set(), []
            for m in d.get("messages", []):
                if m["key"] and m["key"] not in seen:
                    seen.add(m["key"])
                    out.append({"key": m["key"], "card": m["card"], "override": ov.get(m["key"], ""), "hidden": m["key"] in hid})
            for k in list(ov) + list(hid):     # keep people who haven't texted lately
                if k not in seen:
                    seen.add(k)
                    out.append({"key": k, "card": "", "override": ov.get(k, ""), "hidden": k in hid})
            return self._json({"ok": d.get("ok", False), "senders": out[:20]})
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
                elif kind == "calendar" and body.get("cal") == "apple":
                    ok = open_apple_calendar_day(str(body.get("date", "")))
                elif kind == "calendar":
                    ok = open_calendar_day(int(body.get("cal", -1)), str(body.get("date", "")))
                elif kind == "meeting":
                    ok = open_meeting(str(body.get("url", "")))
                elif kind == "messages-app":
                    ok = mac_open(["-a", "Messages"])
                elif kind == "app":
                    name = str(body.get("app", ""))
                    prefs = get_prefs()
                    target = (prefs["tiles"].get(name) or {}).get("open", "")
                    if name not in prefs["badgeApps"] + ["Reminders"]:
                        ok = False
                    elif target.startswith("https://"):
                        chrome = app_installed("Google Chrome")
                        ok = mac_open((["-a", "Google Chrome"] if chrome else []) + [target])
                    else:
                        ok = mac_open(["-a", target or name])
                elif kind == "weather-app":
                    ok = mac_open(["-a", "Weather"])
                elif kind == "action":
                    acts = get_prefs()["actions"]
                    i = int(body.get("i", -1))
                    ok = 0 <= i < len(acts) and run_action(acts[i])
                elif kind == "settings":
                    # in your normal browser, on the main screen, where typing works
                    ok = mac_open(["http://127.0.0.1:%d/?settings" % PORT])
                elif kind == "restart":
                    start = os.path.join(ROOT, "Start Command Center.command")
                    try:
                        subprocess.Popen(["/bin/bash", start], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         stdin=subprocess.DEVNULL, start_new_session=True)
                        ok = True
                    except Exception:
                        ok = False
                else:
                    ok = False
                return self._json({"ok": bool(ok)})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if p == "/api/reminders":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(min(n, 2000)) or b"{}")
                return self._json({"ok": complete_reminder(str(body.get("id", "")))})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if p == "/api/lights":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(min(n, 2000)) or b"{}")
                return self._json(lights_control(str(body.get("room", "")), str(body.get("action", "")), body.get("value")))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:200]}, 400)
        if p == "/api/music":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(min(n, 1000)) or b"{}")
                return self._json({"ok": music_control(str(body.get("action", "")))})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if p != "/api/config":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(min(n, 100000)) or b"{}")
            cfg = load_config()
            if "calendars" in body:
                cals = []
                for c in body.get("calendars", [])[:12]:
                    url = str(c.get("url", "")).strip()
                    if not url:
                        continue
                    cals.append({"name": str(c.get("name", "Calendar"))[:40],
                                 "color": str(c.get("color", ""))[:9],
                                 "url": url[:2000]})
                # keep existing URLs when the page sends "__keep__"
                old = cfg.get("calendars", [])
                for i, c in enumerate(cals):
                    if c["url"] == "__keep__" and i < len(old):
                        c["url"] = old[i]["url"]
                cfg["calendars"] = [c for c in cals if c["url"] != "__keep__"]
                _ics_cache.clear()
            if isinstance(body.get("nameOverrides"), dict):
                ov = {}
                for k, v in list(body["nameOverrides"].items())[:200]:
                    k, v = str(k)[:120], str(v or "").strip()[:60]
                    if k and v:
                        ov[k] = v
                cfg["nameOverrides"] = ov
            if isinstance(body.get("goveeKey"), str) and body["goveeKey"].strip():
                k = body["goveeKey"].strip()
                if k == "__clear__":
                    cfg.pop("goveeKey", None)
                elif re.fullmatch(r"[A-Za-z0-9-]{8,80}", k):
                    cfg["goveeKey"] = k
                _govee["devices"] = None
                _govee["states"] = {}
            if isinstance(body.get("hiddenSenders"), list):
                cfg["hiddenSenders"] = sorted({norm_handle(str(k)[:120]) for k in body["hiddenSenders"][:200] if str(k).strip()} - {""})
            if isinstance(body.get("layout"), dict):
                cfg["layout"] = body["layout"]
                save_config(cfg)
                cfg["layout"] = get_layout()      # store the cleaned-up version
            if isinstance(body.get("prefs"), dict):
                np = dict(cfg.get("prefs") or {})
                for k in ("spotifyClientId", "speaker", "fullscreen", "msgNames", "badges", "badgeApps",
                          "calSource", "hiddenCals", "tiles", "weather", "actions", "hiddenRemLists", "lights"):
                    if k in body["prefs"]:
                        np[k] = body["prefs"][k]
                if body["prefs"].get("spotifyReset"):
                    np["spotifyReset"] = int(time.time())
                cfg["prefs"] = np
                save_config(cfg)
                cfg["prefs"] = get_prefs()
            cfg["rev"] = int(cfg.get("rev", 0)) + 1     # tells the Edge to pick up the change
            save_config(cfg)
            return self._json({"ok": True, "layout": get_layout(), "prefs": get_prefs(), "rev": cfg["rev"]})
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 400)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("Command Center running at http://127.0.0.1:%d" % PORT)
    srv.serve_forever()
