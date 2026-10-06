"""Opt-in background health monitoring.

`tux-monitor enable` installs a systemd user timer that runs `tux-monitor check` periodically. A check is
a handful of fast, local, read-only probes (no AI, no network). The monitor sends a desktop notification only
when something *new* goes wrong, and keeps the active findings so the next tux session can pick them up.

Nothing runs until the user enables it. `tux-monitor disable` removes everything it installed.

  tux-monitor enable [--every 1h|6h|12h|1d]   install and start the timer (default every 6h)
  tux-monitor disable [--purge]               stop and remove it (--purge also deletes history)
  tux-monitor status                          enabled?, last check, active findings
  tux-monitor check [--no-notify]             run the checks now
  tux-monitor report                          active findings and recent history
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

UNIT = "tux-monitor"
INTERVALS = {"1h": "1h", "6h": "6h", "12h": "12h", "1d": "1d"}
DEFAULT_INTERVAL = "6h"
DISK_WARN, DISK_CRIT, INODE_WARN = 90, 95, 90
BATTERY_WEAR_WARN = 0.6          # full charge below 60% of design capacity
FIRST_RUN_LOOKBACK = "-24h"      # how far back the first check reads the kernel log

Run = Callable[[str], "tuple[int, str]"]


def _run(cmd: str) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL, env={**os.environ, "LC_ALL": "C", "SYSTEMD_PAGER": ""})
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        return -1, ""


# --------------------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------------------

def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "tux" / "monitor"


def install_dir() -> Path:
    """A stable copy of tux that the timer runs, so plugin updates or uninstalling pipx don't break it."""
    return _xdg("XDG_DATA_HOME", ".local/share") / "tux" / "monitor"


def unit_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "systemd" / "user"


# --------------------------------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------------------------------

@dataclass
class Finding:
    key: str          # stable identity, e.g. "disk:/" or "unit:cups.service"
    severity: str     # "critical" | "warning"
    title: str
    detail: str = ""
    event: bool = False   # one-off events (an OOM kill) vs ongoing conditions (a full disk)
    first_seen: str = ""
    last_seen: str = ""


def check_disks(run: Run) -> list[Finding]:
    found = []
    code, out = run("df -P -x tmpfs -x devtmpfs -x squashfs -x overlay -x efivarfs -x fuse.portal")
    for line in out.splitlines()[1:] if code == 0 else []:
        parts = line.split()
        if len(parts) < 6 or not parts[4].endswith("%"):
            continue
        pct, mount = int(parts[4][:-1]), parts[5]
        if mount.startswith(("/snap/", "/run/", "/boot/efi")) and pct >= 99:
            continue   # read-only images and the EFI partition are full by design
        if pct >= DISK_WARN:
            found.append(Finding(f"disk:{mount}", "critical" if pct >= DISK_CRIT else "warning",
                                 f"{mount} is {pct}% full", f"{_human(parts[3])} free on {parts[0]}"))
    code, out = run("df -P -i -x tmpfs -x devtmpfs -x squashfs -x overlay -x efivarfs -x vfat")
    for line in out.splitlines()[1:] if code == 0 else []:
        parts = line.split()
        if len(parts) >= 6 and parts[4].endswith("%") and parts[4] != "-%" and int(parts[4][:-1]) >= INODE_WARN:
            found.append(Finding(f"inodes:{parts[5]}", "warning", f"{parts[5]} is running out of inodes",
                                 f"{parts[4]} of inodes used; usually millions of small files"))
    return found


def _human(kib: str) -> str:
    try:
        n = float(kib) * 1024
    except ValueError:
        return kib
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# Failures that are noise rather than problems: desktop-launched apps get transient per-launch units
# (app-…@<id>.service / .scope) that "fail" whenever the app exits non-zero, and crash reporters'
# helper units fail routinely.
IGNORED_UNITS = re.compile(r"^app-.*\.(service|scope)$|^(drkonqi|apport|whoopsie|kubuntu-notification)[\w@.-]*$")


def check_units(run: Run) -> list[Finding]:
    found = []
    for scope, flag in (("system", ""), ("user", "--user ")):
        code, out = run(f"systemctl {flag}--failed --plain --no-legend --no-pager")
        for line in out.splitlines() if code == 0 else []:
            unit = line.split()[0] if line.split() else ""
            if unit.endswith((".service", ".mount", ".socket", ".timer", ".path", ".device")) \
                    and not IGNORED_UNITS.match(unit):
                found.append(Finding(f"unit:{scope}:{unit}", "warning", f"{unit} has failed",
                                     f"{scope} unit; see `systemctl {flag}status {unit}`"))
    return found


# kernel-log patterns: (key, severity, title, regex)
KERNEL_EVENTS = [
    ("storage-errors", "critical", "Storage errors in the kernel log",
     re.compile(r"I/O error|nvme\S* .*(?<!timeout, )error|ata\d+.*failed command|EXT4-fs error"
                r"|BTRFS (error|critical)|XFS .*(corruption|error)|blk_update_request: .*error", re.I)),
    ("storage-timeouts", "warning", "The storage controller timed out or reset",
     re.compile(r"nvme\S* .*(timeout|resetting|reset controller)|ata\d+.*(exception|hard resetting|link is slow)",
                re.I)),
    ("hardware-errors", "critical", "Hardware errors (machine check)",
     re.compile(r"Machine check|mce: \[Hardware Error\]|EDAC .*(CE|UE)|PCIe Bus Error.*severity=(Uncorrected|Fatal)", re.I)),
    ("oom", "warning", "Programs were killed for running out of memory",
     re.compile(r"Out of memory: Killed process|oom-kill:", re.I)),
    ("gpu-hang", "warning", "The GPU hung or was reset",
     re.compile(r"GPU HANG|gpu hang|ring \S+ timeout|i915 .*Resetting|amdgpu: .*reset|NVRM: Xid", re.I)),
    ("thermal", "warning", "The CPU overheated and was throttled",
     re.compile(r"temperature above threshold|cpu clock throttled|critical temperature reached", re.I)),
]


def check_kernel_log(run: Run, since: str) -> tuple[list[Finding], bool]:
    """New kernel errors since the last check. Returns (findings, journal_readable)."""
    since = since.replace("T", " ")   # journalctl wants "YYYY-MM-DD HH:MM:SS"
    code, out = run(f"journalctl -k --no-pager -q -o short-iso --since {_shq(since)}")
    if code != 0 or "insufficient permissions" in out.lower() or "no journal files" in out.lower():
        return [], False
    found = []
    for key, severity, title, rx in KERNEL_EVENTS:
        hits = [line for line in out.splitlines() if rx.search(line)]
        if hits:
            sample = re.sub(r"^\S+ \S+ kernel: ", "", hits[-1])[:160]
            if key == "oom":
                procs = sorted({m.group(1) for line in hits
                                for m in [re.search(r"Killed process \d+ \(([^)]+)\)", line)] if m})
                if procs:
                    title = f"{title}: {', '.join(procs[:4])}"
            found.append(Finding(f"kernel:{key}", severity, title,
                                 f"{len(hits)} message{'s' if len(hits) != 1 else ''}, latest: {sample}", event=True))
    return found, True


def _shq(s: str) -> str:
    import shlex
    return shlex.quote(s)


def check_drive_health(run: Run) -> list[Finding]:
    """SMART status as reported by udisks (no root needed)."""
    if not shutil.which("udisksctl"):
        return []
    code, out = run("udisksctl dump")
    if code != 0:
        return []
    found, drive, model = [], "", ""
    for line in out.splitlines():
        if line.startswith("/org/freedesktop/UDisks2/drives/"):
            drive, model = line.rstrip(":").rsplit("/", 1)[-1], ""
        elif drive and line.strip().startswith("Model:"):
            model = line.split(":", 1)[1].strip()
        elif drive and line.strip().startswith("SmartFailing:") and line.split(":", 1)[1].strip() == "true":
            found.append(Finding(f"smart:{drive}", "critical", f"Drive {model or drive} reports it is failing",
                                 "SMART says the drive is failing. Back up your data now."))
        elif drive and line.strip().startswith("SmartCriticalWarning:") and line.split(":", 1)[1].strip():
            warn = line.split(":", 1)[1].strip()
            found.append(Finding(f"smart:{drive}", "critical", f"Drive {model or drive} has a critical warning",
                                 f"NVMe critical warning: {warn}. Back up your data."))
    return found


def check_battery(root: Path = Path("/sys/class/power_supply")) -> list[Finding]:
    found = []
    for bat in sorted(root.glob("BAT*")):
        def read(name: str) -> float | None:
            try:
                return float((bat / name).read_text().strip())
            except (OSError, ValueError):
                return None
        full = read("energy_full") or read("charge_full")
        design = read("energy_full_design") or read("charge_full_design")
        if full and design and full / design < BATTERY_WEAR_WARN:
            found.append(Finding(f"battery:{bat.name}", "warning",
                                 f"Battery holds only {full / design:.0%} of its original charge",
                                 "It's worn out; expect much shorter battery life. Replacement fixes it."))
    return found


# --------------------------------------------------------------------------------------------------
# Check, dedupe, notify
# --------------------------------------------------------------------------------------------------

@dataclass
class State:
    last_check: str = ""
    active: dict[str, dict] = field(default_factory=dict)
    journal_unreadable: bool = False


def load_state() -> State:
    try:
        data = json.loads((state_dir() / "state.json").read_text())
        return State(**{k: v for k, v in data.items() if k in State.__dataclass_fields__})
    except (OSError, ValueError, TypeError):
        return State()


def save_state(s: State) -> None:
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = d / "state.json.tmp"
    tmp.write_text(json.dumps(asdict(s), indent=1))
    os.replace(tmp, d / "state.json")


def _history(change: str, f: Finding) -> None:
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "history.jsonl", "a") as fh:
        fh.write(json.dumps({**asdict(f), "time": _now(), "change": change}) + "\n")


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


@dataclass
class CheckResult:
    new: list[Finding]
    resolved: list[Finding]
    active: list[Finding]
    notes: list[str]


def check(run: Run = _run, battery_root: Path = Path("/sys/class/power_supply")) -> CheckResult:
    state = load_state()
    now = _now()
    since = state.last_check or FIRST_RUN_LOOKBACK
    kernel, readable = check_kernel_log(run, since)
    current = check_disks(run) + check_units(run) + check_drive_health(run) + check_battery(battery_root) + kernel

    new, resolved, active = [], [], {}
    for f in current:
        prev = state.active.get(f.key)
        f.last_seen = now
        if prev and not f.event:
            f.first_seen = prev.get("first_seen", now)
        else:
            f.first_seen = now
            new.append(f)     # events are always new: they only cover messages since the last check
            _history("new", f)
        active[f.key] = asdict(f)
    for key, prev in state.active.items():
        if key not in active:
            gone = Finding(**prev)
            if gone.event:
                # events stay listed for a day so the next tux session can see them, then age out
                if _age_hours(gone.last_seen) < 24:
                    active[key] = prev
                    continue
            else:
                resolved.append(gone)
            _history("resolved" if not gone.event else "expired", gone)

    notes = []
    if not readable and not state.journal_unreadable:
        notes.append("Can't read the kernel log; add yourself to the 'adm' or 'systemd-journal' group "
                     "to monitor storage and hardware errors.")
    save_state(State(last_check=now, active=active, journal_unreadable=not readable))
    return CheckResult(new, resolved, [Finding(**v) for v in active.values()], notes)


def _age_hours(stamp: str) -> float:
    try:
        return (dt.datetime.now() - dt.datetime.fromisoformat(stamp)).total_seconds() / 3600
    except ValueError:
        return 1e9


def notify(findings: list[Finding], run: Run = _run) -> bool:
    if not findings or not shutil.which("notify-send"):
        return False
    critical = any(f.severity == "critical" for f in findings)
    if len(findings) == 1:
        summary, body = f"tux: {findings[0].title}", findings[0].detail
    else:
        summary = f"tux found {len(findings)} new problems"
        body = "\n".join(f"• {f.title}" for f in findings[:5])
    body += "\n\nRun `tux` (or `claude --agent tux:tux`) and ask about it."
    code, _ = run(f"notify-send --app-name=tux --icon=dialog-{'error' if critical else 'warning'} "
                  f"--urgency={'critical' if critical else 'normal'} {_shq(summary)} {_shq(body)}")
    return code == 0


# --------------------------------------------------------------------------------------------------
# Enable / disable (systemd user timer)
# --------------------------------------------------------------------------------------------------

def unit_files(interval: str) -> dict[str, str]:
    launcher = install_dir() / "tux-monitor"
    return {
        f"{UNIT}.service": f"""[Unit]
Description=tux background health check (opt-in; disable with: tux-monitor disable)

[Service]
Type=oneshot
ExecStart=/usr/bin/env python3 {launcher} check
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus
Nice=10
IOSchedulingClass=idle
""",
        f"{UNIT}.timer": f"""[Unit]
Description=Run the tux health check every {interval}

[Timer]
OnBootSec=10min
OnUnitActiveSec={INTERVALS[interval]}
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
""",
    }


LAUNCHER = """#!/usr/bin/env python3
# Installed by `tux-monitor enable`; removed by `tux-monitor disable`.
import os, sys
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from tux.monitor import main
main()
"""


def is_enabled(run: Run = _run) -> bool:
    code, out = run(f"systemctl --user is-enabled {UNIT}.timer")
    return code == 0 and out.strip().splitlines()[-1:] == ["enabled"]


def enable(interval: str = DEFAULT_INTERVAL, run: Run = _run) -> str:
    if interval not in INTERVALS:
        raise ValueError(f"interval must be one of {', '.join(INTERVALS)}")
    code, out = run("systemctl --user is-system-running")
    if code != 0 and out.strip() not in ("degraded", "starting", "running"):
        raise RuntimeError("systemd user services aren't available here. To run checks without systemd, add a "
                           f"cron job instead:\n  0 */6 * * * python3 {install_dir() / 'tux-monitor'} check")

    # copy tux to a stable location so plugin updates / pipx changes don't break the timer
    src = Path(__file__).resolve().parent
    dest = install_dir()
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest / "tux", ignore=shutil.ignore_patterns("__pycache__"))
    (dest / "tux-monitor").write_text(LAUNCHER)
    (dest / "tux-monitor").chmod(0o755)

    unit_dir().mkdir(parents=True, exist_ok=True)
    for name, text in unit_files(interval).items():
        (unit_dir() / name).write_text(text)
    for cmd in ("systemctl --user daemon-reload", f"systemctl --user enable --now {UNIT}.timer"):
        code, out = run(cmd)
        if code != 0:
            raise RuntimeError(f"`{cmd}` failed: {out.strip()}")
    return (f"Background monitoring is on: a health check runs every {interval}, starting within a few minutes.\n"
            f"It checks disk space, failed services, drive health, kernel storage/hardware errors, OOM kills,\n"
            f"GPU hangs, overheating and battery wear. Checks run locally: no AI, no network, no usage.\n"
            f"You'll get a desktop notification only when something new goes wrong.\n"
            f"Installed: {unit_dir() / (UNIT + '.timer')}, {unit_dir() / (UNIT + '.service')}, {dest}\n"
            f"Turn it off any time with: tux-monitor disable")


def disable(purge: bool = False, run: Run = _run) -> str:
    run(f"systemctl --user disable --now {UNIT}.timer")
    removed = []
    for name in (f"{UNIT}.timer", f"{UNIT}.service"):
        p = unit_dir() / name
        if p.exists():
            p.unlink()
            removed.append(str(p))
    if install_dir().exists():
        shutil.rmtree(install_dir())
        removed.append(str(install_dir()))
    run("systemctl --user daemon-reload")
    if purge and state_dir().exists():
        shutil.rmtree(state_dir())
        removed.append(str(state_dir()))
    return "Background monitoring is off." + (f" Removed: {', '.join(removed)}" if removed else "")


# --------------------------------------------------------------------------------------------------
# Reporting & CLI
# --------------------------------------------------------------------------------------------------

def format_findings(findings: list[Finding]) -> str:
    order = {"critical": 0, "warning": 1}
    return "\n".join(f"  [{f.severity}] {f.title}" + (f"\n      {f.detail}" if f.detail else "")
                     + f"\n      first seen {f.first_seen}"
                     for f in sorted(findings, key=lambda f: (order.get(f.severity, 2), f.key)))


def active_findings() -> list[Finding]:
    return [Finding(**v) for v in load_state().active.values()]


def status(run: Run = _run) -> str:
    state = load_state()
    lines = [f"Background monitoring: {'ON' if is_enabled(run) else 'off (turn on with: tux-monitor enable)'}",
             f"Last check: {state.last_check or 'never'}"]
    if state.journal_unreadable:
        lines.append("Note: the kernel log isn't readable, so storage/hardware errors aren't monitored.")
    findings = active_findings()
    lines.append(f"Active findings ({len(findings)}):\n{format_findings(findings)}" if findings
                 else "Active findings: none")
    return "\n".join(lines)


def report(limit: int = 15) -> str:
    out = [status()]
    try:
        hist = (state_dir() / "history.jsonl").read_text().splitlines()[-limit:]
    except OSError:
        hist = []
    if hist:
        out.append("Recent history:")
        for line in hist:
            h = json.loads(line)
            out.append(f"  {h['time']}  {h['change']:<8} [{h['severity']}] {h['title']}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "status"
    try:
        if cmd in ("-h", "--help", "help"):
            print(__doc__.strip())
        elif cmd == "enable":
            interval = args[args.index("--every") + 1] if "--every" in args else DEFAULT_INTERVAL
            print(enable(interval))
        elif cmd == "disable":
            print(disable(purge="--purge" in args))
        elif cmd == "status":
            print(status())
        elif cmd == "report":
            print(report())
        elif cmd == "check":
            result = check()
            if "--no-notify" not in args:
                notify(result.new)
            for note in result.notes:
                print(f"Note: {note}")
            print(f"{len(result.new)} new, {len(result.resolved)} resolved, {len(result.active)} active.")
            if result.active:
                print(format_findings(result.active))
        else:
            sys.exit(f"unknown command '{cmd}'\n\n{__doc__.strip()}")
    except (RuntimeError, ValueError) as e:
        sys.exit(f"tux-monitor: {e}")


if __name__ == "__main__":
    main()
