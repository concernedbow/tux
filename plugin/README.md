<img src="assets/icon.png" alt="" width="96" align="right">

# tux: Linux troubleshooting agent

tux diagnoses and fixes problems on your Linux machine. Describe the problem ("my webcam is black", "wifi
keeps dropping", "why is boot so slow?"), and tux inspects your system, finds the cause, explains it,
and fixes it once you approve each change.

## Use it

```bash
claude --agent tux:tux                    # a tux session
claude --agent tux:tux "is my SSD healthy?"
```

In any Claude Code session:
- `/tux:doctor`: full health check
- `/tux:undo`: revert a change tux made
- `/tux:monitor`: opt-in background monitoring

## What it does

- **Diagnoses** 14 areas (storage, memory, CPU and thermals, GPU, network, audio, camera, USB, Bluetooth,
  battery, display, boot, packages) with curated read-only checks, plus logs and configs.
- **Fixes** with your approval. Every command that changes your system needs your OK. A few destructive
  commands (disk wipes, deleting system directories) are blocked outright.
- **Undo**: every file edit and package or service change is recorded and can be reversed.
- **Background monitoring** (off by default): a local systemd timer checks for new problems and shows a
  desktop notification. It uses no AI and no network.
- Works with apt, dnf, pacman and zypper, plus snap and flatpak.

## Settings

**Auto-approve read-only commands** (off by default). When it's off, Claude Code asks before each new
kind of inspection command, and you can choose "don't ask again". Turn it on (`/config`, or
`claude plugin configure tux@tux`) to let tux run commands that only *read* system state (lspci,
journalctl, df, systemctl status…) without asking. Commands that change anything always ask, whatever
this setting says.

## Data and privacy

The tux project has **no servers and collects nothing**. To diagnose problems, what tux reads (your
messages, command output, log excerpts, and contents of files it reads or edits) is sent to **Anthropic**
through Claude Code, under your Claude account, along with any web searches tux makes to look up errors.
tux declares no connectors and sends your data nowhere else. Two diagnostic checks contact outside
services without sending personal data. The `network` scan pings `1.1.1.1` (Cloudflare) and looks up
`example.com` to test connectivity. On Fedora, Arch and openSUSE, the `packages` scan refreshes repository
metadata from your distro's mirrors to check for updates. Both are part of `/tux:doctor`. The background
monitor sends nothing anywhere. tux
never reads SSH keys, keyrings, password stores, browser logins or `.env` files. Everything it stores
(notes, the undo journal and its backups, monitor history) stays on your machine, readable only by your
account.

Full policy: [PRIVACY.md](https://github.com/concernedbow/tux/blob/main/PRIVACY.md). Source, issues and
the standalone terminal app: [github.com/concernedbow/tux](https://github.com/concernedbow/tux).

License: MIT
