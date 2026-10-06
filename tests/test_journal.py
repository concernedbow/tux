"""Undo: the change journal, inverse commands, and safety checks."""

import pytest

from tux import journal
from tux.journal import Journal, invert, parse, probes


@pytest.fixture
def j(tmp_path):
    return Journal(root=tmp_path / "state", prober=lambda cmd: {})


def no_runner(cmd):
    raise AssertionError(f"runner should not be called, got {cmd!r}")


class FakeRunner:
    def __init__(self, fail_on: str | None = None):
        self.calls: list[str] = []
        self.fail_on = fail_on

    def __call__(self, cmd):
        self.calls.append(cmd)
        if self.fail_on and self.fail_on in cmd:
            return 1, "E: simulated failure"
        return 0, "ok"


def edit(j: Journal, path, new: str, summary="edit"):
    """Simulate tux editing a file: journal it, then write it."""
    before = path.read_bytes() if path.exists() else None
    path.write_text(new)
    return j.record_file(str(path), before, new.encode(), summary=summary)


# --- file changes ---------------------------------------------------------------------------------

def test_undo_restores_previous_content(j, tmp_path):
    f = tmp_path / "grub"
    f.write_text("TIMEOUT=5\n")
    e = edit(j, f, "TIMEOUT=0\n")

    result = j.undo(e.id, no_runner)

    assert result.ok, result.message
    assert f.read_text() == "TIMEOUT=5\n"
    assert j.get(e.id).undone_by == result.new.id
    assert result.new.undoes == e.id and result.new.source == "undo"


def test_undo_keeps_file_mode(j, tmp_path):
    f = tmp_path / "script.sh"
    f.write_text("echo old\n")
    f.chmod(0o750)
    e = edit(j, f, "echo new\n")
    assert j.undo(e.id, no_runner).ok
    assert f.stat().st_mode & 0o777 == 0o750


def test_undo_deletes_file_tux_created(j, tmp_path):
    f = tmp_path / "99-tux.conf"
    e = edit(j, f, "options snd_hda_intel power_save=0\n")
    assert e.backup is None
    plan = j.plan(e)
    assert "Delete" in plan.description
    assert j.undo(e.id, no_runner).ok
    assert not f.exists()


def test_cannot_undo_twice(j, tmp_path):
    f = tmp_path / "conf"
    f.write_text("a\n")
    e = edit(j, f, "b\n")
    first = j.undo(e.id, no_runner)
    second = j.undo(e.id, no_runner)
    assert not second.ok and f"already undone by #{first.new.id}" in second.message


def test_refuses_when_file_modified_since_unless_forced(j, tmp_path):
    f = tmp_path / "conf"
    f.write_text("original\n")
    e = edit(j, f, "tux\n")
    f.write_text("user edited after tux\n")

    refused = j.undo(e.id, no_runner)
    assert not refused.ok and "modified since" in refused.message
    assert f.read_text() == "user edited after tux\n"
    assert "+original" in j.plan(e).diff   # the plan shows what forcing would do

    forced = j.undo(e.id, no_runner, force=True)
    assert forced.ok and f.read_text() == "original\n"


def test_older_change_blocked_until_newer_undone(j, tmp_path):
    f = tmp_path / "conf"
    f.write_text("v1\n")
    first = edit(j, f, "v2\n")
    second = edit(j, f, "v3\n")

    blocked = j.undo(first.id, no_runner)
    assert not blocked.ok and f"#{second.id}" in blocked.message and f.read_text() == "v3\n"

    assert j.undo(second.id, no_runner).ok and f.read_text() == "v2\n"
    assert j.undo(first.id, no_runner).ok and f.read_text() == "v1\n"


def test_undo_last_walks_back_through_history(j, tmp_path):
    f = tmp_path / "conf"
    f.write_text("v1\n")
    edit(j, f, "v2\n")
    edit(j, f, "v3\n")
    assert j.undo("last", no_runner).ok and f.read_text() == "v2\n"
    # the undo record itself is skipped, so "last" keeps going back instead of redoing
    assert j.undo("last", no_runner).ok and f.read_text() == "v1\n"
    assert j.undo("last", no_runner).message == "Nothing to undo."


def test_undo_of_undo_reapplies_change(j, tmp_path):
    f = tmp_path / "conf"
    f.write_text("before\n")
    e = edit(j, f, "after\n")
    undo = j.undo(e.id, no_runner)

    redo = j.undo(undo.new.id, no_runner)

    assert redo.ok and f.read_text() == "after\n"
    assert j.get(undo.new.id).undone_by == redo.new.id
    assert j.get(e.id).undone_by is None      # the original change is in effect again
    assert j.undo(e.id, no_runner).ok and f.read_text() == "before\n"


def test_snapshot_without_after_hash_restores_whatever_changed(j, tmp_path):
    """tux-backup snapshots before a shell edit (sed -i); the new content isn't known up front."""
    f = tmp_path / "fstab"
    f.write_text("UUID=1 / ext4 defaults 0 1\n")
    e = j.record_file(str(f), f.read_bytes(), None, summary="snapshot")
    f.write_text("UUID=1 / ext4 defaults,noatime 0 1\n")   # edited by a command
    plan = j.plan(e)
    assert not plan.blocked and "-UUID=1 / ext4 defaults,noatime 0 1" in plan.diff
    assert j.undo(e.id, no_runner).ok and "noatime" not in f.read_text()


def test_restore_that_matches_is_a_noop(j, tmp_path):
    f = tmp_path / "conf"
    f.write_text("same\n")
    e = j.record_file(str(f), b"same\n", None)
    plan = j.plan(e)
    assert plan.noop and "already matches" in plan.description


def test_file_needing_root_is_restored_through_runner(j, tmp_path, monkeypatch):
    f = tmp_path / "root-owned"
    f.write_text("old\n")
    e = edit(j, f, "new\n")

    def deny(*a, **k):
        raise PermissionError("read-only")
    real_open = open
    monkeypatch.setattr("builtins.open", lambda p, mode="r", *a, **k:
                        deny() if str(p) == str(f) and "b" in mode and ("+" in mode or "w" in mode)
                        else real_open(p, mode, *a, **k))

    def runner(cmd):
        runner.cmd = cmd
        assert cmd.startswith("sudo tee ") and str(f) in cmd
        src = cmd.split("<")[1].split(">")[0].strip().strip("'")
        f.write_bytes(real_open(src, "rb").read())   # what root's tee would do
        return 0, ""
    result = j.undo(e.id, runner)
    assert result.ok and f.read_text() == "old\n" and runner.cmd


def test_regenerate_commands_rerun_after_config_undo(j, tmp_path):
    f = tmp_path / "grub"
    f.write_text("GRUB_CMDLINE_LINUX_DEFAULT=\"quiet splash\"\n")
    e = edit(j, f, "GRUB_CMDLINE_LINUX_DEFAULT=\"quiet splash i915.enable_psr=0\"\n")
    regen = j.record_command("sudo update-grub", {}, {}, 0)
    assert regen.regenerate and not regen.inverse

    assert "only rebuilds files from config" in j.plan(regen).blocked
    plan = j.plan(e)
    assert plan.follow_up == ["sudo update-grub"] and plan.warnings

    runner = FakeRunner()
    assert j.undo(e.id, runner).ok
    assert runner.calls == ["sudo update-grub"]


# --- commands -------------------------------------------------------------------------------------

def test_command_undo_runs_inverse_and_journals_it(j):
    e = j.record_command("sudo apt install -y vainfo", {"pkg:apt:vainfo": False}, {"pkg:apt:vainfo": True}, 0)
    assert e.inverse == ["sudo apt-get remove -y vainfo"]
    runner = FakeRunner()

    result = j.undo(e.id, runner)

    assert result.ok and runner.calls == ["sudo apt-get remove -y vainfo"]
    assert result.new.command == "sudo apt-get remove -y vainfo" and result.new.undoes == e.id
    assert j.get(e.id).undone_by == result.new.id


def test_failed_inverse_leaves_change_marked_active(j):
    e = j.record_command("sudo apt install -y vainfo", {"pkg:apt:vainfo": False}, {"pkg:apt:vainfo": True}, 0)
    result = j.undo(e.id, FakeRunner(fail_on="remove"))
    assert not result.ok and "failed" in result.message
    assert j.get(e.id).undone_by is None


def test_non_invertible_command_is_explained_not_run(j):
    e = j.record_command("sudo ./install-driver.sh --force", {}, {}, 0)
    result = j.undo(e.id, no_runner)
    assert not result.ok and "no automatic inverse" in result.message


@pytest.mark.parametrize("command,before,after,inverse,note", [
    # only packages that were actually newly installed get removed
    ("sudo apt install -y vim curl", {"pkg:apt:vim": True, "pkg:apt:curl": False},
     {"pkg:apt:vim": True, "pkg:apt:curl": True}, ["sudo apt-get remove -y curl"], "vim was already installed"),
    ("sudo apt-get install -y nothing-new", {"pkg:apt-get:nothing-new": True},
     {"pkg:apt-get:nothing-new": True}, [], "already installed"),
    # a failed install changes nothing, so there's nothing to undo
    ("sudo apt install -y typo-pkg", {"pkg:apt:typo-pkg": False}, {"pkg:apt:typo-pkg": False}, [], None),
    ("sudo apt purge -y tlp", {"pkg:apt:tlp": True}, {"pkg:apt:tlp": False},
     ["sudo apt-get install -y tlp"], "config files"),
    ("sudo apt update && sudo apt install -y zenity", {"pkg:apt:zenity": False}, {"pkg:apt:zenity": True},
     ["sudo apt-get remove -y zenity"], None),
    ("sudo dnf install -y htop", {"pkg:dnf:htop": False}, {"pkg:dnf:htop": True},
     ["sudo dnf remove -y htop"], None),
    ("sudo pacman -S --noconfirm htop", {"pkg:pacman:htop": False}, {"pkg:pacman:htop": True},
     ["sudo pacman -R --noconfirm htop"], None),
    ("sudo snap remove spotify", {"pkg:snap:spotify": True}, {"pkg:snap:spotify": False},
     ["sudo snap install spotify"], None),
    ("sudo apt-mark hold linux-firmware", {"hold:linux-firmware": False}, {"hold:linux-firmware": True},
     ["sudo apt-mark unhold linux-firmware"], None),
    # services: reverse only what changed
    ("sudo systemctl enable --now tlp", {"svc-enabled:tlp": "disabled", "svc-active:tlp": "inactive"},
     {"svc-enabled:tlp": "enabled", "svc-active:tlp": "active"},
     ["sudo systemctl disable tlp", "sudo systemctl stop tlp"], None),
    ("sudo systemctl enable bluetooth", {"svc-enabled:bluetooth": "enabled", "svc-active:bluetooth": "active"},
     {"svc-enabled:bluetooth": "enabled", "svc-active:bluetooth": "active"}, [], None),
    ("sudo systemctl mask power-profiles-daemon",
     {"svc-enabled:power-profiles-daemon": "enabled", "svc-active:power-profiles-daemon": "active"},
     {"svc-enabled:power-profiles-daemon": "masked", "svc-active:power-profiles-daemon": "active"},
     ["sudo systemctl unmask power-profiles-daemon", "sudo systemctl enable power-profiles-daemon"], None),
    ("systemctl --user stop pipewire", {"svc-active:--user pipewire": "active"},
     {"svc-active:--user pipewire": "inactive"}, ["systemctl --user start pipewire"], None),
    ("sudo systemctl restart NetworkManager", {}, {}, [], None),
    # tux-sudo is treated like sudo
    ("tux-sudo apt install -y zenity", {"pkg:apt:zenity": False}, {"pkg:apt:zenity": True},
     ["sudo apt-get remove -y zenity"], None),
    # things tux can't reverse are reported, never guessed
    ("curl -s https://x.y/a | sudo tee /etc/a", {}, {}, [], "pipelines"),
    ("sudo modprobe -r uvcvideo && sudo rm /etc/modprobe.d/x.conf", {}, {}, [], "has no automatic inverse"),
])
def test_invert(command, before, after, inverse, note):
    got, notes, _ = invert(command, before, after)
    assert got == inverse
    if note:
        assert any(note in n for n in notes), notes
    else:
        assert not notes, notes


def test_compound_command_undone_in_reverse_order():
    cmd = "sudo apt install -y tlp && sudo systemctl enable tlp"
    before = {"pkg:apt:tlp": False, "svc-enabled:tlp": "not-found", "svc-active:tlp": "inactive"}
    after = {"pkg:apt:tlp": True, "svc-enabled:tlp": "enabled", "svc-active:tlp": "inactive"}
    got, _, _ = invert(cmd, before, after)
    # not-found -> enabled isn't a plain disable/enable pair; removing the package undoes it
    assert got[-1] == "sudo apt-get remove -y tlp"


def test_probes_query_state_without_changing_it():
    p = probes("sudo apt install -y vim && sudo systemctl enable --now tlp && sudo apt-mark hold mesa")
    assert set(p) == {"pkg:apt:vim", "svc-enabled:tlp", "svc-active:tlp", "hold:mesa"}
    assert all(not q.startswith("sudo") for q in p.values())
    seen = []
    state = journal.probe("sudo systemctl enable tlp", run=lambda c: (seen.append(c) or (0, "enabled")))
    assert state == {"svc-enabled:tlp": "enabled", "svc-active:tlp": "enabled"} and len(seen) == 2


def test_parse_handles_sudo_flags():
    a = parse("sudo -E -u root apt-get install -y foo")
    assert a.kind == "pkg" and a.items == ["foo"] and a.root


def test_backups_and_journal_are_private(tmp_path):
    """Backups can contain copies of root-only files (e.g. Wi-Fi passwords), so only the owner may read them."""
    import stat
    j = Journal(root=tmp_path / "state", prober=lambda c: {})
    f = tmp_path / "wifi.nmconnection"
    f.write_text("psk=hunter2\n")
    e = edit(j, f, "psk=changed\n")
    mode = lambda p: stat.S_IMODE(p.stat().st_mode)  # noqa: E731
    assert mode(j.root) == 0o700 and mode(j.backups) == 0o700
    assert mode(j.backups / e.backup) == 0o600 and mode(j.file) == 0o600
