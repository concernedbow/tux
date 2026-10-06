---
name: doctor
description: Run a full health check of this Linux machine and report problems, worst first, with proposed fixes.
when_to_use: When the user asks for a health check, a checkup, "is anything wrong with my system", or why their computer feels slow, unstable or broken without naming a specific cause.
argument-hint: "[area to focus on]"
context: fork
agent: tux:tux
background: false
---

Run a full health check of this machine. $ARGUMENTS

1. Run `tux-snapshot`, then `tux-scan overview storage memory cpu gpu network audio boot packages`
   (add `battery` if this is a laptop, and give any area the user named extra attention).
2. Dig into anything suspicious with follow-up commands: logs, configs, package state.
3. Report:
   - **Problems**, worst first, each with the evidence and a concrete fix.
   - **Warnings** worth keeping an eye on.
   - **Healthy**: one line on what looks fine.
4. Don't apply fixes during the checkup. List them and offer to apply them.
