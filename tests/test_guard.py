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


def test_changes_are_journaled(tmp_path):
    event = {"hook_event_name": "PostToolUse", "agent_type": "tux:tux", "tool_name": "Bash",
             "tool_input": {"command": "tux-sudo touch /etc/tux-test"}, "tool_response": {}}
    subprocess.run([sys.executable, str(GUARD)], input=json.dumps(event), text=True, check=True,
                   env={"XDG_STATE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    log = (tmp_path / "tux" / "journal.jsonl").read_text()
    assert "touch /etc/tux-test" in log


# --- undo through the plugin: hook → journal → tux-undo / tux-backup ----------------------------------

BIN = GUARD.parent.parent / "bin"


def hook(event: dict, state) -> dict | None:
    out = subprocess.run([sys.executable, str(GUARD)], input=json.dumps({"agent_type": "tux:tux", **event}),
                         capture_output=True, text=True, check=True,
                         env={"XDG_STATE_HOME": str(state), "PATH": "/usr/bin:/bin"}).stdout.strip()
    return json.loads(out)["hookSpecificOutput"] if out else None


def helper(name: str, *args, state) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(BIN / name), *args], capture_output=True, text=True,
                          env={"XDG_STATE_HOME": str(state), "PATH": "/usr/bin:/bin"})


def test_edit_tool_change_can_be_undone(tmp_path):
    state, f = tmp_path / "state", tmp_path / "pipewire.conf"
    f.write_text("default.clock.quantum = 1024\n")
    edit = {"tool_name": "Edit", "tool_use_id": "toolu_1", "tool_input": {"file_path": str(f)}}

    assert hook({"hook_event_name": "PreToolUse", **edit}, state) is None   # captures, doesn't decide
    f.write_text("default.clock.quantum = 256\n")                            # Claude Code's Edit runs
    hook({"hook_event_name": "PostToolUse", **edit}, state)

    listing = helper("tux-undo", "list", state=state)
    assert f"edit {f}" in listing.stdout and "undoable" in listing.stdout
    show = helper("tux-undo", "show", state=state)
    assert "+default.clock.quantum = 1024" in show.stdout and f.read_text().endswith("256\n")

    done = helper("tux-undo", state=state)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f.read_text() == "default.clock.quantum = 1024\n"


def test_write_tool_new_file_undo_deletes_it(tmp_path):
    state, f = tmp_path / "state", tmp_path / "new.conf"
    event = {"tool_name": "Write", "tool_use_id": "toolu_2", "tool_input": {"file_path": str(f)}}
    hook({"hook_event_name": "PreToolUse", **event}, state)
    f.write_text("created by tux\n")
    hook({"hook_event_name": "PostToolUse", **event}, state)
    assert helper("tux-undo", "1", state=state).returncode == 0
    assert not f.exists()


def test_backup_then_shell_edit_then_undo(tmp_path):
    state, f = tmp_path / "state", tmp_path / "grub"
    f.write_text('GRUB_TIMEOUT=5\n')
    out = helper("tux-backup", str(f), state=state)
    assert "restore with: tux-undo 1" in out.stdout
    f.write_text('GRUB_TIMEOUT=0\n')        # e.g. tux-sudo sed -i …
    assert helper("tux-undo", "1", state=state).returncode == 0
    assert f.read_text() == 'GRUB_TIMEOUT=5\n'


def test_undo_permission_prompt_shows_the_plan(tmp_path):
    state, f = tmp_path / "state", tmp_path / "conf"
    f.write_text("a\n")
    helper("tux-backup", str(f), state=state)
    f.write_text("b\n")
    decision = hook({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "t",
                     "tool_input": {"command": "tux-undo 1"}}, state)
    assert decision["permissionDecision"] == "ask"
    assert "Restore" in decision["permissionDecisionReason"] and "+a" in decision["permissionDecisionReason"]
    # list/show are read-only and run without asking
    for cmd in ("tux-undo list", "tux-undo show 1", "tux-backup /etc/hosts"):
        d = hook({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "t",
                  "tool_input": {"command": cmd}}, state)
        assert d["permissionDecision"] == "allow", cmd


def test_tux_helpers_are_not_double_journaled(tmp_path):
    state = tmp_path / "state"
    hook({"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_use_id": "t",
          "tool_input": {"command": "tux-undo 1"}, "tool_response": {}}, state)
    assert not (state / "tux" / "journal.jsonl").exists()


def test_bash_change_records_probed_state(tmp_path):
    state = tmp_path / "state"
    cmd = "tux-sudo systemctl --user enable tux-test-nonexistent.service"
    event = {"tool_name": "Bash", "tool_use_id": "toolu_3", "tool_input": {"command": cmd}}
    assert hook({"hook_event_name": "PreToolUse", **event}, state)["permissionDecision"] == "ask"
    assert (state / "tux" / "pending" / "toolu_3.json").exists()
    hook({"hook_event_name": "PostToolUse", **event, "tool_response": {}}, state)
    assert not (state / "tux" / "pending" / "toolu_3.json").exists()
    entry = json.loads((state / "tux" / "journal.jsonl").read_text().splitlines()[-1])
    assert entry["command"] == cmd and entry["inverse"] == []   # nothing actually changed
