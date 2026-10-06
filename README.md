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

Inside any Claude Code session you can also run `/tux:doctor` for a full health check, or just
ask: *"use tux to figure out why my wifi keeps dropping"*.

Optionally, add a shortcut to your shell profile: `alias tux='claude --agent tux:tux'`

### Option B: the `tux` command

```bash
pipx install git+https://github.com/concernedbow/tux
tux                   # interactive
tux doctor            # full health check
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
- **Backups**: configs are backed up before tux edits them.
- **Audit log**: every change tux makes is logged to `~/.local/state/tux/actions.log`.
- **Root access** goes through a graphical password prompt (`tux-sudo`), so you can see each root command
  before you allow it. On a headless machine, tux asks you to run root commands yourself with `! sudo …`.
- **Scoped**: the plugin's permission hook applies only in tux sessions. Your other Claude Code sessions behave as before.

**Privacy:** command output and log excerpts are sent to Claude to be analysed. Don't use tux on
machines where that isn't acceptable.

## How it works

```
.claude-plugin/     plugin + marketplace manifests
agents/tux.md       the troubleshooting agent (Claude Code mode)
skills/doctor/      /tux:doctor health check
hooks/hooks.json    → scripts/guard.py: allow / ask / deny each command, log changes
bin/                tux-scan, tux-snapshot, tux-note, tux-sudo (on the agent's PATH)
tux/                Python package: safety classifier, diagnostics, API-mode agent + terminal app
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
