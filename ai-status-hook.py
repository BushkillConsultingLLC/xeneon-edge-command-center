#!/usr/bin/env python3
"""Feeds the Command Center's AI task card from Claude Code or Codex.

Claude Code: registered as a hook (see "Connect Claude Code.command").
  It receives one JSON object on stdin per event and prints nothing.
Codex: registered as `notify` in ~/.codex/config.toml. It receives one JSON
  argument when a turn finishes.

The card is a screen on your desk, so this script never shows shell commands,
file contents or tool output: only short, plain descriptions.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS = os.path.join(HERE, "claude-status.json")
MAX_STEPS = 6


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load():
    try:
        with open(STATUS) as f:
            return json.load(f)
    except Exception:
        return {}


def save(s):
    s["updated"] = now()
    tmp = STATUS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, STATUS)


def short(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def describe_tool(name, inp):
    """A short, safe description of what a tool call is doing."""
    inp = inp if isinstance(inp, dict) else {}
    base = lambda p: os.path.basename(str(p or "")) or "a file"
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return "Editing " + base(inp.get("file_path") or inp.get("notebook_path"))
    if name == "Read":
        return "Reading " + base(inp.get("file_path"))
    if name in ("Grep", "Glob"):
        return "Searching the code"
    if name == "Bash":
        # Claude Code usually supplies a plain-English description; never show the raw command
        return short(inp.get("description") or "Running a command", 60)
    if name in ("WebSearch", "WebFetch"):
        return "Looking things up online"
    if name in ("Task", "Agent"):
        return short("Delegating: " + str(inp.get("description") or "a subtask"), 60)
    if name.startswith("mcp__"):
        parts = name.split("__")
        return "Using " + (parts[1].replace("_", " ") if len(parts) > 1 else "a connector")
    return "Using " + name


def add_step(s, label):
    steps = s.get("steps") or []
    if steps and steps[-1].get("label") == label:
        return
    for st in steps:
        if st.get("state") == "active":
            st["state"] = "done"
    steps.append({"label": label, "state": "active"})
    s["steps"] = steps[-MAX_STEPS:]


def claude_event(ev):
    name = ev.get("hook_event_name", "")
    s = load()
    project = os.path.basename(ev.get("cwd") or "") or "Claude Code"
    if name == "UserPromptSubmit":
        s = {"state": "working", "source": "Claude Code",
             "task": short(ev.get("prompt") or "Working on your request", 90),
             "detail": "In " + project, "steps": []}
    elif name == "PreToolUse":
        s["state"] = "working"
        s.setdefault("task", "Working in " + project)
        add_step(s, describe_tool(ev.get("tool_name", ""), ev.get("tool_input")))
    elif name == "Notification":
        s["state"] = "waiting"
        s["detail"] = short(ev.get("message") or "Claude Code needs your attention", 120)
    elif name == "Stop":
        s["state"] = "done"
        for st in s.get("steps") or []:
            st["state"] = "done"
        if ev.get("last_assistant_message"):
            s["detail"] = short(ev["last_assistant_message"], 140)
    elif name == "SessionEnd":
        s["state"] = "idle"
    else:
        return
    save(s)


def codex_event(payload):
    if payload.get("type") != "agent-turn-complete":
        return
    s = load()
    msgs = payload.get("input-messages") or []
    s.update({"state": "done", "source": "Codex",
              "task": short(msgs[0] if msgs else s.get("task") or "Codex finished a turn", 90),
              "detail": short(payload.get("last-assistant-message") or "", 140),
              "steps": []})
    save(s)


def main():
    try:
        if len(sys.argv) > 1 and sys.argv[-1].lstrip().startswith("{"):
            codex_event(json.loads(sys.argv[-1]))
        else:
            data = sys.stdin.read()
            if data.strip():
                claude_event(json.loads(data))
    except Exception:
        pass          # never get in the assistant's way
    sys.exit(0)


if __name__ == "__main__":
    main()
