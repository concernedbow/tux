# Privacy

tux is an open-source tool that runs on your own computer. **The tux project has no servers and collects
nothing**: no telemetry, no analytics, no accounts. This page explains what data leaves your machine
when you use tux, where it goes, and what tux stores locally.

## What is sent, and where

tux works by letting Claude inspect your system. Whatever tux reads to diagnose a problem becomes part
of the conversation with Claude and is sent to **Anthropic**: your messages, command output (for example
`lspci`, `journalctl`, `df`), excerpts of config files and logs, and file contents it reads or edits.

| How you run tux | Where that data goes | Governed by |
|---|---|---|
| Claude Code plugin (`claude --agent tux:tux`, `/tux:…`) | Anthropic, through Claude Code, under your Claude account | Your Claude plan's terms and [Anthropic's Privacy Policy](https://www.anthropic.com/legal/privacy) |
| `tux --api` (terminal app with an API key) | Anthropic's API, using your API key | [Anthropic's Commercial Terms](https://www.anthropic.com/legal/commercial-terms) and [Privacy Policy](https://www.anthropic.com/legal/privacy) |
| Web search (to look up error messages and known bugs) | Search queries Claude writes. These go through Claude Code's web search in plugin mode, or Anthropic's web search tool in API mode (unless `--no-web`). | Same as above |
| Background monitor (`tux-monitor`) | **Nowhere.** Checks run locally, and notifications are local desktop notifications | n/a |

tux declares no connectors or MCP servers, and it never sends your data to any other service.

## Network checks that contact other services

A few diagnostic checks contact outside services to test your network or look for updates. They send
no personal data beyond what any network request reveals (your IP address, and to your DNS resolver
the name being looked up):

| Check | When it runs | What it contacts |
|---|---|---|
| Connectivity test | The `network` scan (`tux-scan network`, and `/tux:doctor`) | Two ping packets to `1.1.1.1` (Cloudflare) and a DNS lookup of `example.com` through your configured DNS resolver |
| Update check | The `packages` scan (`tux-scan packages`, and `/tux:doctor`) on Fedora/RHEL (`dnf check-update`), Arch (`checkupdates`) and openSUSE (`zypper list-updates`) | Your distribution's configured package mirrors, to refresh repository metadata, just like a normal update check. On Debian and Ubuntu, `apt list --upgradable` reads the local cache and contacts nothing. |

Commands that tux proposes during a fix, such as installing a package, can also contact your package
mirrors. Those commands always need your approval first.

## What tux won't read

tux refuses to read files that typically hold secrets: SSH private keys, GnuPG keyrings,
`/etc/shadow`, password stores and keyrings, browser saved logins and cookies, cloud credentials, and
`.env` files. Command output can still contain personal details such as your username, hostname, device
serial numbers, Wi-Fi network names and IP addresses. Don't use tux on a machine where sharing that with
Anthropic isn't acceptable.

## What tux stores on your computer

All of this stays local and is readable only by your user account:

| Path | Contents |
|---|---|
| `~/.config/tux/notes.md` | Facts tux saved about your machine (hardware quirks, past fixes) |
| `~/.local/state/tux/journal.jsonl` and `backups/` | The change journal and copies of files from before tux edited them, used by undo. These copies can include sensitive configuration. |
| `~/.local/state/tux/monitor/` | Background monitor state and history (only if you turned monitoring on) |
| `~/.config/systemd/user/tux-monitor.*`, `~/.local/share/tux/monitor/` | The monitor's timer and program files (only while monitoring is on) |

To delete everything: run `tux-monitor disable --purge`, then
`rm -rf ~/.config/tux ~/.local/state/tux ~/.local/share/tux`.

## Contact

Questions or concerns: [open an issue](https://github.com/concernedbow/tux/issues).
