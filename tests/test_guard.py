import json
import subprocess
import sys
from pathlib import Path

GUARD = Path(__file__).resolve().parent.parent / "scripts" / "guard.py"


def run(event: dict) -> dict | None:
    out = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(event), capture_output=True, text=True,
                         check=True).stdout.strip()
    return json.loads(out)["hookSpecificOutput"] if out else None


def bash(cmd: str, agent: str | None = "tux:tux") -> dict | None:
    event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": cmd}}
    if agent:
        event["agent_type"] = agent
    return run(event)


def test_decisions_in_tux_session():
    assert bash("tux-scan audio network")["permissionDecision"] == "allow"
    assert bash("tux-sudo systemctl restart bluetooth")["permissionDecision"] == "ask"
    assert bash("sudo dd if=/dev/zero of=/dev/sda")["permissionDecision"] == "deny"


def test_silent_outside_tux():
    assert bash("rm -rf /", agent=None) is None
    assert bash("rm -rf /", agent="general-purpose") is None


def test_secret_reads_denied():
    r = run({"hook_event_name": "PreToolUse", "agent_type": "tux:tux", "tool_name": "Read",
             "tool_input": {"file_path": "/etc/shadow"}})
    assert r["permissionDecision"] == "deny"


def test_changes_are_logged(tmp_path, monkeypatch):
    event = {"hook_event_name": "PostToolUse", "agent_type": "tux:tux", "tool_name": "Bash",
             "tool_input": {"command": "tux-sudo apt install -y zenity"}, "tool_response": {}}
    subprocess.run([sys.executable, str(GUARD)], input=json.dumps(event), text=True, check=True,
                   env={"XDG_STATE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    log = (tmp_path / "tux" / "actions.log").read_text()
    assert "apt install" in log
