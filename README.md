<img src="assets/icon.png" alt="" width="96" align="right">

# tux 🐧

**An AI troubleshooting agent for your Linux machine.** Describe a problem in plain English ("my
webcam is black", "wifi drops every few minutes", "why is boot so slow?"). tux inspects your system,
finds the cause, explains it, and fixes it once you approve each change.

An illustrative session:

```
› my bluetooth headphones keep cutting out

  ▸ tux-snapshot
  ▸ tux-scan bluetooth audio
  ▸ journalctl -b -u bluetooth -g "firmware|reset" --no-pager

Your Intel AX211 adapter is hitting a known firmware bug ("Missing completion reports"), and
PipeWire falls back to the low-quality HSP profile after each reconnect. Two fixes:

1. Update the adapter firmware: `tux-sudo apt install --only-upgrade linux-firmware`
2. Pin the A2DP profile so it doesn't fall back...
```

## What it does

- **Answers questions** about your system: "which GPU driver am I using?", "what's eating my disk?"
- **Diagnoses hardware**: curated checks for storage (SMART/NVMe health, I/O errors), memory (OOM kills, ECC),
  CPU and thermals, GPU and drivers, network, audio (PipeWire/ALSA/SOF), cameras (UVC/IPU6/libcamera),
  USB, Bluetooth, battery, displays, and boot.
- **Troubleshoots**: reads logs, configs, and package state, and searches the web for error strings and
  known bugs in your exact versions.
- **Fixes things, with your approval**: installs packages, edits configs (backing them up first), and
  restarts services, then checks that the fix worked.
- **Undoes its own changes**: every config edit, package install or removal, and service change is
  recorded, and `/tux:undo` (or just "undo that") reverses it. See [Undo](#undo).
- **Watches your machine, if you want it to** (opt-in): background health checks with a desktop
  notification when something new goes wrong. See [Background monitoring](#background-monitoring-opt-in).
- **Remembers your machine**: hardware quirks and past fixes are saved to `~/.config/tux/notes.md`, so the
  next session starts with them.

Works on any distro: apt, dnf, pacman, and zypper systems, plus snap and flatpak.

## Install

### Option A: Claude Code (recommended, no API key)

tux runs as a [Claude Code](https://claude.com/claude-code) plugin, so it uses your existing
Claude subscription.

```bash
claude plugin marketplace add concernedbow/tux
claude plugin install tux@tux
```

Then start a tux session:

```bash
claude --agent tux:tux                          # interactive
claude --agent tux:tux "why is my fan so loud?"  # start with a question
```

Inside any Claude Code session you can also run `/tux:doctor` for a full health check, `/tux:undo` to
revert a change, `/tux:monitor` for background monitoring, or just ask: *"use tux to figure out why my wifi keeps dropping"*.

Optionally, add a shortcut to your shell profile: `alias tux='claude --agent tux:tux'`

### Option B: the `tux` command

```bash
pipx install git+https://github.com/concernedbow/tux@v0.1.0
tux                   # interactive
tux doctor            # full health check
/undo, /changes       # inside the app: revert a change, list what tux changed
tux monitor on        # opt-in background monitoring (see below)
tux "is my SSD healthy?"
tux --read-only ...   # diagnose only, never change anything
```

If Claude Code is installed, `tux` opens it with the tux agent and installs the plugin on first run.
If it isn't, `tux` runs its own terminal app on the Anthropic API. That needs an API key
(`export ANTHROPIC_API_KEY=sk-ant-...`), and usage is billed per token. Pass `--api` to use API mode
even when Claude Code is installed.

## Safety model

tux runs commands on your real machine, so every command is classified before it runs:

| Kind | Examples | What happens |
|---|---|---|
| **Read-only** | `lspci`, `journalctl`, `systemctl status`, `ip addr`, `smartctl -H`, `dpkg -l` | Runs immediately |
| **Change** | `apt install`, `systemctl restart`, editing `/etc/...`, anything with `sudo` | Shown to you first; runs only if you approve |
| **Blocked** | `rm -rf /`, `mkfs`, `dd of=/dev/sda`, `curl … \| sh`, removing the kernel or `sudo` | Never runs |

The classifier ([`tux/safety.py`](tux/safety.py)) is conservative. A command counts as read-only only if
every part of the pipeline is on an allowlist with safe arguments. Anything unrecognised needs your
approval. Output redirection, `$(...)`, `sed -i`, and `find -delete` all count as changes.

Other safeguards:
- **Secrets stay private**: tux refuses to read SSH keys, password stores, browser credentials, or `.env` files.
- **Undo**: every change is journaled with what's needed to reverse it (see below).
- **Root access** goes through a graphical password prompt (`tux-sudo`), so you can see each root command
  before you allow it. On a headless machine, tux asks you to run root commands yourself with `! sudo …`.
- **Scoped**: the plugin's permission hook applies only in tux sessions. Your other Claude Code sessions behave as before.

## Data and privacy

The tux project has **no servers and collects nothing**: no telemetry, no analytics. To diagnose
problems, tux sends what it reads (your messages, command output, log excerpts, and contents of
files it reads or edits) to **Anthropic**:

- **Claude Code plugin:** through Claude Code, under your Claude account and plan terms.
- **`tux --api`:** directly to Anthropic's API with your key. Web search queries go through Anthropic's
  search tool unless you pass `--no-web`.
- **Background monitor:** sends nothing anywhere. It's entirely local.

tux never reads SSH keys, keyrings, password stores, browser logins or `.env` files. Command output can
still include details like your hostname, serial numbers and network names. Everything tux stores
(notes, the undo journal and backups, monitor history) stays on your machine, readable only by your
account. Full details, including how to delete it all: [PRIVACY.md](PRIVACY.md).

## Undo

tux records every change it makes in a journal (`~/.local/state/tux/journal.jsonl`):

- **File edits** keep the previous content. Undo restores it, or deletes the file if tux created it.
  Ownership and permissions are kept.
- **Package and service changes** are checked before and after they run. The undo is worked out from
  what actually changed: undoing `apt install vim curl` removes only the packages that weren't already
  installed, and undoing `systemctl enable --now tlp` disables and stops tlp only if it wasn't already
  enabled and running. Supports apt, dnf, yum, zypper, pacman, snap, flatpak, apt-mark holds, and systemctl
  enable/disable/start/stop/mask/unmask.

Before anything is reverted, tux shows exactly what will happen (the diff or the commands) and asks for
approval. It also refuses unsafe undos:
- It won't overwrite a file you edited after tux changed it (unless you confirm with `--force`).
- It won't undo an older edit while a newer edit to the same file is still in place.
- Commands it can't reverse reliably, such as pipelines or custom scripts, are reported as manual,
  never guessed.

If a later command rebuilt files from that config (`update-grub`, `update-initramfs`, …), tux re-runs it
after restoring the file. Undos are journaled too, so an undo can be undone.

```bash
tux-undo list        # in Claude Code the agent runs these for you
tux-undo show 3      # what undoing #3 would do (changes nothing)
tux-undo 3           # undo it (you approve it in the permission prompt)
```

## Background monitoring (opt-in)

Background monitoring is **off by default**, and tux never turns it on for you. Turn it on with
`/tux:monitor on` in Claude Code, or `tux monitor on` / `tux-monitor enable`:

```bash
tux-monitor enable --every 6h   # 1h, 6h (default), 12h or 1d
tux-monitor status              # on/off, last check, active findings
tux-monitor report              # findings plus recent history
tux-monitor disable             # removes everything it installed (--purge also deletes history)
```

It installs a systemd user timer that runs a quick local check: disk space and inodes, failed services,
drive health (SMART via udisks, no root needed), new kernel storage and hardware errors, OOM kills, GPU
hangs, overheating, and battery wear. The checks are plain local commands, with **no AI, no network and no
usage**.

You get a desktop notification only when something **new** goes wrong. An ongoing problem is reported
once, and resolutions are logged. Active findings show up at the start of your next tux session, so
you can just ask about them. Turning it on or off is recorded in the undo journal like any other change.

## How it works

```
.claude-plugin/     plugin + marketplace manifests
agents/tux.md       the troubleshooting agent (Claude Code mode)
skills/             /tux:doctor health check, /tux:undo, /tux:monitor
hooks/hooks.json    → scripts/guard.py: allow / ask / deny each command, journal changes
bin/                tux-scan, tux-snapshot, tux-note, tux-backup, tux-undo, tux-monitor, tux-sudo
tux/                Python package: safety classifier, diagnostics, change journal + undo, monitor,
                    API-mode agent + terminal app
```

Both modes share the same safety classifier and diagnostics. In API mode, tux runs its own agent loop
on the Claude API with tools for commands, diagnostics, log search, file edits, web search, and notes.

## Development

```bash
git clone https://github.com/concernedbow/tux && cd tux
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
claude --plugin-dir . --agent tux:tux   # try local plugin changes
```

Contributions welcome, especially new diagnostic areas in `tux/sysinfo.py` and read-only commands in
`tux/safety.py` (each new one needs a test).

## License

MIT
