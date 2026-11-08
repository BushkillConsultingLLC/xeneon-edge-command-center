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
    name_style = get_prefs().get("msgNames", "full")
    overrides = load_config().get("nameOverrides") or {}
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
PANEL_IDS = ("calendar", "messages", "notifications", "music", "ai", "reminders")
# The Edge is split into 8 zones: 1-4 across the top, 5-8 across the bottom.
# A panel may take several zones as long as they form a rectangle.
DEFAULT_LAYOUT = {
    "zones": ["calendar", "messages", "music", "ai",
              "calendar", "messages", "music", "reminders"],
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
DEFAULT_PREFS = {"spotifyClientId": "", "speaker": "office speakers", "fullscreen": True, "spotifyReset": 0,
                 "msgNames": "full", "badges": True, "badgeApps": DEFAULT_BADGE_APPS,
                 "calSource": "links", "hiddenCals": [], "tiles": {}}


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
    if saved.get("calSource") in ("links", "apple", "both"):
        p["calSource"] = saved["calSource"]
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
    if isinstance(lay.get("zones"), list):
        out["zones"] = clean_zones(lay["zones"])
    elif isinstance(lay.get("panels"), list):
        out["zones"] = zones_from_panels(lay["panels"])
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


def unread_fallback(name):
    """When an app shows no Dock badge, ask it another way.
    Mail: its own unread count for every inbox (asks once for Automation permission).
    Web apps like Gmail: the count in the window title, e.g. "Inbox (3) - ... - Gmail"."""
    if name == "Mail":
        ok, out, _ = osa('tell application "Mail" to return unread count of inbox', timeout=4)
        if ok and out.strip().isdigit():
            return "" if out.strip() == "0" else out.strip()
        return None
    ok, out, _ = osa('tell application "System Events" to if exists process "%s" then return name of every window of process "%s"'
                     % (name.replace('"', ''), name.replace('"', '')), timeout=3)
    if not ok:
        return None
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
            alt = unread_fallback(name)
            if alt:
                label = alt
        title = (get_prefs()["tiles"].get(name) or {}).get("label") or ""
        out.append({"app": name, "running": running, "label": label, "title": title})
    if time.time() - _badge_debug["t"] > 30:
        _badge_debug["t"] = time.time()
        try:
            with open(os.path.join(ROOT, "badge-debug.txt"), "w") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + ("dock error: " + err[:300] if labels is None else "dock: " + repr(labels)[:1500])
                        + "\nresult: " + json.dumps(out) + "\n")
        except Exception:
            pass
    return {"ok": True, "apps": out, "note": note}


# ---------------- Apple Reminders (read with AppleScript, no account needed) ----------------
_rem_cache = {"t": 0, "data": None}
REM_SCRIPT = r'''
set out to ""
tell application "Reminders"
  repeat with L in lists
    set lname to name of L
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
        items = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 6:
                continue
            rid, lst, due, allday, prio, name = parts[0], parts[1], parts[2], parts[3], parts[4], "\t".join(parts[5:])
            items.append({"id": rid, "list": lst, "due": due, "allDay": allday == "1",
                          "priority": int(prio) if prio.isdigit() else 0, "title": name.strip()})
        data = {"ok": True, "items": items}
    _rem_cache.update(t=time.time(), data=data)
    return data


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
                               "rev": int(cfg.get("rev", 0))})
        if p == "/api/music":
            return self._json(music_state())
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
            return self._json(reminders(force=parse_qs(u.query).get("force") == ["1"]))
        if p == "/api/senders":
            d = recent_messages(limit=60)
            ov = load_config().get("nameOverrides") or {}
            seen, out = set(), []
            for m in d.get("messages", []):
                if m["key"] and m["key"] not in seen:
                    seen.add(m["key"])
                    out.append({"key": m["key"], "card": m["card"], "override": ov.get(m["key"], "")})
            for k, v in ov.items():            # keep overrides for people who haven't texted lately
                if k not in seen:
                    out.append({"key": k, "card": "", "override": v})
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
            if isinstance(body.get("layout"), dict):
                cfg["layout"] = body["layout"]
                save_config(cfg)
                cfg["layout"] = get_layout()      # store the cleaned-up version
            if isinstance(body.get("prefs"), dict):
                np = dict(cfg.get("prefs") or {})
                for k in ("spotifyClientId", "speaker", "fullscreen", "msgNames", "badges", "badgeApps",
                          "calSource", "hiddenCals", "tiles"):
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
