"""Per-machine notes that persist between sessions (shared by the plugin and the API CLI)."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tux"
NOTES_FILE = CONFIG_DIR / "notes.md"


def load_notes() -> str:
    try:
        return NOTES_FILE.read_text().strip()
    except OSError:
        return ""


def add_note(note: str) -> Path:
    NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with NOTES_FILE.open("a") as f:
        f.write(f"- [{dt.date.today().isoformat()}] {note.strip()}\n")
    return NOTES_FILE
