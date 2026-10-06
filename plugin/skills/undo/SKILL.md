---
name: undo
description: Undo a change tux made to this machine (a config edit, package install or removal, or service change), or list what tux has changed.
when_to_use: When the user says undo, revert, roll back, put it back, or that a fix tux applied made things worse.
argument-hint: "[change id | list]"
---

The user wants to undo something tux changed. Argument: "$ARGUMENTS" (empty means the most recent change).

1. If the argument is `list` or empty, run `tux-undo list` first and identify the change. If several
   recent changes could be meant, ask which one.
2. Run `tux-undo show <ID>` and explain in one or two sentences what undoing it will do, including any
   notes (for example a package that was already installed stays installed).
3. Run `tux-undo <ID>`. The user approves it in the permission prompt.
4. Report the result. If the original problem comes back, offer to diagnose it differently.

If tux-undo refuses because newer changes touched the same file, undo those first (newest first). If it
refuses because the file was edited since, explain that and only retry with `--force` if the user agrees
to lose those edits. If a change can't be undone automatically, explain what it did and propose a
manual reversal, as a normal change for the user to approve.
