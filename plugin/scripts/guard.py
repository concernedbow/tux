#!/usr/bin/env python3
"""Claude Code hook for tux sessions.

PreToolUse (Bash): system changes always ask (even if the user has broad allow rules) and catastrophic
commands are denied outright. Read-only commands are approved automatically only if the user turned on
the plugin's "Auto-approve read-only commands" option (off by default); otherwise they go through
Claude Code's normal permission prompt. Before a change runs, the
state it could affect (packages, services, holds) is captured so it can be undone.
PreToolUse (Read): files that hold secrets are denied.
PreToolUse (Write/Edit): the file's current content is captured so the edit can be undone.
PostToolUse: the change is recorded in tux's journal (see `tux-undo list`).

Only acts inside tux (sessions started with `--agent tux:tux`, or the tux subagent). In every other
Claude Code session it stays silent, so installing the plugin doesn't change normal behaviour.
"""

import base64
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tux import journal  # noqa: E402
from tux.safety import Risk, classify, is_sensitive_path  # noqa: E402

# tux's own helpers journal themselves (or change nothing worth recording)
SELF_JOURNALED = re.compile(r"^\s*(\S*/)?tux-(undo|backup|note|scan|snapshot)\b")


def auto_approve_read_only() -> bool:
    """The user's opt-in plugin setting (exported by Claude Code), or `tux --read-only`'s launcher flag."""
    value = os.environ.get("CLAUDE_PLUGIN_OPTION_AUTO_APPROVE_READ_ONLY") or os.environ.get("TUX_AUTO_APPROVE_READ_ONLY", "")
    return value.strip().lower() in ("true", "1", "yes", "on")


def decide(decision: str, reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": decision, "permissionDecisionReason": reason}}))


def pending_path(event: dict) -> str:
    tool_use_id = re.sub(r"[^A-Za-z0-9_-]", "", str(event.get("tool_use_id", "")))
    return os.path.join(journal.state_dir(), "pending", f"{tool_use_id or 'unknown'}.json")


def save_pending(event: dict, data: dict) -> None:
    path = pending_path(event)
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        os.chmod(os.path.dirname(os.path.dirname(path)), 0o700)
        _prune_pending(os.path.dirname(path))
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def _prune_pending(folder: str, max_age: float = 86400) -> None:
    """Snapshots for tool calls the user declined never get a PostToolUse; drop them after a day."""
    import time
    cutoff = time.time() - max_age
    for name in os.listdir(folder):
        p = os.path.join(folder, name)
        try:
            if os.path.getmtime(p) < cutoff:
                os.unlink(p)
        except OSError:
            pass


def take_pending(event: dict) -> dict | None:
    path = pending_path(event)
    try:
        with open(path) as f:
            data = json.load(f)
        os.unlink(path)
        return data
    except (OSError, ValueError):
        return None


def undo_plan_reason(command: str) -> str:
    """Spell out what `tux-undo …` will do, so the approval prompt shows the real effect."""
    args = [a for a in command.split()[1:] if a != "--force"]
    j = journal.Journal()
    entry = j.resolve(args[0] if args else None)
    if entry is None:
        return "tux: undo (no matching change found)"
    plan = j.plan(entry, journal.read_local, force="--force" in command)
    return "tux: " + plan.render()[:1500]


def _interval(command: str) -> str:
    m = re.search(r"--every\s+(\S+)", command)
    return m.group(1) if m else "6h"


def pre_tool_use(event: dict, tool: str, args: dict) -> None:
    if tool == "Read":
        path = os.path.realpath(os.path.expanduser(str(args.get("file_path", ""))))
        if is_sensitive_path(path):
            decide("deny", "tux doesn't read files that hold secrets (keys, passwords, tokens). "
                           "Ask the user to check this file themselves.")
        return

    if tool in ("Write", "Edit"):
        path = os.path.abspath(os.path.expanduser(str(args.get("file_path", ""))))
        try:
            before = journal.read_local(path)
        except OSError:
            return  # unreadable: the edit will fail too
        save_pending(event, {"path": path,
                             "before": base64.b64encode(before).decode() if before is not None else None})
        return

    if tool == "Bash":
        command = str(args.get("command", ""))
        verdict = classify(command)
        if verdict.risk is Risk.BLOCKED:
            decide("deny", f"Blocked by tux safety policy: {verdict.reason}. Don't retry variants; if it's "
                           "truly needed, explain to the user exactly what to run themselves and why.")
        elif verdict.risk is Risk.READ_ONLY:
            if auto_approve_read_only():
                decide("allow", "tux: read-only inspection (auto-approved by your plugin setting)")
            # otherwise: no decision, so Claude Code's normal permission flow applies
        elif re.match(r"^\s*(\S*/)?tux-undo\b", command):
            decide("ask", undo_plan_reason(command))
        elif re.match(r"^\s*(\S*/)?tux-monitor\s+enable\b", command):
            save_pending(event, {"command": command, "before": journal.probe(command)})
            decide("ask", "tux: turn ON background monitoring? This installs a systemd user timer "
                          "(~/.config/systemd/user/tux-monitor.timer) that runs a local health check every "
                          f"{_interval(command)} and shows a desktop notification when something new goes wrong. "
                          "No AI, no network. Turn it off any time with `tux-monitor disable`.")
        else:
            if not SELF_JOURNALED.match(command):
                save_pending(event, {"command": command, "before": journal.probe(command)})
            decide("ask", f"tux: this changes your system ({verdict.reason})")


def post_tool_use(event: dict, tool: str, args: dict) -> None:
    j = journal.Journal()
    if tool == "Bash":
        command = str(args.get("command", ""))
        if SELF_JOURNALED.match(command) or classify(command).risk is Risk.READ_ONLY:
            return
        pending = take_pending(event) or {}
        response = event.get("tool_response") or {}
        code = response.get("exit_code") if isinstance(response, dict) else None
        j.record_command(command, pending.get("before") or {}, journal.probe(command), code, source="plugin")
    elif tool in ("Write", "Edit"):
        pending = take_pending(event)
        if pending is None:
            return  # without the "before" content we can't offer a safe undo
        before = base64.b64decode(pending["before"]) if pending["before"] is not None else None
        after = journal.read_local(pending["path"])
        if before != after:
            j.record_file(pending["path"], before, after,
                          summary=f"{'create' if before is None else 'edit'} {pending['path']}", source="plugin")


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    agent = event.get("agent_type") or ""
    if not (agent == "tux" or agent.startswith("tux:")):
        return
    tool = event.get("tool_name")
    args = event.get("tool_input") or {}
    if event.get("hook_event_name") == "PreToolUse":
        pre_tool_use(event, tool, args)
    elif event.get("hook_event_name") == "PostToolUse":
        post_tool_use(event, tool, args)


if __name__ == "__main__":
    main()
