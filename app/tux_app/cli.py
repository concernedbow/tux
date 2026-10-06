"""Terminal front end for tux."""

from __future__ import annotations

import argparse
import os
import sys

import anthropic
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax
from rich.text import Text

from tux import __version__, journal, monitor, notes, sysinfo
from tux.safety import Verdict

from . import prompts, tools
from .agent import DEFAULT_MODEL, Agent

console = Console()

HELP = """\
[bold]Commands[/bold]
  /doctor         full system health check
  /scan <area>    run a diagnostic scan yourself (no AI): {areas}
  /notes          show what tux remembers about this machine
  /changes        list changes tux has made (alias: /log)
  /undo [id]      undo a change (default: the most recent one), after showing what it will do
  /monitor [on|off|status|report] [--every 1h|6h|12h|1d]
                  opt-in background health checks with desktop notifications (off by default)
  /clear          start a fresh conversation
  /help           this help
  /exit           quit (or Ctrl-D)
Anything else is sent to tux. Ctrl-C interrupts the current step.
"""


class TerminalView:
    def __init__(self, show_thinking: bool):
        self.show_thinking = show_thinking
        self._text = ""
        self._live: Live | None = None
        self._status = None

    # --- streaming -------------------------------------------------------
    def _stop_status(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    def thinking_started(self) -> None:
        self._end_text()
        if self.show_thinking:
            console.print(Text("thinking…", style="dim italic"))
        else:
            self._status = console.status("[dim]thinking…[/dim]", spinner="dots")
            self._status.start()

    def thinking_delta(self, text: str) -> None:
        if self.show_thinking:
            console.print(Text(text, style="dim italic"), end="")

    def text_delta(self, text: str) -> None:
        self._stop_status()
        if self._live is None:
            self._text = ""
            self._live = Live(Markdown(""), console=console, refresh_per_second=12, vertical_overflow="visible")
            self._live.start()
        self._text += text
        self._live.update(Markdown(self._text))

    def _end_text(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def block_done(self) -> None:
        self._stop_status()
        self._end_text()

    def finish(self) -> None:
        self.block_done()

    # --- tools -----------------------------------------------------------
    def tool_started(self, name: str, args: dict) -> None:
        self.block_done()
        label = {
            "run_command": lambda a: f"$ {a.get('command', '')}",
            "hardware_scan": lambda a: f"scanning {a.get('area', '')}",
            "read_file": lambda a: f"reading {a.get('path', '')}",
            "write_file": lambda a: f"writing {a.get('path', '')}",
            "search_logs": lambda a: "searching logs " + " ".join(f"{k}={v}" for k, v in a.items()),
            "remember": lambda a: f"remembering: {a.get('note', '')}",
            "undo_change": lambda a: f"undo: {a.get('action', '')} {a.get('change_id', 'last') if a.get('action') != 'list' else ''}",
        }.get(name, lambda a: name)(args)
        console.print(Text(f"  ▸ {label}", style="cyan"), highlight=False)

    def stream_line(self, line: str) -> None:
        console.print(Text("    " + line.rstrip("\n"), style="dim"), highlight=False)

    def tool_finished(self, name: str, result: str, is_error: bool) -> None:
        if is_error:
            first = result.strip().splitlines()[0] if result.strip() else "error"
            console.print(Text(f"    ✗ {first[:200]}", style="yellow"))

    def web_search(self, query: str) -> None:
        self.block_done()
        console.print(Text(f"  ▸ web search: {query}", style="magenta"))

    def notice(self, text: str) -> None:
        self.block_done()
        console.print(f"[yellow]{text}[/yellow]")


class TerminalApprover:
    def __init__(self, view: TerminalView, auto_yes: bool = False):
        self.view = view
        self.auto_yes = auto_yes

    def _ask(self) -> tuple[bool, str]:
        if self.auto_yes:
            return True, ""
        choice = Prompt.ask("  Run it? [bold]y[/bold]es / [bold]n[/bold]o / [bold]a[/bold]lways this session",
                            choices=["y", "n", "a"], default="y", show_choices=False)
        if choice == "n":
            reason = Prompt.ask("  Why not? (optional, helps tux adjust)", default="", show_default=False)
            return False, reason
        return True, "always" if choice == "a" else ""

    def approve_command(self, command: str, purpose: str, verdict: Verdict) -> tuple[bool, str]:
        self.view.block_done()
        body = Text()
        body.append(command + "\n\n", style="bold")
        body.append(purpose, style="")
        body.append(f"\n({verdict.reason})", style="dim")
        title = "[red]needs root[/red]" if verdict.needs_root else "[yellow]wants to change your system[/yellow]"
        console.print(Panel(body, title=title, title_align="left", border_style="yellow"))
        return self._ask()

    def approve_write(self, path: str, diff: str, purpose: str) -> tuple[bool, str]:
        self.view.block_done()
        console.print(Panel(Syntax(diff, "diff", theme="ansi_dark", word_wrap=True),
                            title=f"[yellow]edit {path}[/yellow]", subtitle=purpose, title_align="left",
                            border_style="yellow"))
        return self._ask()


def _show_changes() -> None:
    entries = journal.Journal().entries()[-30:]
    if not entries:
        console.print("[dim]No changes recorded yet.[/dim]")
        return
    for e in entries:
        console.print(journal.describe(e), highlight=False, markup=False)
    console.print(f"[dim]Journal: {journal.Journal().file}   Undo one with /undo <id>[/dim]")


def _undo(ctx: tools.ToolContext, ref: str) -> None:
    j = ctx.journal
    entry = j.resolve(ref or None)
    if entry is None:
        console.print("[yellow]" + ("No change with that id." if ref else "Nothing to undo.") + "[/yellow]")
        return
    force = False
    plan = j.plan(entry, lambda p: journal.read_with_root(p, ctx.runner))
    if plan.blocked and "--force" in plan.blocked:
        console.print(Panel(plan.render(), title="[yellow]modified since tux changed it[/yellow]", border_style="yellow"))
        force = Prompt.ask("  Restore anyway and discard those edits?", choices=["y", "n"], default="n") == "y"
        if not force:
            return
        plan = j.plan(entry, lambda p: journal.read_with_root(p, ctx.runner), force=True)
    if plan.blocked:
        console.print(Panel(plan.render(), title="[red]can't undo[/red]", border_style="red"))
        return
    console.print(Panel(Syntax(plan.render(), "diff", theme="ansi_dark", word_wrap=True),
                        title=f"[yellow]undo #{entry.id}[/yellow]", title_align="left", border_style="yellow"))
    if Prompt.ask("  Undo it?", choices=["y", "n"], default="y") != "y":
        return
    result = j.undo(entry.id, ctx.runner, force=force)
    console.print(f"[{'green' if result.ok else 'red'}]{result.message}[/]", highlight=False)


MONITOR_PITCH = """\
Background monitoring runs a quick local health check every {every}: disk space, failed services,
drive health (SMART), kernel storage/hardware errors, OOM kills, GPU hangs, overheating, battery wear.
It runs locally (no AI, no network, no usage) as a systemd user timer, and shows a desktop
notification only when something new goes wrong. Turn it off any time with /monitor off."""


def monitor_command(args: list[str], confirm: bool = True) -> None:
    """/monitor and `tux monitor`: enabling always needs an explicit yes from the user."""
    cmd = args[0] if args else "status"
    every = args[args.index("--every") + 1] if "--every" in args else monitor.DEFAULT_INTERVAL
    if cmd in ("on", "enable", "off", "disable"):
        turning_on = cmd in ("on", "enable")
        if turning_on:
            console.print(Panel(MONITOR_PITCH.format(every=every), title="tux background monitoring",
                                border_style="cyan"))
            if confirm and Prompt.ask("  Turn it on?", choices=["y", "n"], default="n") != "y":
                console.print("[dim]Left off.[/dim]")
                return
        command = f"tux-monitor {'enable' if turning_on else 'disable'}"
        j = journal.Journal()
        before = j.prober(command)
        try:
            msg = monitor.enable(every) if turning_on else monitor.disable(purge="--purge" in args)
        except (RuntimeError, ValueError) as e:
            console.print(f"[red]{e}[/red]")
            return
        entry = j.record_command(command, before, j.prober(command), 0, source="api")
        console.print(msg, highlight=False)
        if entry.inverse:
            console.print(f"[dim]Recorded as change #{entry.id} (/undo {entry.id} reverses it).[/dim]")
    elif cmd == "status":
        console.print(monitor.status(), highlight=False, markup=False)
    elif cmd == "report":
        console.print(monitor.report(), highlight=False, markup=False)
    elif cmd == "check":
        with console.status("[dim]checking…[/dim]"):
            result = monitor.check()
        console.print(f"{len(result.new)} new, {len(result.resolved)} resolved, {len(result.active)} active.")
        if result.active:
            console.print(monitor.format_findings(result.active), highlight=False, markup=False)
    else:
        console.print("[yellow]Usage: /monitor [on|off|status|report|check] [--every 1h|6h|12h|1d][/yellow]")


def _run_turn(agent: Agent, view: TerminalView, text: str) -> None:
    try:
        agent.ask(text)
    except KeyboardInterrupt:
        view.finish()
        agent.repair()
        console.print("[yellow]Interrupted.[/yellow]")
    except anthropic.AuthenticationError:
        view.finish()
        _auth_help()
        sys.exit(1)
    except anthropic.RateLimitError:
        view.finish()
        agent.repair()
        console.print("[yellow]Rate limited by the API. Wait a moment and try again.[/yellow]")
    except anthropic.APIConnectionError:
        view.finish()
        agent.repair()
        console.print("[red]Couldn't reach the Anthropic API. Check your internet connection.[/red]")
    except anthropic.APIStatusError as e:
        view.finish()
        agent.repair()
        console.print(f"[red]API error {e.status_code}: {e.message}[/red]")
    finally:
        view.finish()


def _auth_help() -> None:
    console.print(Panel(
        "tux needs an Anthropic API key.\n\n"
        "1. Create one at https://console.anthropic.com/settings/keys\n"
        "2. Add it to your shell profile:\n"
        "   [bold]export ANTHROPIC_API_KEY=sk-ant-...[/bold]\n\n"
        "Or log in with the Anthropic CLI: [bold]ant auth login[/bold]\n\n"
        "Have Claude Code? Install it and run [bold]tux[/bold] without --api to use your Claude subscription instead.",
        title="[red]Not authenticated[/red]", border_style="red"))


def _has_credentials() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    config = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "anthropic")
    return os.path.isdir(config)


MARKETPLACE = "concernedbow/tux"
PLUGIN_ID = "tux@tux"
READ_ONLY_NOTE = ("READ-ONLY MODE: the user started tux with --read-only. Do not change the system. Diagnose, "
                  "then list the exact commands you would run so the user can run them.")


def _plugin_installed() -> bool:
    import subprocess
    try:
        out = subprocess.run(["claude", "plugin", "list", "--json"], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return f'"{PLUGIN_ID}' in out or '"tux"' in out


def _install_plugin() -> bool:
    import subprocess
    console.print("[dim]First run: installing the tux plugin into Claude Code…[/dim]")
    for cmd in (["claude", "plugin", "marketplace", "add", MARKETPLACE], ["claude", "plugin", "install", PLUGIN_ID]):
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0 and "already" not in (p.stdout + p.stderr).lower():
            console.print(f"[red]`{' '.join(cmd)}` failed:[/red]\n{p.stdout}{p.stderr}")
            return False
    return True


def run_claude_code(args: argparse.Namespace, question: str) -> None:
    """Hand off to Claude Code running the tux agent, on the user's own Claude login."""
    cmd = ["claude", "--agent", "tux:tux"]
    plugin_dir = os.environ.get("TUX_PLUGIN_DIR")
    if plugin_dir:
        cmd += ["--plugin-dir", plugin_dir]
    elif not _plugin_installed() and not _install_plugin():
        console.print(f"Install it by hand with:\n  claude plugin marketplace add {MARKETPLACE}\n"
                      f"  claude plugin install {PLUGIN_ID}")
        sys.exit(1)
    if args.read_only:
        # dontAsk denies anything not pre-approved; the user asked for read-only, so let the hook
        # pre-approve inspection commands for this session (changes are still denied)
        os.environ["TUX_AUTO_APPROVE_READ_ONLY"] = "1"
        cmd += ["--permission-mode", "dontAsk", "--append-system-prompt", READ_ONLY_NOTE]
    if args.model != DEFAULT_MODEL:
        cmd += ["--model", args.model]
    if question:
        cmd.append("/tux:doctor" if question == "doctor" else question)
    os.execvp("claude", cmd)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="tux", description="An AI assistant that diagnoses and fixes problems on your Linux machine. "
        "Runs inside Claude Code (your Claude subscription) when it's installed, otherwise uses an "
        "Anthropic API key.")
    parser.add_argument("question", nargs="*", help="ask a one-off question ('doctor' runs a health check)")
    parser.add_argument("--api", action="store_true",
                        help="use the built-in terminal app with an Anthropic API key instead of Claude Code")
    parser.add_argument("--read-only", action="store_true", help="diagnose only; never change anything")
    parser.add_argument("--yes", action="store_true",
                        help="(API mode) approve every change without asking; blocked commands stay blocked")
    parser.add_argument("--model", default=os.environ.get("TUX_MODEL", DEFAULT_MODEL))
    parser.add_argument("--effort", default=os.environ.get("TUX_EFFORT", "high"),
                        choices=["low", "medium", "high", "xhigh", "max"], help="(API mode) reasoning effort")
    parser.add_argument("--no-web", action="store_true", help="(API mode) disable web search")
    parser.add_argument("--show-thinking", action="store_true", help="(API mode) print reasoning summaries")
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="(API mode) stay in interactive mode after answering a one-off question")
    parser.add_argument("--version", action="version", version=f"tux {__version__}")
    args = parser.parse_args(argv)
    question = " ".join(args.question).strip()

    if args.question and args.question[0] == "monitor":
        # local and instant in both modes; no need to start Claude
        monitor_command(args.question[1:])
        return

    import shutil
    if not args.api and shutil.which("claude"):
        run_claude_code(args, question)
        return
    run_api(args, question)


def run_api(args: argparse.Namespace, question: str) -> None:
    if not _has_credentials():
        _auth_help()
        sys.exit(1)

    view = TerminalView(show_thinking=args.show_thinking)
    ctx = tools.ToolContext(approver=TerminalApprover(view, auto_yes=args.yes), read_only=args.read_only,
                            on_line=view.stream_line)
    with console.status("[dim]looking at your system…[/dim]"):
        agent = Agent(view=view, ctx=ctx, model=args.model, effort=args.effort, web=not args.no_web)

    if question:
        _run_turn(agent, view, prompts.DOCTOR_PROMPT if question == "doctor" else question)
        if not args.interactive:
            return

    mode = " [red](read-only)[/red]" if args.read_only else ""
    if not question:
        console.print(f"[bold]tux[/bold] {__version__}{mode} — describe a problem or ask a question. "
                      "[dim]/help for commands[/dim]")

    while True:
        try:
            text = console.input("\n[bold green]›[/bold green] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if not text:
            continue
        if text.startswith("/"):
            cmd, _, rest = text[1:].partition(" ")
            if cmd in ("exit", "quit", "q"):
                return
            if cmd == "help":
                console.print(HELP.format(areas=", ".join(sysinfo.SCANS)))
            elif cmd == "clear":
                agent.reset()
                console.print("[dim]Conversation cleared.[/dim]")
            elif cmd == "notes":
                console.print(notes.load_notes() or "[dim]No notes yet.[/dim]", highlight=False)
                console.print(f"[dim]{notes.NOTES_FILE}[/dim]")
            elif cmd in ("changes", "log"):
                _show_changes()
            elif cmd == "monitor":
                monitor_command(rest.split())
            elif cmd == "undo":
                try:
                    _undo(ctx, rest.strip())
                except KeyboardInterrupt:
                    console.print("[yellow]Interrupted.[/yellow]")
            elif cmd == "scan":
                with console.status(f"[dim]scanning {rest or 'overview'}…[/dim]"):
                    out = sysinfo.scan(rest.strip() or "overview")
                console.print(out, highlight=False, markup=False)
            elif cmd == "doctor":
                _run_turn(agent, view, prompts.DOCTOR_PROMPT)
            else:
                console.print(f"[yellow]Unknown command /{cmd}. Try /help.[/yellow]")
            continue
        _run_turn(agent, view, text)


if __name__ == "__main__":
    main()
