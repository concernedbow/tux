"""Tool definitions sent to Claude, and their local implementations."""

from __future__ import annotations

import difflib
import os
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from tux import journal, notes, sysinfo
from tux.safety import Risk, Verdict, classify, is_sensitive_path

MAX_OUTPUT_CHARS = 30_000



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
    _tool(
        "undo_change",
        "List, inspect, or undo changes tux made (file edits, package installs/removals, service changes). "
        "Use when the user wants to revert something, or a fix made things worse. 'list' shows recent "
        "changes; 'show' explains exactly what undoing one would do; 'undo' reverts it after the user "
        "approves. Omit change_id to target the most recent change.",
        {
            "action": {"type": "string", "enum": ["list", "show", "undo"]},
            "change_id": {"type": "integer"},
            "force": {"type": "boolean", "description": "Restore even if the file was modified since tux "
                                                        "changed it. Only after the user agrees."},
        },
        ["action"],
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
    prober: Callable[[str], dict] | None = None   # state probes for undo; injectable for tests

    @property
    def journal(self) -> journal.Journal:
        return journal.Journal(prober=self.prober)

    def runner(self, command: str) -> tuple[int, str]:
        """Run a journal/undo step; steps starting with `sudo ` prompt on the terminal."""
        return _exec(command, 600, interactive_sudo=command.startswith("sudo "), on_line=self.on_line)


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return f"{text[:half]}\n\n... [{len(text) - MAX_OUTPUT_CHARS} chars truncated] ...\n\n{text[-half:]}"


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
        return (f"BLOCKED by safety policy ({verdict.reason}). Do not retry variants of this command. "
                "If it is truly needed, explain to the user exactly what to run themselves and why."), True

    if verdict.risk is Risk.CHANGE:
        if ctx.read_only:
            return ("Not run: tux is in read-only mode. Tell the user the exact command and what it would do "
                    "so they can run it themselves."), True
        if command not in ctx.always_allowed:
            ok, note = ctx.approver.approve_command(command, purpose, verdict)
            if not ok:
                return f"User declined to run this command.{' Their reason: ' + note if note else ''}", True
            if note == "always":
                ctx.always_allowed.add(command)

    if verdict.risk is not Risk.CHANGE:
        code, out = _exec(command, timeout, interactive_sudo=verdict.needs_root, on_line=ctx.on_line)
        return _truncate(f"exit code: {code}\n{out}"), code != 0

    j = ctx.journal
    before = j.prober(command)
    code, out = _exec(command, timeout, interactive_sudo=verdict.needs_root, on_line=ctx.on_line)
    entry = j.record_command(command, before, j.prober(command), code, source="api")
    if entry.inverse:
        footer = f"[recorded as change #{entry.id}; undo reverses it with: {' && '.join(entry.inverse)}]"
    elif entry.notes:
        footer = f"[recorded as change #{entry.id}; not automatically undoable: {'; '.join(entry.notes)}]"
    else:
        footer = f"[recorded as change #{entry.id}]"
    return _truncate(f"exit code: {code}\n{out}\n{footer}"), code != 0


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
        old = journal.read_with_root(str(p), ctx.runner)
    except PermissionError as e:
        return f"Could not read existing {p}: {e}", True
    new = content.encode()
    diff = "".join(difflib.unified_diff((old or b"").decode(errors="replace").splitlines(True),
                                        content.splitlines(True), f"{p} (current)", f"{p} (proposed)"))
    if old == new:
        return "No changes: the file already has this content.", False

    ok, note = ctx.approver.approve_write(str(p), diff, purpose)
    if not ok:
        return f"User declined the edit.{' Their reason: ' + note if note else ''}", True

    j = ctx.journal
    code, out = journal.write_with_root(str(p), new, ctx.runner, j.root)
    if code != 0:
        return f"Write failed: {out}", True
    entry = j.record_file(str(p), old, new, summary=purpose or f"edit {p}", source="api")
    return f"Wrote {p}. Recorded as change #{entry.id} (the previous version is saved; undo_change can restore it).", False


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


def undo_change(ctx: ToolContext, action: str, change_id: int | None = None,
                force: bool = False) -> tuple[str, bool]:
    j = ctx.journal
    if action == "list":
        entries = j.entries()[-25:]
        return ("\n".join(journal.describe(e) for e in entries) if entries else "No changes recorded yet."), False

    entry = j.resolve(change_id)
    if entry is None:
        return ("No change with that id." if change_id is not None else "Nothing to undo."), True
    plan = j.plan(entry, lambda p: journal.read_with_root(p, ctx.runner), force)
    if action == "show" or plan.blocked:
        return plan.render(), bool(plan.blocked)

    if ctx.read_only:
        return "Not undone: tux is in read-only mode.\n" + plan.render(), True
    needs_root = any(s.startswith("sudo ") for s in plan.steps + plan.follow_up) or (
        entry.path is not None and not os.access(entry.path if os.path.exists(entry.path)
                                                 else os.path.dirname(entry.path), os.W_OK))
    ok, note = ctx.approver.approve_command(plan.render(), f"Undo change #{entry.id}",
                                            Verdict(Risk.CHANGE, "reverts an earlier change", needs_root))
    if not ok:
        return f"User declined the undo.{' Their reason: ' + note if note else ''}", True
    result = j.undo(entry.id, ctx.runner, force=force)
    return result.message, not result.ok


HANDLERS = {
    "run_command": run_command,
    "hardware_scan": hardware_scan,
    "read_file": read_file,
    "write_file": write_file,
    "search_logs": search_logs,
    "remember": remember,
    "undo_change": undo_change,
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
