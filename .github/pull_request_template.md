## What this changes

<!-- One focused change. Link the issue if there is one. -->

## How I tested it

<!-- Tests added/updated, and any manual runs (distro, plugin or API mode). -->

## Checklist

- [ ] Tests pass (`pytest`)
- [ ] New read-only commands in `safety.py` have tests for both their safe and dangerous forms
- [ ] Anything that contacts the network is labeled in the scan output and declared in PRIVACY.md and the READMEs
- [ ] Changes to the system still go through approval and the undo journal
- [ ] Nothing that's only needed for development was added under `plugin/`
