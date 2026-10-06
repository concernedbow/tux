"""Opt-in background monitoring: checks, dedupe, notifications, enable/disable."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tux import journal, monitor
from tux.safety import Risk, classify

ROOT = Path(__file__).resolve().parent.parent / "plugin"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for var in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))


class FakeSystem:
    """Answers shell commands by prefix; records every call."""

    def __init__(self, **answers):
        self.answers = {"df -P -i": (0, "Filesystem Inodes IUsed IFree IUse% Mounted on\n"),
                        "df -P": (0, "Filesystem 1024-blocks Used Available Capacity Mounted on\n"),
                        "systemctl": (0, ""), "journalctl": (0, ""), "udisksctl": (0, ""),
                        "notify-send": (0, "")}
        self.answers.update({k.replace("_", " "): v for k, v in answers.items()})
        self.calls: list[str] = []

    def set(self, prefix, code, out=""):
        self.answers[prefix] = (code, out)

    def __call__(self, cmd):
        self.calls.append(cmd)
        for prefix in sorted(self.answers, key=len, reverse=True):
            if cmd.startswith(prefix):
                return self.answers[prefix]
        return 0, ""


def df(*rows):
    return (0, "Filesystem 1024-blocks Used Available Capacity Mounted on\n" + "\n".join(rows))


def run_check(system, tmp_path=None, battery=None):
    return monitor.check(system, battery_root=battery or Path("/nonexistent"))


# --- individual checks ------------------------------------------------------------------------------

def test_disk_thresholds_and_ignored_mounts():
    s = FakeSystem()
    s.set("df -P", *df("/dev/nvme0n1p2 1000 960 40 96% /", "/dev/sda1 1000 910 90 91% /data",
                       "/dev/loop3 100 100 0 100% /snap/core/123", "/dev/nvme0n1p1 100 100 0 100% /boot/efi",
                       "/dev/sdb1 1000 500 500 50% /backup"))
    s.set("df -P -i", 0, "Filesystem Inodes IUsed IFree IUse% Mounted on\n/dev/sda1 100 95 5 95% /data\n")
    found = {f.key: f for f in monitor.check_disks(s)}
    assert set(found) == {"disk:/", "disk:/data", "inodes:/data"}
    assert found["disk:/"].severity == "critical" and found["disk:/data"].severity == "warning"


def test_failed_units_ignore_desktop_app_and_crash_reporter_noise():
    s = FakeSystem()
    s.set("systemctl --failed", 0, "nginx.service loaded failed failed A high performance web server\n")
    s.set("systemctl --user --failed", 0,
          "app-\\x2fusr\\x2fshare\\x2fapport@c04fc938e16f4e6c9a9c58019f30378c.service loaded failed failed x\n"
          "drkonqi-coredump-pickup.service loaded failed failed x\n"
          "pipewire.service loaded failed failed PipeWire\n")
    keys = {f.key for f in monitor.check_units(s)}
    assert keys == {"unit:system:nginx.service", "unit:user:pipewire.service"}


KERNEL_LOG = """\
2026-10-06T01:00:00+0200 host kernel: nvme nvme0: I/O tag 64 (f040) QID 2 timeout, completion polled
2026-10-06T01:05:00+0200 host kernel: blk_update_request: I/O error, dev sda, sector 2048
2026-10-06T01:06:00+0200 host kernel: Out of memory: Killed process 4242 (firefox) total-vm:1234kB
2026-10-06T01:07:00+0200 host kernel: Out of memory: Killed process 4343 (code) total-vm:1234kB
2026-10-06T01:08:00+0200 host kernel: i915 0000:00:02.0: [drm] GPU HANG: ecode 12:1:85dffffb
2026-10-06T01:09:00+0200 host kernel: CPU0: Core temperature above threshold, cpu clock throttled
2026-10-06T01:10:00+0200 host kernel: audit: apparmor="DENIED" operation="open" profile="snap.firefox"
"""


def test_kernel_log_categories():
    s = FakeSystem()
    s.set("journalctl", 0, KERNEL_LOG)
    found, readable = monitor.check_kernel_log(s, "2026-10-06T00:00:00")
    by = {f.key: f for f in found}
    assert readable
    assert by["kernel:storage-errors"].severity == "critical"
    assert by["kernel:storage-timeouts"].severity == "warning"     # a polled timeout isn't an I/O error
    assert "firefox" in by["kernel:oom"].title and "code" in by["kernel:oom"].title
    assert {"kernel:gpu-hang", "kernel:thermal"} <= set(by)
    assert all(f.event for f in found)
    assert "--since '2026-10-06 00:00:00'" in s.calls[0]               # journalctl's accepted format


def test_unreadable_journal_is_reported_once():
    s = FakeSystem()
    s.set("journalctl", 1, "No journal files were opened due to insufficient permissions.")
    first = run_check(s)
    assert first.notes and "adm" in first.notes[0]
    assert run_check(s).notes == []


UDISKS_DUMP = """\
/org/freedesktop/UDisks2/drives/Samsung_SSD_860:
  org.freedesktop.UDisks2.Drive:
    Model:                      Samsung SSD 860
  org.freedesktop.UDisks2.Drive.Ata:
    SmartFailing:               true
/org/freedesktop/UDisks2/drives/Phison_NVMe:
  org.freedesktop.UDisks2.Drive:
    Model:                      Phison NVMe
  org.freedesktop.UDisks2.NVMe.Controller:
    SmartCriticalWarning:       spare
/org/freedesktop/UDisks2/drives/Healthy_NVMe:
  org.freedesktop.UDisks2.Drive:
    Model:                      Healthy NVMe
  org.freedesktop.UDisks2.NVMe.Controller:
    SmartCriticalWarning:
"""


def test_drive_health_from_udisks(monkeypatch):
    monkeypatch.setattr(monitor.shutil, "which", lambda b: "/usr/bin/" + b)
    s = FakeSystem()
    s.set("udisksctl dump", 0, UDISKS_DUMP)
    found = {f.key: f for f in monitor.check_drive_health(s)}
    assert set(found) == {"smart:Samsung_SSD_860", "smart:Phison_NVMe"}
    assert all(f.severity == "critical" for f in found.values())
    assert "spare" in found["smart:Phison_NVMe"].detail


def test_battery_wear(tmp_path):
    for name, full, design in (("BAT0", 40, 100), ("BAT1", 90, 100)):
        d = tmp_path / name
        d.mkdir()
        (d / "energy_full").write_text(f"{full}\n")
        (d / "energy_full_design").write_text(f"{design}\n")
    found = monitor.check_battery(tmp_path)
    assert [f.key for f in found] == ["battery:BAT0"] and "40%" in found[0].title


# --- dedupe across checks -----------------------------------------------------------------------

def test_conditions_notify_once_then_resolve():
    s = FakeSystem()
    s.set("df -P", *df("/dev/x 1000 960 40 96% /"))
    first = run_check(s)
    assert [f.key for f in first.new] == ["disk:/"]

    second = run_check(s)
    assert second.new == [] and [f.key for f in second.active] == ["disk:/"]
    assert second.active[0].first_seen == first.new[0].first_seen

    s.set("df -P", *df("/dev/x 1000 500 500 50% /"))
    third = run_check(s)
    assert [f.key for f in third.resolved] == ["disk:/"] and third.active == []
    history = (monitor.state_dir() / "history.jsonl").read_text()
    assert '"change": "new"' in history and '"change": "resolved"' in history
    assert "new" in monitor.report() and "resolved" in monitor.report()


def test_events_only_cover_new_log_lines_and_stay_visible_for_a_day():
    s = FakeSystem()
    s.set("journalctl", 0, KERNEL_LOG)
    first = run_check(s)
    assert "kernel:oom" in {f.key for f in first.new}
    assert "--since -24h" in next(c for c in s.calls if c.startswith("journalctl"))   # first run looks back a day

    s.set("journalctl", 0, "")       # nothing new since the last check
    s.calls.clear()
    second = run_check(s)
    assert second.new == [] and second.resolved == []
    assert "kernel:oom" in {f.key for f in second.active}          # still listed for the next tux session
    since = next(c for c in s.calls if c.startswith("journalctl"))
    assert monitor.load_state().last_check[:10] in since           # only reads lines after the last check


# --- notifications ----------------------------------------------------------------------------------

def test_notification_urgency_and_summary(monkeypatch):
    monkeypatch.setattr(monitor.shutil, "which", lambda b: "/usr/bin/" + b)
    s = FakeSystem()
    crit = monitor.Finding("disk:/", "critical", "/ is 97% full", "3 GB free")
    warn = monitor.Finding("unit:x", "warning", "x.service has failed")
    assert monitor.notify([crit], s)
    assert "--urgency=critical" in s.calls[-1] and "'tux: / is 97% full'" in s.calls[-1]
    assert monitor.notify([warn, crit], s)
    assert "tux found 2 new problems" in s.calls[-1]
    assert not monitor.notify([], s)


def test_no_notification_without_notify_send(monkeypatch):
    monkeypatch.setattr(monitor.shutil, "which", lambda b: None)
    s = FakeSystem()
    assert not monitor.notify([monitor.Finding("k", "warning", "t")], s) and s.calls == []


# --- enable / disable -------------------------------------------------------------------------------

def test_enable_installs_timer_and_standalone_copy():
    s = FakeSystem()
    s.set("systemctl --user is-system-running", 1, "degraded\n")   # degraded is fine
    msg = monitor.enable("12h", s)

    units = monitor.unit_dir()
    service = (units / "tux-monitor.service").read_text()
    timer = (units / "tux-monitor.timer").read_text()
    launcher = monitor.install_dir() / "tux-monitor"
    assert f"ExecStart=/usr/bin/env python3 {launcher} check" in service
    assert "OnUnitActiveSec=12h" in timer and "WantedBy=timers.target" in timer
    assert (monitor.install_dir() / "tux" / "monitor.py").exists()
    assert s.calls[-2:] == ["systemctl --user daemon-reload", "systemctl --user enable --now tux-monitor.timer"]
    assert "every 12h" in msg and "tux-monitor disable" in msg

    # the installed copy runs on its own, independent of the repo/plugin location
    out = subprocess.run([sys.executable, str(launcher), "status"], capture_output=True, text=True,
                         env={"XDG_STATE_HOME": str(monitor.state_dir().parent.parent), "PATH": "/usr/bin:/bin",
                              "HOME": str(Path.home())}, cwd="/")
    assert out.returncode == 0 and "Background monitoring" in out.stdout


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze not available")
def test_generated_units_pass_systemd_verify():
    monitor.enable("6h", FakeSystem())
    out = subprocess.run(["systemd-analyze", "--user", "verify",
                          str(monitor.unit_dir() / "tux-monitor.timer"), str(monitor.unit_dir() / "tux-monitor.service")],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_enable_rejects_bad_interval_and_missing_systemd():
    with pytest.raises(ValueError):
        monitor.enable("5m", FakeSystem())
    s = FakeSystem()
    s.set("systemctl --user is-system-running", 1, "Failed to connect to bus: No medium found")
    with pytest.raises(RuntimeError, match="cron"):
        monitor.enable("6h", s)
    assert not (monitor.unit_dir() / "tux-monitor.timer").exists()


def test_disable_removes_everything_and_purge_deletes_history():
    s = FakeSystem()
    monitor.enable("6h", s)
    run_check(FakeSystem())
    assert monitor.state_dir().exists()

    msg = monitor.disable(run=s)
    assert "systemctl --user disable --now tux-monitor.timer" in s.calls
    assert not (monitor.unit_dir() / "tux-monitor.timer").exists() and not monitor.install_dir().exists()
    assert monitor.state_dir().exists() and "off" in msg       # history kept by default

    monitor.disable(purge=True, run=s)
    assert not monitor.state_dir().exists()


# --- opt-in guarantees ------------------------------------------------------------------------------

def test_only_enable_and_disable_need_approval():
    for cmd in ("tux-monitor", "tux-monitor status", "tux-monitor report", "tux-monitor check --no-notify"):
        assert classify(cmd).risk is Risk.READ_ONLY, cmd
    for cmd in ("tux-monitor enable", "tux-monitor enable --every 1h", "tux-monitor disable"):
        assert classify(cmd).risk is Risk.CHANGE, cmd


def test_skill_cannot_be_invoked_by_the_model():
    front = (ROOT / "skills" / "monitor" / "SKILL.md").read_text().split("---")[1]
    assert "disable-model-invocation: true" in front


def test_agent_is_told_never_to_enable_on_its_own():
    prompt = (ROOT / "agents" / "tux.md").read_text()
    assert "Never run `tux-monitor enable` until the" in prompt and "wait for the answer" in prompt


def test_enable_permission_prompt_explains_what_it_installs(tmp_path):
    event = {"hook_event_name": "PreToolUse", "agent_type": "tux:tux", "tool_name": "Bash", "tool_use_id": "t",
             "tool_input": {"command": "tux-monitor enable --every 1h"}}
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "guard.py")], input=json.dumps(event),
                         capture_output=True, text=True, env={"XDG_STATE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    d = json.loads(out.stdout)["hookSpecificOutput"]
    assert d["permissionDecision"] == "ask"
    assert "systemd user timer" in d["permissionDecisionReason"] and "every 1h" in d["permissionDecisionReason"]


def test_cli_enable_requires_explicit_yes(monkeypatch):
    from tux_app import cli
    calls = []
    monkeypatch.setattr(monitor, "enable", lambda every: calls.append(every) or "on")
    monkeypatch.setattr(journal, "probe", lambda cmd: {})    # don't query the real systemd

    monkeypatch.setattr(cli.Prompt, "ask", lambda *a, **k: "n")
    cli.monitor_command(["on"])
    assert calls == []                                  # declining leaves it off

    monkeypatch.setattr(cli.Prompt, "ask", lambda *a, **k: "y")
    cli.monitor_command(["on", "--every", "1h"])
    assert calls == ["1h"]
    assert journal.Journal().entries()[-1].command == "tux-monitor enable"   # journaled like any change


# --- undo and session integration ---------------------------------------------------------------

@pytest.mark.parametrize("cmd,before,after,inverse", [
    ("tux-monitor enable", "disabled", "enabled", ["tux-monitor disable"]),
    ("tux-monitor enable --every 1h", "not-found", "enabled", ["tux-monitor disable"]),
    ("tux-monitor disable", "enabled", "not-found", ["tux-monitor enable"]),
    ("tux-monitor enable", "enabled", "enabled", []),          # already on: nothing to undo
])
def test_enable_disable_are_undoable(cmd, before, after, inverse):
    key = "svc-enabled:--user tux-monitor.timer"
    assert journal.probes(cmd).get(key) == "systemctl --user is-enabled tux-monitor.timer 2>/dev/null; true"
    got, notes, _ = journal.invert(cmd, {key: before}, {key: after})
    assert got == inverse and not notes


def test_snapshot_shows_active_findings():
    s = FakeSystem()
    s.set("df -P", *df("/dev/x 1000 960 40 96% /"))
    run_check(s)
    env = {"XDG_STATE_HOME": str(monitor.state_dir().parent.parent), "XDG_CONFIG_HOME": "/nonexistent",
           "PATH": "/usr/bin:/bin", "HOME": str(Path.home())}
    out = subprocess.run([sys.executable, str(ROOT / "bin" / "tux-snapshot")], capture_output=True, text=True,
                         env=env).stdout
    assert "Background monitor findings" in out and "/ is 96% full" in out
