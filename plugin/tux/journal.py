"""Change journal and undo.

Every change tux makes is recorded with what's needed to reverse it:
  - file changes keep a backup of the previous content (or note the file didn't exist) plus a hash of
    the content tux wrote, so undo can tell whether the file was modified afterwards;
  - commands that install/remove packages, enable/start services or hold packages are probed before and
    after they run, and the inverse is derived from what actually changed (undoing `apt install vim`
    won't remove vim if it was already installed).

Undoing a change is itself journaled, so an undo can be undone.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import difflib
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

# runner(shell_command) -> (exit_code, output). Commands that need root start with "sudo "; each
# front end decides how to satisfy that (terminal sudo, or a graphical askpass in Claude Code).
Runner = Callable[[str], "tuple[int, str]"]


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "tux"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------------------------------
# Entries and storage
# --------------------------------------------------------------------------------------------------

@dataclass
class Entry:
    id: int
    time: str
    kind: str                          # "file" | "command"
    summary: str
    source: str = "tux"                # "api" | "plugin" | "undo"
    # file entries
    path: str | None = None
    backup: str | None = None          # backup file name; None = the file didn't exist before
    before_hash: str | None = None
    after_hash: str | None = None      # None = unknown (snapshot taken before an edit made by a command)
    # command entries
    command: str | None = None
    exit_code: int | None = None
    inverse: list[str] = field(default_factory=list)   # commands that reverse it, in order
    notes: list[str] = field(default_factory=list)     # why parts can't be (or needn't be) undone
    regenerate: bool = False           # rebuilds output from config (update-grub, update-initramfs…)
    # undo bookkeeping
    undoes: int | None = None
    undone_by: int | None = None


class Journal:
    def __init__(self, root: Path | None = None, prober: Callable[[str], dict] | None = None):
        self.root = root or state_dir()
        self.prober = prober or probe
        self.file = self.root / "journal.jsonl"
        self.backups = self.root / "backups"

    def _ensure_private_dir(self, path: Path) -> None:
        # backups can hold copies of root-only files (e.g. Wi-Fi passwords), so keep them owner-only
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)

    @contextlib.contextmanager
    def _locked(self):
        self._ensure_private_dir(self.root)
        with open(self.root / "journal.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def entries(self) -> list[Entry]:
        try:
            lines = self.file.read_text().splitlines()
        except OSError:
            return []
        known = set(Entry.__dataclass_fields__)
        return [Entry(**{k: v for k, v in json.loads(line).items() if k in known}) for line in lines if line.strip()]

    def _write_all(self, entries: list[Entry]) -> None:
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("".join(json.dumps(asdict(e)) + "\n" for e in entries))
        os.replace(tmp, self.file)

    def get(self, entry_id: int) -> Entry | None:
        return next((e for e in self.entries() if e.id == entry_id), None)

    def _append(self, entry: Entry) -> Entry:
        with self._locked():
            entries = self.entries()
            entry.id = (entries[-1].id + 1) if entries else 1
            entries.append(entry)
            self._write_all(entries)
        return entry

    def _update(self, entry_id: int, **changes) -> None:
        with self._locked():
            entries = self.entries()
            for e in entries:
                if e.id == entry_id:
                    for k, v in changes.items():
                        setattr(e, k, v)
            self._write_all(entries)

    # --- recording -------------------------------------------------------------------------------

    def record_file(self, path: str, before: bytes | None, after: bytes | None, summary: str = "",
                    source: str = "tux", undoes: int | None = None) -> Entry:
        """Record a file change. `before=None` means the file didn't exist; `after=None` means unknown."""
        path = str(Path(path).expanduser().absolute())
        entry = self._append(Entry(
            id=0, time=_now(), kind="file", summary=summary or f"edit {path}", source=source, path=path,
            before_hash=_sha(before) if before is not None else None,
            after_hash=_sha(after) if after is not None else None, undoes=undoes))
        if before is not None:
            self._ensure_private_dir(self.backups)
            name = f"{entry.id:05d}-{Path(path).name}"
            fd = os.open(self.backups / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(before)
            self._update(entry.id, backup=name)
            entry.backup = name
        return entry

    def record_command(self, command: str, before: dict, after: dict, exit_code: int | None,
                       source: str = "tux", undoes: int | None = None) -> Entry:
        inverse, notes, regenerate = invert(command, before, after)
        return self._append(Entry(
            id=0, time=_now(), kind="command", summary=command, source=source, command=command,
            exit_code=exit_code, inverse=inverse, notes=notes, regenerate=regenerate, undoes=undoes))

    def backup_bytes(self, entry: Entry) -> bytes | None:
        return (self.backups / entry.backup).read_bytes() if entry.backup else None

    # --- undo ------------------------------------------------------------------------------------

    def resolve(self, ref: str | int | None) -> Entry | None:
        """'last' / None → the most recent change that can still be undone; otherwise an id."""
        if ref in (None, "", "last"):
            # skip undo records, so repeated "undo last" walks back through history instead of redoing
            return next((e for e in reversed(self.entries())
                         if e.undone_by is None and e.source != "undo" and undoable(e)), None)
        try:
            return self.get(int(str(ref).lstrip("#")))
        except ValueError:
            return None

    def plan(self, entry: Entry, read_current: Callable[[str], bytes | None] | None = None,
             force: bool = False) -> "Plan":
        read_current = read_current or read_local
        p = Plan(entry=entry)
        if entry.undone_by is not None:
            p.blocked = f"change #{entry.id} was already undone by #{entry.undone_by}"
            return p

        if entry.kind == "command":
            if entry.inverse:
                p.steps = list(entry.inverse)
                p.description = "run " + " && ".join(entry.inverse)
                p.warnings += entry.notes
            elif entry.regenerate:
                p.blocked = (f"`{entry.command}` only rebuilds files from config. Undo the config change it was "
                             "rebuilding, and tux re-runs it automatically.")
            elif entry.notes:
                p.blocked = "it can't be reversed automatically: " + "; ".join(entry.notes)
            else:
                p.blocked = "it didn't change anything tux tracks (packages, services, holds), so there's nothing to undo"
            return p

        # file entry
        assert entry.path is not None
        # newer edits to the same file that are still in effect (undo records only reverse other entries)
        later = [e for e in self.entries() if e.id > entry.id and e.kind == "file" and e.path == entry.path
                 and e.undone_by is None and e.undoes is None]
        if later and not force:
            ids = ", ".join(f"#{e.id}" for e in later)
            p.blocked = f"{entry.path} was changed again later by {ids}; undo those first (newest first)"
            return p

        current = read_current(entry.path)
        cur_hash = _sha(current) if current is not None else None
        if entry.after_hash is not None and cur_hash != entry.after_hash and not force:
            if entry.backup is None and current is None:
                p.blocked = f"{entry.path} has already been deleted; nothing to undo"
            else:
                p.blocked = (f"{entry.path} has been modified since tux changed it; undoing would discard those "
                             "edits. Use --force to restore anyway.")
            p.diff = _diff(current, self.backup_bytes(entry), entry.path)
            return p

        target = self.backup_bytes(entry)
        if target is None:
            p.description = f"Delete {entry.path} (tux created it)"
        elif current == target:
            p.description = f"{entry.path} already matches the content before change #{entry.id}"
            p.noop = True
        else:
            p.description = f"Restore {entry.path} to its content before change #{entry.id}"
        p.diff = _diff(current, target, entry.path)
        p.follow_up = [e.command for e in self.entries()
                       if e.id > entry.id and e.kind == "command" and e.regenerate and e.undone_by is None and e.command]
        if p.follow_up:
            p.warnings.append("These later commands rebuilt files from config and will be re-run afterwards: "
                              + "; ".join(p.follow_up))
        return p

    def undo(self, ref: str | int | None, runner: Runner, force: bool = False,
             read_current: Callable[[str], bytes | None] | None = None) -> "UndoResult":
        entry = self.resolve(ref)
        if entry is None:
            return UndoResult(False, "No matching change to undo." if ref not in (None, "", "last")
                              else "Nothing to undo.")
        read_current = read_current or (lambda p: read_with_root(p, runner))
        plan = self.plan(entry, read_current, force)
        if plan.blocked:
            return UndoResult(False, f"Can't undo #{entry.id}: {plan.blocked}", entry=entry)

        if entry.kind == "command":
            outputs = []
            combined = " && ".join(plan.steps)
            before = self.prober(combined)
            for step in plan.steps:
                code, out = runner(step)
                outputs.append(f"$ {step}\n{out.strip()}")
                if code != 0:
                    return UndoResult(False, f"Undo of #{entry.id} failed at `{step}` (exit {code}).\n"
                                      + "\n".join(outputs), entry=entry)
            new = self.record_command(combined, before, self.prober(combined), 0, source="undo", undoes=entry.id)
        else:
            if plan.noop:
                return UndoResult(True, plan.description, entry=entry)
            current = read_current(entry.path)
            target = self.backup_bytes(entry)
            code, out = write_with_root(entry.path, target, runner, self.root)
            if code != 0:
                return UndoResult(False, f"Couldn't restore {entry.path}: {out}", entry=entry)
            new = self.record_file(entry.path, current, target, summary=f"undo #{entry.id}: {plan.description}",
                                   source="undo", undoes=entry.id)
            for cmd in plan.follow_up:
                code, out = runner(cmd)
                if code != 0:
                    self._mark_undone(entry, new)
                    return UndoResult(False, f"Restored {entry.path}, but re-running `{cmd}` failed (exit {code}):"
                                      f"\n{out}\nRun it again by hand.", entry=entry, new=new)

        self._mark_undone(entry, new)
        return UndoResult(True, f"Undid #{entry.id}: {plan.description}. "
                          f"Recorded as #{new.id}, which can itself be undone.", entry=entry, new=new)

    def _mark_undone(self, entry: Entry, new: Entry) -> None:
        self._update(entry.id, undone_by=new.id)
        if entry.undoes is not None:
            # reversing an undo puts the original change back in effect
            self._update(entry.undoes, undone_by=None)


@dataclass
class Plan:
    entry: Entry
    description: str = ""
    steps: list[str] = field(default_factory=list)
    diff: str = ""
    warnings: list[str] = field(default_factory=list)
    follow_up: list[str] = field(default_factory=list)
    blocked: str = ""
    noop: bool = False

    def render(self) -> str:
        e = self.entry
        lines = [f"Change #{e.id} ({e.time}, {e.kind}): {e.summary}"]
        if self.blocked:
            lines.append(f"Can't undo: {self.blocked}")
        else:
            lines.append(f"Undo will: {self.description}")
        lines += [f"Note: {w}" for w in self.warnings]
        if self.diff:
            lines.append(self.diff.rstrip())
        return "\n".join(lines)


@dataclass
class UndoResult:
    ok: bool
    message: str
    entry: Entry | None = None
    new: Entry | None = None


def undoable(e: Entry) -> bool:
    return e.kind == "file" or bool(e.inverse)


def describe(e: Entry) -> str:
    if e.undone_by is not None:
        status = f"undone by #{e.undone_by}"
    elif e.kind == "file":
        status = "undoable"
    elif e.inverse:
        status = "undoable"
    elif e.regenerate:
        status = "re-run after undoing its config"
    else:
        status = "manual: " + ("; ".join(e.notes) if e.notes else "no automatic inverse")
    return f"#{e.id:<4} {e.time}  {e.summary}  [{status}]"


def _diff(current: bytes | None, target: bytes | None, path: str) -> str:
    def lines(b: bytes | None) -> list[str]:
        return [] if b is None else b.decode(errors="replace").splitlines(True)
    return "".join(difflib.unified_diff(lines(current), lines(target), f"{path} (now)", f"{path} (after undo)"))


# --------------------------------------------------------------------------------------------------
# Reading and writing files that may need root
# --------------------------------------------------------------------------------------------------

def read_local(path: str) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


def read_with_root(path: str, runner: Runner) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None
    except PermissionError:
        code, out = runner(f"sudo base64 -w0 {shlex.quote(path)}")
        if code != 0:
            raise PermissionError(f"couldn't read {path} as root: {out.strip()}")
        return base64.b64decode(out.strip().splitlines()[-1])


def write_with_root(path: str, content: bytes | None, runner: Runner, scratch: Path) -> tuple[int, str]:
    """Write (or, for content=None, delete) a file, using sudo only when needed. Keeps owner/mode."""
    p = Path(path)
    try:
        if content is None:
            p.unlink(missing_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "r+b" if p.exists() else "wb") as f:   # rewrite in place to keep owner and mode
                f.write(content)
                f.truncate()
        return 0, ""
    except PermissionError:
        pass
    if content is None:
        return runner(f"sudo rm -f {shlex.quote(path)}")
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = scratch / "restore.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(content)
    try:
        return runner(f"sudo tee {shlex.quote(path)} < {shlex.quote(str(tmp))} > /dev/null")
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------------------------------
# Commands: parsing, state probes, and inverses
# --------------------------------------------------------------------------------------------------

ROOT_PREFIX = re.compile(r"^(sudo|tux-sudo|pkexec|doas)$")
NOOP_COMMANDS = [
    (["apt", "update"]), (["apt-get", "update"]), (["dnf", "makecache"]), (["dnf", "check-update"]),
    (["zypper", "refresh"]), (["zypper", "ref"]), (["snap", "refresh", "--list"]),
    (["systemctl", "daemon-reload"]), (["systemctl", "--user", "daemon-reload"]),
]
NOOP_SYSTEMCTL_VERBS = {"restart", "reload", "try-restart", "reload-or-restart", "try-reload-or-restart",
                        "daemon-reload", "reset-failed", "kill"}
REGENERATE = {"update-grub", "update-grub2", "grub-mkconfig", "grub2-mkconfig", "update-initramfs", "dracut",
              "mkinitcpio", "locale-gen", "update-locale", "sysctl", "udevadm", "update-desktop-database",
              "fc-cache", "ldconfig", "depmod", "update-ca-certificates", "netplan", "modprobe"}


@dataclass
class Action:
    kind: str                     # pkg | svc | hold | monitor | noop | regen | unknown
    raw: str
    root: bool = False
    manager: str = ""
    verb: str = ""
    items: list[str] = field(default_factory=list)
    user: bool = False
    now: bool = False


def _segments(command: str) -> list[str] | None:
    """Split on && and ; (pipes and backgrounding make a command non-invertible)."""
    if re.search(r"(?<![|&])\|(?!\|)|`|\$\(|(?<!&)&(?!&)", command.replace("2>&1", "").replace(">&2", "")):
        return None
    return [s.strip() for s in re.split(r"&&|;", command) if s.strip()]


def _strip_root(argv: list[str]) -> tuple[list[str], bool]:
    root = False
    while argv and ROOT_PREFIX.match(argv[0]):
        root = True
        argv = argv[1:]
        while argv and argv[0].startswith("-"):    # sudo -A, sudo -E, sudo -u user …
            argv = argv[2:] if argv[0] in ("-u", "-g", "-C", "-D") else argv[1:]
    while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
        argv = argv[1:]
    return argv, root


def _positional(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def parse(segment: str) -> Action:
    try:
        argv = shlex.split(segment)
    except ValueError:
        return Action("unknown", segment)
    argv = [a for a in argv if a not in ("2>&1", ">/dev/null", "2>/dev/null")]
    argv, root = _strip_root(argv)
    if not argv:
        return Action("noop", segment)
    name, args = argv[0].rsplit("/", 1)[-1], argv[1:]
    a = Action("unknown", segment, root=root, manager=name)

    if any(argv[: len(n)] == n for n in NOOP_COMMANDS):
        a.kind = "noop"
    elif name in ("apt", "apt-get") and args:
        verb = _positional(args)[0] if _positional(args) else ""
        pkgs = _positional(args)[1:]
        if verb in ("install", "reinstall") and pkgs and not any(p.endswith(".deb") or "/" in p for p in pkgs):
            a.kind, a.verb, a.items = "pkg", "install", [p.split("=")[0] for p in pkgs]
        elif verb in ("remove", "purge") and pkgs:
            a.kind, a.verb, a.items = "pkg", verb, pkgs
    elif name in ("dnf", "yum", "zypper") and args:
        pos = _positional(args)
        verb = pos[0] if pos else ""
        if verb in ("install", "in") and pos[1:]:
            a.kind, a.verb, a.items = "pkg", "install", pos[1:]
        elif verb in ("remove", "erase", "rm") and pos[1:]:
            a.kind, a.verb, a.items = "pkg", "remove", pos[1:]
    elif name == "pacman" and args:
        flag = next((x for x in args if x.startswith("-") and not x.startswith("--")), "")
        pkgs = _positional(args)
        if re.fullmatch(r"-S[a-z]*", flag) and "u" not in flag and "y" not in flag and pkgs:
            a.kind, a.verb, a.items = "pkg", "install", pkgs
        elif re.fullmatch(r"-R[a-z]*", flag) and pkgs:
            a.kind, a.verb, a.items = "pkg", "remove", pkgs
    elif name == "snap" and args and args[0] in ("install", "remove") and _positional(args[1:]):
        a.kind, a.verb, a.items = "pkg", args[0], _positional(args[1:])
    elif name == "flatpak" and args and args[0] in ("install", "uninstall"):
        refs = [x for x in _positional(args[1:]) if x.count(".") >= 2]
        if refs:
            a.kind, a.verb, a.items = "pkg", "install" if args[0] == "install" else "remove", refs
    elif name == "apt-mark" and args and args[0] in ("hold", "unhold") and args[1:]:
        a.kind, a.verb, a.items = "hold", args[0], _positional(args[1:])
    elif name == "systemctl":
        a.user = "--user" in args
        a.now = "--now" in args
        pos = _positional(args)
        if pos and pos[0] in NOOP_SYSTEMCTL_VERBS:
            a.kind = "noop"
        elif pos and pos[0] in ("enable", "disable", "start", "stop", "mask", "unmask") and pos[1:]:
            a.kind, a.verb, a.items = "svc", pos[0], pos[1:]
    elif name in REGENERATE:
        a.kind = "regen"
    elif name == "tux-monitor" and args and args[0] in ("enable", "disable"):
        a.kind, a.verb, a.items = "monitor", args[0], ["tux-monitor.timer"]
    elif name in ("tux-backup", "tux-note", "tux-undo"):
        a.kind = "noop"    # journaled separately / harmless
    return a


def _pkg_query(manager: str, item: str) -> str:
    q = shlex.quote(item)
    return {
        "apt": f"dpkg-query -W -f='${{Status}}' {q} 2>/dev/null | grep -q 'install ok installed'",
        "apt-get": f"dpkg-query -W -f='${{Status}}' {q} 2>/dev/null | grep -q 'install ok installed'",
        "dnf": f"rpm -q {q} >/dev/null 2>&1", "yum": f"rpm -q {q} >/dev/null 2>&1",
        "zypper": f"rpm -q {q} >/dev/null 2>&1", "pacman": f"pacman -Q {q} >/dev/null 2>&1",
        "snap": f"snap list {q} >/dev/null 2>&1", "flatpak": f"flatpak info {q} >/dev/null 2>&1",
    }[manager]


def probes(command: str) -> dict[str, str]:
    """State queries whose before/after values decide the inverse. Maps key -> shell command."""
    out: dict[str, str] = {}
    for seg in _segments(command) or []:
        a = parse(seg)
        for item in a.items:
            if a.kind == "pkg":
                out[f"pkg:{a.manager}:{item}"] = _pkg_query(a.manager, item)
            elif a.kind == "hold":
                out[f"hold:{item}"] = f"apt-mark showhold | grep -qx {shlex.quote(item)}"
            elif a.kind == "svc" or a.kind == "monitor":
                scope = "--user " if a.user or a.kind == "monitor" else ""
                q = shlex.quote(item)
                out[f"svc-enabled:{scope}{item}"] = f"systemctl {scope}is-enabled {q} 2>/dev/null; true"
                out[f"svc-active:{scope}{item}"] = f"systemctl {scope}is-active {q} 2>/dev/null; true"
    return out


def probe(command: str, run: Callable[[str], tuple[int, str]] | None = None) -> dict:
    """Capture the state the command could change. Read-only and fast."""
    def default_run(cmd: str) -> tuple[int, str]:
        try:
            p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5,
                               stdin=subprocess.DEVNULL, env={**os.environ, "LC_ALL": "C"})
            return p.returncode, p.stdout.strip()
        except subprocess.TimeoutExpired:
            return -1, ""
    run = run or default_run
    state: dict = {}
    for key, cmd in probes(command).items():
        code, out = run(cmd)
        state[key] = (out.splitlines()[-1] if out else "unknown") if key.startswith("svc-") else code == 0
    return state


def invert(command: str, before: dict, after: dict) -> tuple[list[str], list[str], bool]:
    """Work out the commands that reverse `command`, given probe() results from before and after it ran.

    Returns (inverse_commands, notes, regenerate). An empty inverse with notes means it can't be undone
    automatically; an empty inverse with no notes means it changed nothing that needs undoing.
    """
    segments = _segments(command)
    if segments is None:
        return [], ["pipelines and command substitution can't be reversed automatically"], False
    actions = [parse(s) for s in segments]
    inverse: list[str] = []
    notes: list[str] = []
    regenerate = False
    for a in reversed(actions):   # undo in reverse order
        sudo = "sudo " if a.root else ""
        if a.kind == "noop":
            continue
        if a.kind == "regen":
            regenerate = True
            continue
        if a.kind == "unknown":
            notes.append(f"`{a.raw}` has no automatic inverse")
            continue
        if a.kind == "pkg":
            key = lambda i: f"pkg:{a.manager}:{i}"   # noqa: E731
            if a.verb in ("install", "reinstall"):
                changed = [i for i in a.items if not before.get(key(i)) and after.get(key(i))]
                kept = [i for i in a.items if before.get(key(i))]
                if kept:
                    notes.append(f"{', '.join(kept)} was already installed, so undo leaves it in place")
                if changed:
                    inverse.append(sudo + _pkg_cmd(a.manager, "remove", changed))
            else:
                changed = [i for i in a.items if before.get(key(i)) and not after.get(key(i))]
                if changed:
                    inverse.append(sudo + _pkg_cmd(a.manager, "install", changed))
                    if a.verb == "purge":
                        notes.append("purge deleted the packages' config files; reinstalling restores defaults")
        elif a.kind == "hold":
            if a.verb == "hold":
                changed = [i for i in a.items if not before.get(f"hold:{i}") and after.get(f"hold:{i}")]
            else:
                changed = [i for i in a.items if before.get(f"hold:{i}") and not after.get(f"hold:{i}")]
            if changed:
                inverse.append(sudo + f"apt-mark {'unhold' if a.verb == 'hold' else 'hold'} " + " ".join(changed))
        elif a.kind == "monitor":
            was, now = (before.get("svc-enabled:--user tux-monitor.timer"),
                        after.get("svc-enabled:--user tux-monitor.timer"))
            if was != now and "enabled" in (was, now):
                inverse.append("tux-monitor disable" if now == "enabled" else "tux-monitor enable")
        elif a.kind == "svc":
            scope = "--user " if a.user else ""
            sudo = "" if a.user else sudo
            for unit in a.items:
                was_e, now_e = before.get(f"svc-enabled:{scope}{unit}"), after.get(f"svc-enabled:{scope}{unit}")
                was_a, now_a = before.get(f"svc-active:{scope}{unit}"), after.get(f"svc-active:{scope}{unit}")
                steps = []
                if was_e != now_e and was_e is not None:
                    if now_e == "masked":
                        steps.append(f"systemctl {scope}unmask {unit}")
                    if was_e == "masked":
                        steps.append(f"systemctl {scope}mask {unit}")
                    elif was_e == "enabled":
                        steps.append(f"systemctl {scope}enable {unit}")
                    elif was_e == "disabled" and now_e == "enabled":
                        steps.append(f"systemctl {scope}disable {unit}")
                if was_a != now_a and was_a is not None:
                    if was_a == "active":
                        steps.append(f"systemctl {scope}start {unit}")
                    elif now_a == "active":
                        steps.append(f"systemctl {scope}stop {unit}")
                inverse += [sudo + s for s in steps]
    return inverse, notes, regenerate


def _pkg_cmd(manager: str, verb: str, items: list[str]) -> str:
    pkgs = " ".join(shlex.quote(i) for i in items)
    if manager in ("apt", "apt-get"):
        return f"apt-get {'remove' if verb == 'remove' else 'install'} -y {pkgs}"
    if manager in ("dnf", "yum"):
        return f"{manager} {'remove' if verb == 'remove' else 'install'} -y {pkgs}"
    if manager == "zypper":
        return f"zypper --non-interactive {'remove' if verb == 'remove' else 'install'} {pkgs}"
    if manager == "pacman":
        return f"pacman {'-R' if verb == 'remove' else '-S'} --noconfirm {pkgs}"
    if manager == "snap":
        return f"snap {verb} {pkgs}"
    if manager == "flatpak":
        return f"flatpak {'uninstall' if verb == 'remove' else 'install'} -y {pkgs}"
    raise ValueError(manager)
