---
name: monitor
description: Turn tux's background health monitoring on or off, or check its status and findings.
argument-hint: "[on | off | status | report] [--every 1h|6h|12h|1d]"
disable-model-invocation: true
---

The user invoked tux's background monitoring command with: "$ARGUMENTS" (empty means status).

Background monitoring is strictly opt-in. Only turn it on when the arguments say `on`/`enable`.

- **status** (or empty): run `tux-monitor status` and summarize it. If it's off, explain in two
  sentences what it does (below) and that `/tux:monitor on` turns it on.
- **on** / **enable**: first tell the user what it will do. Then run `tux-monitor enable` (add
  `--every <interval>` if they gave one; the default is every 6h). They approve it in the permission prompt.
  What it does:
  - Installs a systemd user timer (`~/.config/systemd/user/tux-monitor.timer`) that runs a quick local
    check: disk space, failed services, drive health (SMART), kernel storage and hardware errors, OOM kills,
    GPU hangs, overheating, and battery wear.
  - Runs locally only: no AI, no network, no usage. A desktop notification appears only when something
    *new* goes wrong. Findings show up at the start of the next tux session.
  - Turn it off any time with `/tux:monitor off`. That removes everything it installed.
- **off** / **disable**: run `tux-monitor disable` (add `--purge` only if they want the history deleted too).
- **report**: run `tux-monitor report`. For each active finding, offer to investigate it.
