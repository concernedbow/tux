# Contributing to tux

Thanks for helping! Bug reports, new diagnostics, and distro support are especially welcome.

## Set up

```bash
git clone https://github.com/concernedbow/tux && cd tux
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                                   # run the tests
claude --plugin-dir ./plugin --agent tux:tux       # try your plugin changes in Claude Code
```

## Where things live

- `plugin/`: the Claude Code plugin. **It ships to users exactly as is**, so keep only runtime files here.
  - `plugin/tux/`: the core, shared with the terminal app: `safety.py` (command classifier),
    `sysinfo.py` (diagnostic scans), `journal.py` (undo), `monitor.py` (background checks)
  - `agents/`, `skills/`, `hooks/`, `scripts/guard.py`, `bin/`: the agent, slash commands, permission
    hook and helpers
- `app/tux_app/`: the standalone terminal app on the Claude API (`tux --api`)
- `tests/`: the pytest suite. It stays out of `plugin/`.

## Ground rules

tux runs commands on real machines, so a few things are non-negotiable:

- **The safety classifier stays conservative.** A command is read-only only if every part of it is
  provably harmless. Every new read-only command, or argument check, in `plugin/tux/safety.py` needs a
  test that it's read-only, plus a test that its dangerous forms are classified as changes.
- **Nothing changes the system without approval.** Changes must go through the permission prompt (in
  plugin mode) or the approver (in API mode), and must be recorded in the journal so they can be undone.
- **Declare anything that touches the network.** If a diagnostic contacts an outside service, label it in
  the scan output and add it to [PRIVACY.md](PRIVACY.md) and both READMEs.
- **Never read secrets.** Don't loosen `SENSITIVE_PATHS` without discussing it in an issue first.
- **Background monitoring stays opt-in.** Nothing may enable it on the user's behalf.

## Pull requests

- Keep each PR focused on one thing, and describe how you tested it. Mention your distro if it's relevant.
- CI (pytest on Python 3.10, 3.12 and 3.13) must pass before a PR can merge.
- Don't bump versions in a PR. Releases are cut separately.

## Releasing (maintainer)

1. Bump `version` in `plugin/.claude-plugin/plugin.json` and `__version__` in `plugin/tux/__init__.py`.
   Installed plugins only update when the `plugin.json` version changes.
2. Add a `CHANGELOG.md` entry, and update the pinned `pipx install …@vX.Y.Z` line in `README.md`.
3. Commit, push, then tag `vX.Y.Z` and push the tag. Release tags are protected and can't be moved or
   deleted, so double-check before you push.

## Security issues

Please report them privately. See [SECURITY.md](SECURITY.md).
