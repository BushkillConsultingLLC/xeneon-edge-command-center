#!/bin/bash
# Connects Claude Code and/or Codex to the Command Center's AI task card.
# Safe to re-run. Backs up each config file before changing it.
cd "$(dirname "$0")"
HOOK="$(pwd)/ai-status-hook.py"
chmod +x "$HOOK"

echo ""
echo "=== Connect your AI assistant to the Command Center ==="
echo ""

/usr/bin/python3 - "$HOOK" <<'PY'
import json, os, shutil, sys, time
hook = sys.argv[1]
cmd = '/usr/bin/python3 "%s"' % hook
home = os.path.expanduser("~")

# ---- Claude Code: ~/.claude/settings.json hooks
cc_dir = os.path.join(home, ".claude")
if os.path.isdir(cc_dir) or shutil.which("claude"):
    path = os.path.join(cc_dir, "settings.json")
    os.makedirs(cc_dir, exist_ok=True)
    try:
        cfg = json.load(open(path)) if os.path.exists(path) else {}
    except Exception:
        print("Claude Code: couldn't read %s (not valid JSON). Left it alone." % path)
        cfg = None
    if cfg is not None:
        if os.path.exists(path):
            shutil.copy2(path, path + ".backup-" + time.strftime("%Y%m%d-%H%M%S"))
        hooks = cfg.setdefault("hooks", {})
        added = 0
        for event in ("UserPromptSubmit", "PreToolUse", "Notification", "Stop", "SessionEnd"):
            groups = hooks.setdefault(event, [])
            if any(h.get("command") == cmd for g in groups for h in g.get("hooks", [])):
                continue
            group = {"hooks": [{"type": "command", "command": cmd, "timeout": 5}]}
            if event == "PreToolUse":
                group["matcher"] = "*"
            groups.append(group)
            added += 1
        json.dump(cfg, open(path, "w"), indent=2)
        print("Claude Code: connected (%s). New sessions will update the card." % ("added hooks" if added else "already set up"))
else:
    print("Claude Code: not found on this Mac. Skipped.")

# ---- Codex: notify in ~/.codex/config.toml
cx_dir = os.path.join(home, ".codex")
if os.path.isdir(cx_dir) or shutil.which("codex"):
    path = os.path.join(cx_dir, "config.toml")
    os.makedirs(cx_dir, exist_ok=True)
    text = open(path).read() if os.path.exists(path) else ""
    line = 'notify = ["/usr/bin/python3", "%s"]' % hook
    if hook in text:
        print("Codex: already connected.")
    elif any(l.strip().startswith("notify") for l in text.splitlines()):
        print("Codex: you already have a notify program set in %s." % path)
        print("       Codex allows only one. To use the Command Center instead, replace it with:")
        print("       " + line)
    else:
        if text:
            shutil.copy2(path, path + ".backup-" + time.strftime("%Y%m%d-%H%M%S"))
        # top-level keys must come before any [table], so put it first
        open(path, "w").write(line + "\n" + text)
        print("Codex: connected. The card updates when Codex finishes a turn.")
else:
    print("Codex: not found on this Mac. Skipped.")
PY

echo ""
read -r -p "Press Return to close."
