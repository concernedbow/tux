# Changelog

## 0.1.0 (first release)

- Linux troubleshooting agent for Claude Code (`claude --agent tux:tux`), plus a standalone terminal app
  that uses the Claude API (`tux --api`)
- Curated diagnostics for 14 areas (`tux-scan`), per-machine notes, and `/tux:doctor` health checks
- Safety: read-only commands run without asking, changes need approval, destructive commands are
  blocked, and secret files are never read
- Undo: every file edit and package or service change is recorded and can be reversed (`/tux:undo`)
- Opt-in background monitoring with desktop notifications (`/tux:monitor on`); off by default
