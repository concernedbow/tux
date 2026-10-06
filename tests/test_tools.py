import pytest

from tux import journal, notes, sysinfo, tools
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
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
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
    entry = journal.Journal().entries()[-1]
    assert entry.kind == "file" and entry.path == str(f)
    assert journal.Journal().backup_bytes(entry) == b"a=1\n"
    assert f"change #{entry.id}" in out


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


# --- undo through the API-mode tools ---------------------------------------------------------------

def _write(approver, path, content):
    return tools.dispatch(tools.ToolContext(approver), "write_file",
                          {"path": str(path), "content": content, "purpose": "test"})


def test_undo_change_restores_after_approval(tmp_path):
    f = tmp_path / "conf"
    f.write_text("old\n")
    _write(FakeApprover(), f, "new\n")
    approver = FakeApprover()
    ctx = tools.ToolContext(approver)

    shown, err = tools.dispatch(ctx, "undo_change", {"action": "show"})
    assert not err and "Restore" in shown and f.read_text() == "new\n"   # show changes nothing

    out, err = tools.dispatch(ctx, "undo_change", {"action": "undo"})
    assert not err and "Undid #1" in out and f.read_text() == "old\n"
    assert "Restore" in approver.commands[0]       # the user saw the plan before approving

    listing, _ = tools.dispatch(ctx, "undo_change", {"action": "list"})
    assert "undone by #2" in listing


def test_declined_undo_changes_nothing(tmp_path):
    f = tmp_path / "conf"
    f.write_text("old\n")
    _write(FakeApprover(), f, "new\n")
    out, err = tools.dispatch(tools.ToolContext(FakeApprover((False, "keep it"))), "undo_change",
                              {"action": "undo", "change_id": 1})
    assert err and "declined" in out and f.read_text() == "new\n"
    assert journal.Journal().get(1).undone_by is None


def test_read_only_mode_refuses_undo(tmp_path):
    f = tmp_path / "conf"
    f.write_text("old\n")
    _write(FakeApprover(), f, "new\n")
    approver = FakeApprover()
    out, err = tools.dispatch(tools.ToolContext(approver, read_only=True), "undo_change", {"action": "undo"})
    assert err and "read-only" in out and f.read_text() == "new\n" and approver.commands == []


def test_run_command_records_inverse_from_probed_state(tmp_path, monkeypatch):
    # a fake apt-get on PATH, so nothing is really installed; it "installs" by creating a marker
    fakebin, marker = tmp_path / "bin", tmp_path / "installed"
    fakebin.mkdir()
    (fakebin / "apt-get").write_text(f"#!/bin/sh\ntouch {marker}\necho 'Setting up demo'\n")
    (fakebin / "apt-get").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fakebin}:/usr/bin:/bin")
    prober = lambda cmd: {"pkg:apt-get:demo": marker.exists()}  # noqa: E731
    approver = FakeApprover()
    ctx = tools.ToolContext(approver, prober=prober)

    out, err = tools.dispatch(ctx, "run_command", {"command": "apt-get install -y demo", "purpose": "t"})

    assert not err and approver.commands == ["apt-get install -y demo"]
    assert "recorded as change #1" in out and "apt-get remove -y demo" in out
    assert journal.Journal().get(1).inverse == ["apt-get remove -y demo"]


def test_unknown_change_is_flagged_as_not_undoable(tmp_path):
    out, err = tools.dispatch(tools.ToolContext(FakeApprover()), "run_command",
                              {"command": f"touch {tmp_path / 'x'}", "purpose": "t"})
    assert "not automatically undoable" in out
    shown, err = tools.dispatch(tools.ToolContext(FakeApprover()), "undo_change",
                                {"action": "show", "change_id": 1})
    assert err and "no automatic inverse" in shown
