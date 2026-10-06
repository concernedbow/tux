# Security policy

tux runs commands on people's own machines, so security bugs matter here more than in most projects.

## Reporting a vulnerability

**Please don't open a public issue.** Report it privately through GitHub instead:
[**Report a vulnerability**](https://github.com/concernedbow/tux/security/advisories/new). Only the maintainer
can see the report.

Include what you can:
- what an attacker could do, and what they need first (a malicious web page the agent reads, a crafted
  log line, local access…)
- steps or a command that reproduces it
- your tux version (`tux --version`, or `version` in the plugin's `plugin.json`) and distro

This is a personal open-source project maintained on a best-effort basis. I aim to acknowledge reports
within a week, and I'll keep you updated while a fix is prepared. Once it's released, you'll be credited
in the advisory unless you'd rather not be.

## What counts

Especially welcome:
- **Safety classifier bypasses**: a command that changes the system, or is destructive, but is classified
  as read-only (so it can be auto-approved), or that slips past the blocklist
- **Permission hook bypasses**: anything that makes tux run a change without the user's approval
- **Secret exposure**: ways to make tux read the files it's meant to refuse (SSH keys, keyrings, `.env`…),
  or to leak the undo journal and backups to other users
- **Prompt injection that leads to action**: content tux reads (logs, configs, web pages) that gets it to
  propose or run harmful commands in a way the approval prompt doesn't make clear
- **Undo or monitor flaws**: undo restoring the wrong content, or the monitor's timer or install path
  being abusable

Out of scope: problems that need the user to approve a clearly described harmful command; bugs in Claude
Code, the Claude API or your distro's tools themselves (report those upstream).

## Supported versions

Only the latest release gets security fixes. Upgrade with `claude plugin marketplace update tux`, or
reinstall with pipx at the newest tag.
