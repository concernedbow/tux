# Changelog

## 0.1.1

- Read-only commands are auto-approved only if you turn on the plugin's "Auto-approve read-only
  commands" setting (off by default); otherwise Claude Code's normal permission prompts apply
- Network requests (`curl`) always need approval
- The plugin now ships from `plugin/` and contains only what it runs; tests and the API-key terminal
  app (`tux_app`) live outside it
- Plugin README with what it does and what data it sends
- Icon re-encoded as a clean, standard PNG

## 0.1.0 (first release)

- Linux troubleshooting agent for Claude Code (`claude --agent tux:tux`), plus a standalone terminal app
  that uses the Claude API (`tux --api`)
- Curated diagnostics for 14 areas (`tux-scan`), per-machine notes, and `/tux:doctor` health checks
- Safety: changes need approval, destructive commands are blocked, and secret files are never read.
  Read-only commands can be auto-approved through an opt-in setting (off by default).
- Undo: every file edit and package or service change is recorded and can be reversed (`/tux:undo`)
- Opt-in background monitoring with desktop notifications (`/tux:monitor on`); off by default
