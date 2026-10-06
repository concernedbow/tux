#!/usr/bin/env python3
"""Claude Code hook for tux sessions.

PreToolUse (Bash): read-only commands run without a prompt, system changes always ask (even if the
user has broad allow rules), and catastrophic commands are denied outright.
PreToolUse (Read): files that hold secrets are denied.
PostToolUse: changes that ran are appended to the tux action log.

Only acts inside tux (sessions started with `--agent tux:tux`, or the tux subagent). In every other
Claude Code session it stays silent, so installing the plugin doesn't change normal behaviour.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tux.safety import Risk, classify, is_sensitive_path  # noqa: E402


def decide(decision: str, reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": decision, "permissionDecisionReason": reason}}))


def log_change(kind: str, detail: str, outcome: str) -> None:
    import datetime as dt
    state = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "tux")
    try:
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "actions.log"), "a") as f:
            f.write(json.dumps({"time": dt.datetime.now().isoformat(timespec="seconds"), "kind": kind,
                                "detail": detail, "outcome": outcome}) + "\n")
    except OSError:
        pass


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
        if tool == "Read":
            path = os.path.realpath(os.path.expanduser(str(args.get("file_path", ""))))
            if is_sensitive_path(path):
                decide("deny", "tux doesn't read files that hold secrets (keys, passwords, tokens). "
                               "Ask the user to check this file themselves.")
            return
        if tool == "Bash":
            verdict = classify(str(args.get("command", "")))
            if verdict.risk is Risk.BLOCKED:
                decide("deny", f"Blocked by tux safety policy: {verdict.reason}. Don't retry variants; if it's "
                               "truly needed, explain to the user exactly what to run themselves and why.")
            elif verdict.risk is Risk.READ_ONLY:
                decide("allow", "tux: read-only inspection")
            else:
                decide("ask", f"tux: this changes your system ({verdict.reason})")
        return

    if event.get("hook_event_name") == "PostToolUse":
        if tool == "Bash":
            command = str(args.get("command", ""))
            if classify(command).risk is not Risk.READ_ONLY:
                response = event.get("tool_response") or {}
                outcome = "interrupted" if isinstance(response, dict) and response.get("interrupted") else "ran"
                log_change("command", command, outcome)
        elif tool in ("Write", "Edit"):
            log_change("write", str(args.get("file_path", "")), "ok")


if __name__ == "__main__":
    main()
