import pytest

from tux import notes, sysinfo, tools
from tux.safety import Verdict


class FakeApprover:
    def __init__(self, answer=(True, "")):
        self.answer = answer
        self.commands: list[str] = []
        self.writes: list[str] = []

    def approve_command(self, command: str, purpose: str, verdict: Verdict):
        self.commands.append(command)
        return self.answer

    def approve_write(self, path: str, diff: str, purpose: str):
        self.writes.append(diff)
        return self.answer


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tools, "ACTION_LOG", tmp_path / "state/actions.log")
    monkeypatch.setattr(tools, "BACKUP_DIR", tmp_path / "state/backups")
    monkeypatch.setattr(notes, "NOTES_FILE", tmp_path / "config/notes.md")


def test_read_only_command_runs_without_approval():
    approver = FakeApprover()
    out, err = tools.dispatch(tools.ToolContext(approver), "run_command", {"command": "echo hi", "purpose": "t"})
    assert not err and "hi" in out and approver.commands == []


def test_change_requires_approval_and_decline_is_reported(tmp_path):
    approver = FakeApprover((False, "not now"))
    target = tmp_path / "f"
    out, err = tools.dispatch(tools.ToolContext(approver), "run_command",
                              {"command": f"touch {target}", "purpose": "t"})
    assert err and "declined" in out and "not now" in out
    assert not target.exists()


def test_read_only_mode_never_changes(tmp_path):
    approver = FakeApprover()
    target = tmp_path / "f"
    out, err = tools.dispatch(tools.ToolContext(approver, read_only=True), "run_command",
                              {"command": f"touch {target}", "purpose": "t"})
    assert err and not target.exists() and approver.commands == []


def test_blocked_never_asks():
    approver = FakeApprover()
    out, err = tools.dispatch(tools.ToolContext(approver), "run_command", {"command": "rm -rf /", "purpose": "t"})
    assert err and "BLOCKED" in out and approver.commands == []


def test_timeout_kills_command():
    out, err = tools.dispatch(tools.ToolContext(FakeApprover()), "run_command",
                              {"command": "sleep 5", "purpose": "t", "timeout_seconds": 1})
    assert "timed out" in out


def test_write_file_backs_up_and_diffs(tmp_path):
    approver = FakeApprover()
    f = tmp_path / "conf"
    f.write_text("a=1\n")
    out, err = tools.dispatch(tools.ToolContext(approver), "write_file",
                              {"path": str(f), "content": "a=2\n", "purpose": "t"})
    assert not err and f.read_text() == "a=2\n"
    assert "-a=1" in approver.writes[0] and "+a=2" in approver.writes[0]
    backups = list(tools.BACKUP_DIR.iterdir())
    assert len(backups) == 1 and backups[0].read_text() == "a=1\n"


def test_read_file_refuses_secrets(tmp_path):
    out, err = tools.dispatch(tools.ToolContext(FakeApprover()), "read_file", {"path": "/etc/shadow"})
    assert err and "secrets" in out


def test_invalid_input_is_rejected():
    out, err = tools.dispatch(tools.ToolContext(FakeApprover()), "run_command", {"command": "ls"})
    assert err and "purpose" in out
    out, err = tools.dispatch(tools.ToolContext(FakeApprover()), "hardware_scan", {"area": "toaster"})
    assert err and "INVALID_INPUT" in out


def test_remember_appends_note():
    tools.dispatch(tools.ToolContext(FakeApprover()), "remember", {"note": "webcam needs ipu6 plugin"})
    assert "webcam needs ipu6 plugin" in notes.load_notes()


def test_snapshot_and_scans_run():
    snap = sysinfo.snapshot()
    assert "Kernel:" in snap
    for area in ("overview", "packages"):
        assert "###" in sysinfo.scan(area)
    assert "Unknown area" in sysinfo.scan("toaster")
