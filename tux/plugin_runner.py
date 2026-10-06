"""Shell runner for the Claude Code plugin's helpers: root steps go through tux-sudo's graphical prompt."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"


def run(command: str, timeout: int = 600) -> tuple[int, str]:
    if command.startswith("sudo "):
        command = f"{shlex.quote(str(BIN / 'tux-sudo'))} {command[5:]}"
    try:
        p = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL,
                           env={**os.environ, "DEBIAN_FRONTEND": "noninteractive", "SYSTEMD_PAGER": ""})
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout}s"
