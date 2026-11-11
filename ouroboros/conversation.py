"""Conversation modes and the location of their histories and execution data."""

from pathlib import Path

WORK = "work"
PERSONAL = "personal"


def conversation_root(drive_root: Path, mode: str) -> Path:
    return drive_root / PERSONAL if mode == PERSONAL else drive_root
