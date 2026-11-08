"""Revision-checked writes for the two live Markdown memory files."""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
from contextlib import contextmanager
from hashlib import sha256

from ouroboros.utils import utc_now_iso

MEMORY_TOOLS = {"identity.md": "update_identity", "scratchpad.md": "update_scratchpad"}


class MemoryWriteRefused(ValueError):
    """The current file was preserved."""


def journal_path(path: pathlib.Path) -> pathlib.Path:
    return path.with_name(f"{path.stem}_journal.jsonl")


def protected_memory_tool(root: pathlib.Path, target: pathlib.Path) -> str | None:
    for filename, tool in MEMORY_TOOLS.items():
        path = (root / "memory" / filename).resolve()
        if target in (path, journal_path(path).resolve()):
            return tool
    return None


@contextmanager
def _publication_lock(path: pathlib.Path):
    # The OS releases this lock if the writer exits. Leave the lock file in place
    # so waiting writers always lock the same file.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def _replace_bytes(path: pathlib.Path, data: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        pathlib.Path(name).unlink(missing_ok=True)


def initialize_memory(path: pathlib.Path, default: str) -> None:
    if path.exists():
        return
    with _publication_lock(path):
        if not path.exists():
            _replace_bytes(path, default.encode("utf-8"))


def _journal_before_replace(path: pathlib.Path, record: dict) -> None:
    """Keep both texts even if the following replacement fails."""
    row = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "a+b") as stream:
        if os.name != "nt":
            os.fchmod(stream.fileno(), 0o600)
        end = stream.seek(0, os.SEEK_END)
        if end:
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                # A previous process may have stopped partway through its row.
                row = b"\n" + row
        stream.write(row)
        stream.flush()
        os.fsync(stream.fileno())
        stream.seek(end)
        if stream.read(len(row)) != row:
            raise OSError("Memory journal verification failed")


def publish_memory_file(
    path: pathlib.Path, content: str, expected_revision: str | None, *, task_id: str | None = None,
) -> tuple[str, int]:
    """Return the published SHA-256 and the previous length in characters."""
    with _publication_lock(path):
        try:
            old_bytes = path.read_bytes()
        except FileNotFoundError:
            old_bytes = None
        revision = sha256(old_bytes).hexdigest() if old_bytes is not None else ""
        if old_bytes is not None and expected_revision is None:
            raise MemoryWriteRefused(f"Revision required. Read {path.name} with drive_read first.")
        if (expected_revision or "") != revision:
            raise MemoryWriteRefused(f"Revision conflict. Read {path.name} again and reconcile your change.")

        old = old_bytes.decode("utf-8") if old_bytes is not None else ""
        new_bytes = content.encode("utf-8")
        if new_bytes == old_bytes:
            return revision, len(old)
        new_revision = sha256(new_bytes).hexdigest()
        _journal_before_replace(journal_path(path), {
            "type": "before_replace",
            "ts": utc_now_iso(),
            "task_id": task_id,
            "old_sha256": revision,
            "new_sha256": new_revision,
            "old_content": old,
            "new_content": content,
        })
        _replace_bytes(path, new_bytes)
        return new_revision, len(old)
