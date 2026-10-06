"""Tool definitions sent to Claude, and their local implementations."""

from __future__ import annotations

import datetime as dt
import difflib
import json
import os
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from . import notes, sysinfo
from .safety import Risk, Verdict, classify, is_sensitive_path

MAX_OUTPUT_CHARS = 30_000

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "tux"
ACTION_LOG = STATE_DIR / "actions.log"
BACKUP_DIR = STATE_DIR / "backups"


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOL_DEFS = [
    _tool(
        "run_command",
        "Run a bash command on the user's machine and return its output. Read-only inspection commands "
        "run immediately; anything that changes the system (installs, config edits, service restarts, sudo) "
        "is shown to the user, who must approve it first. Prefer non-interactive flags (-y, --no-pager). "
        "Use sudo explicitly when root is required; the user will be prompted for their password.",
        {
            "command": {"type": "string", "description": "The bash command to run."},
            "purpose": {"type": "string", "description": "One short sentence, shown to the user, saying why."},
            "timeout_seconds": {"type": "integer", "description": "Default 60, max 900 (for long installs)."},
        },
        ["command", "purpose"],
    ),
    _tool(
        "hardware_scan",
        "Run a curated batch of diagnostics for one area and return the combined output. Faster and more "
        "thorough than issuing individual commands. Areas: " + ", ".join(sysinfo.SCANS) + ".",
        {"area": {"type": "string", "enum": list(sysinfo.SCANS)}},
        ["area"],
    ),
    _tool(
        "read_file",
        "Read a text file (config, log, script). Returns at most `max_lines` lines from the head or tail.",
        {
            "path": {"type": "string"},
            "tail": {"type": "boolean", "description": "Read from the end instead (useful for logs)."},
            "max_lines": {"type": "integer", "description": "Default 400."},
        },
        ["path"],
    ),
    _tool(
        "write_file",
        "Create or overwrite a text file. The user sees a diff and must approve. The previous version is "
        "backed up automatically. Files outside the user's home are written with sudo.",
        {
            "path": {"type": "string"},
            "content": {"type": "string", "description": "The complete new file content."},
            "purpose": {"type": "string"},
        },
        ["path", "content", "purpose"],
    ),
    _tool(
        "search_logs",
        "Search the systemd journal. Combine filters to narrow results.",
        {
            "grep": {"type": "string", "description": "Case-insensitive regex to match messages."},
            "unit": {"type": "string", "description": "systemd unit, e.g. NetworkManager.service"},
            "since": {"type": "string", "description": "e.g. '1 hour ago', 'today', '2026-01-01'"},
            "priority": {"type": "string", "description": "Max priority: emerg..debug or 0-7. 'err' = errors+."},
            "boot": {"type": "integer", "description": "0 = current boot, -1 = previous, etc."},
            "kernel": {"type": "boolean", "description": "Kernel messages only."},
            "user_unit": {"type": "boolean", "description": "Treat `unit` as a user unit."},
            "lines": {"type": "integer", "description": "Default 80."},
        },
        [],
    ),
    _tool(
        "remember",
        "Save a durable fact about this machine (hardware quirks, past fixes, user preferences) to the "
        "notes file that is loaded at the start of every session. Keep each note to one line.",
        {"note": {"type": "string"}},
        ["note"],
    ),
]

WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}


class Approver(Protocol):
    def approve_command(self, command: str, purpose: str, verdict: Verdict) -> tuple[bool, str]: ...
    def approve_write(self, path: str, diff: str, purpose: str) -> tuple[bool, str]: ...


@dataclass
class ToolContext:
    approver: Approver
    read_only: bool = False
    on_line: Callable[[str], None] | None = None
    always_allowed: set[str] = field(default_factory=set)


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return f"{text[:half]}\n\n... [{len(text) - MAX_OUTPUT_CHARS} chars truncated] ...\n\n{text[-half:]}"


def log_action(kind: str, detail: str, outcome: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().isoformat(timespec="seconds")
        with ACTION_LOG.open("a") as f:
            f.write(json.dumps({"time": stamp, "kind": kind, "detail": detail, "outcome": outcome}) + "\n")
    except OSError:
        pass


def _exec(command: str, timeout: int, interactive_sudo: bool,
          on_line: Callable[[str], None] | None = None) -> tuple[int, str]:
    """Run a command, streaming output to `on_line`, killing the whole tree on timeout or Ctrl-C."""
    proc = subprocess.Popen(
        ["bash", "-c", command],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, errors="replace",
        # sudo reads its password from /dev/tty, so it needs our session/terminal
        stdin=None if interactive_sudo else subprocess.DEVNULL,
        start_new_session=not interactive_sudo,
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive", "SYSTEMD_PAGER": "", "PAGER": "cat"},
    )
    chunks: list[str] = []

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            chunks.append(line)
            if on_line:
                on_line(line)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    reason = ""
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        reason = f"timed out after {timeout}s"
    except KeyboardInterrupt:
        reason = "interrupted by user"
    if reason:
        try:
            if interactive_sudo:
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
    t.join(timeout=2)
    out = "".join(chunks)
    return (-1, f"{out}\n[{reason}]") if reason else (proc.returncode, out)


def run_command(ctx: ToolContext, command: str, purpose: str, timeout_seconds: int = 60) -> tuple[str, bool]:
    verdict = classify(command)
    timeout = max(1, min(int(timeout_seconds or 60), 900))

    if verdict.risk is Risk.BLOCKED:
        log_action("command", command, f"blocked: {verdict.reason}")
        return (f"BLOCKED by safety policy ({verdict.reason}). Do not retry variants of this command. "
                "If it is truly needed, explain to the user exactly what to run themselves and why."), True

    if verdict.risk is Risk.CHANGE:
        if ctx.read_only:
            return ("Not run: tux is in read-only mode. Tell the user the exact command and what it would do "
                    "so they can run it themselves."), True
        if command not in ctx.always_allowed:
            ok, note = ctx.approver.approve_command(command, purpose, verdict)
            if not ok:
                log_action("command", command, "declined")
                return f"User declined to run this command.{' Their reason: ' + note if note else ''}", True
            if note == "always":
                ctx.always_allowed.add(command)

    code, out = _exec(command, timeout, interactive_sudo=verdict.needs_root, on_line=ctx.on_line)
    if verdict.risk is Risk.CHANGE:
        log_action("command", command, f"exit {code}")
    return _truncate(f"exit code: {code}\n{out}"), code != 0


def hardware_scan(ctx: ToolContext, area: str) -> tuple[str, bool]:
    return _truncate(sysinfo.scan(area)), False


def read_file(ctx: ToolContext, path: str, tail: bool = False, max_lines: int = 400) -> tuple[str, bool]:
    p = Path(path).expanduser()
    if is_sensitive_path(str(p.resolve())):
        return "Refused: this file holds secrets (keys/passwords). Ask the user to check it themselves.", True
    try:
        if p.stat().st_size > 50_000_000 and not tail:
            return "File is larger than 50MB; read with tail=true or use run_command with grep.", True
        lines = p.read_text(errors="replace").splitlines()
    except PermissionError:
        return f"Permission denied reading {p}. Use run_command with `sudo cat` / `sudo tail` if needed.", True
    except OSError as e:
        return f"Cannot read {p}: {e}", True
    n = max(1, int(max_lines or 400))
    chunk = lines[-n:] if tail else lines[:n]
    header = f"{p} ({len(lines)} lines, showing {'last' if tail else 'first'} {len(chunk)})\n"
    return _truncate(header + "\n".join(chunk)), False


def write_file(ctx: ToolContext, path: str, content: str, purpose: str) -> tuple[str, bool]:
    p = Path(path).expanduser().absolute()
    if ctx.read_only:
        return "Not written: tux is in read-only mode. Show the user the change instead.", True
    if is_sensitive_path(str(p)):
        return "Refused: this path holds secrets.", True

    try:
        old = p.read_text() if p.exists() else ""
    except PermissionError:
        code, old = _exec(f"sudo cat {_q(p)}", 30, interactive_sudo=True)
        if code != 0:
            return f"Could not read existing {p} for backup: {old}", True
    diff = "".join(difflib.unified_diff(old.splitlines(True), content.splitlines(True),
                                        f"{p} (current)", f"{p} (proposed)"))
    if not diff:
        return "No changes: the file already has this content.", False

    ok, note = ctx.approver.approve_write(str(p), diff, purpose)
    if not ok:
        log_action("write", str(p), "declined")
        return f"User declined the edit.{' Their reason: ' + note if note else ''}", True

    backup = ""
    if p.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = BACKUP_DIR / f"{str(p).strip('/').replace('/', '__')}.{stamp}"
        backup_path.write_text(old)
        backup = str(backup_path)

    probe = p if p.exists() else next(d for d in p.parents if d.exists())
    needs_root = not os.access(probe, os.W_OK)
    if needs_root:
        tmp = STATE_DIR / "pending-write"
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp.write_text(content)
        code, out = _exec(f"sudo install -D -m 644 {_q(tmp)} {_q(p)}" if not p.exists()
                          else f"sudo cp {_q(tmp)} {_q(p)}", 60, interactive_sudo=True)
        tmp.unlink(missing_ok=True)
        if code != 0:
            return f"sudo write failed: {out}", True
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    log_action("write", str(p), f"ok backup={backup or 'none (new file)'}")
    restore = f" Restore with: {'sudo ' if needs_root else ''}cp {backup} {p}" if backup else ""
    return f"Wrote {p}.{restore}", False


def _q(p: Path | str) -> str:
    import shlex
    return shlex.quote(str(p))


def search_logs(ctx: ToolContext, grep: str = "", unit: str = "", since: str = "", priority: str = "",
                boot: int | None = None, kernel: bool = False, user_unit: bool = False,
                lines: int = 80) -> tuple[str, bool]:
    if not shutil.which("journalctl"):
        return "journalctl not available (non-systemd system). Use read_file on /var/log/* instead.", True
    args = ["journalctl", "--no-pager", "-q", "-o", "short-iso", "-n", str(max(1, min(int(lines or 80), 1000)))]
    if user_unit:
        args.append("--user")
    if grep:
        args += ["-g", grep, "--case-sensitive=false"]
    if unit:
        args += ["-u", unit]
    if since:
        args += ["--since", since]
    if priority:
        args += ["-p", priority]
    if boot is not None:
        args += ["-b", str(boot)]
    if kernel:
        args.append("-k")
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        out = (p.stdout + p.stderr).strip() or "(no matching entries)"
    except subprocess.TimeoutExpired:
        return "journalctl timed out; narrow the search.", True
    return _truncate(out), False


def remember(ctx: ToolContext, note: str) -> tuple[str, bool]:
    return f"Saved to {notes.add_note(note)}.", False


HANDLERS = {
    "run_command": run_command,
    "hardware_scan": hardware_scan,
    "read_file": read_file,
    "write_file": write_file,
    "search_logs": search_logs,
    "remember": remember,
}

_JSON_TYPES = {"string": str, "integer": int, "boolean": bool}


def validate_input(name: str, args: object) -> str | None:
    """Check a (possibly eagerly-streamed, so possibly truncated) tool input against its schema."""
    schema = next((t["input_schema"] for t in TOOL_DEFS if t["name"] == name), None)
    if schema is None:
        return f"unknown tool {name}"
    if not isinstance(args, dict):
        return "input is not an object"
    for key in schema["required"]:
        if key not in args:
            return f"missing required field '{key}'"
    for key, value in args.items():
        prop = schema["properties"].get(key)
        if prop is None:
            return f"unexpected field '{key}'"
        expected = _JSON_TYPES[prop["type"]]
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            return f"field '{key}' should be {prop['type']}"
        if "enum" in prop and value not in prop["enum"]:
            return f"field '{key}' must be one of {prop['enum']}"
    return None


def dispatch(ctx: ToolContext, name: str, args: dict) -> tuple[str, bool]:
    error = validate_input(name, args)
    if error:
        return f"INVALID_INPUT: {error}. Re-issue the call with valid arguments.", True
    try:
        return HANDLERS[name](ctx, **args)
    except KeyboardInterrupt:
        return "Interrupted by the user.", True
    except Exception as e:  # noqa: BLE001
        return f"Tool error: {type(e).__name__}: {e}", True
