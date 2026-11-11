"""Semantic retrieval over a rolling view of the original chat log."""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import sqlite3
from contextlib import closing
from dataclasses import dataclass

import numpy as np

from ouroboros.chat_embeddings import DIMENSIONS, MODEL_REVISION, E5Encoder, prepare_model
from ouroboros.chat_records import chat_direction, chat_message_id, chat_message_label, owner_chat_id
from ouroboros.memory import Memory

CHAT_WINDOW = 1000
_ONLINE_BATCH_LIMIT = 32
_PASSAGE_CHARS = 600
_PASSAGE_STEP = 500


@dataclass(frozen=True)
class Passage:
    key: bytes
    row_index: int
    text: str


def _model_dir(drive_root: pathlib.Path) -> pathlib.Path:
    return drive_root / "models" / "e5-small"


def _passages(rows: list[dict]) -> list[Passage]:
    passages = []
    for index, row in enumerate(rows):
        text = str(row.get("text") or "").strip()
        if not text or chat_direction(row) == "system":
            continue
        for start in range(0, len(text), _PASSAGE_STEP):
            part = text[start:start + _PASSAGE_CHARS]
            passages.append(Passage(hashlib.sha256(part.encode("utf-8")).digest(), index, part))
            if start + _PASSAGE_CHARS >= len(text):
                break
    return passages


def _vectors(
    drive_root: pathlib.Path, chat_id: int, encoder: E5Encoder, passages: list[Passage],
    *, limit: int | None,
) -> tuple[dict[bytes, np.ndarray], int]:
    # Inference runs outside the write transaction. Simultaneous readers may compute
    # the same vector and can both safely publish it.
    wanted = {part.key: part.text for part in reversed(passages)}
    directory = drive_root / "state" / "chat-search"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    database = directory / f"e5-v1-{MODEL_REVISION[:12]}.sqlite3"
    with closing(sqlite3.connect(database, timeout=5)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS vectors (chat_id INTEGER, digest BLOB, vector BLOB, "
            "PRIMARY KEY (chat_id, digest)) WITHOUT ROWID"
        )
        cached = dict(conn.execute("SELECT digest, vector FROM vectors WHERE chat_id=?", (chat_id,)))
        vectors = {
            key: np.frombuffer(blob, dtype="<f4") for key, blob in cached.items()
            if key in wanted and len(blob) == DIMENSIONS * 4
        }
        missing = [key for key in wanted if key not in vectors][:limit]
        if missing:
            vectors.update(zip(missing, encoder.encode([wanted[key] for key in missing])))
        stale = cached.keys() - wanted.keys()
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO vectors VALUES (?, ?, ?)",
                [(chat_id, key, vectors[key].tobytes()) for key in missing],
            )
            conn.executemany("DELETE FROM vectors WHERE chat_id=? AND digest=?",
                             [(chat_id, key) for key in stale])
    return vectors, len(wanted)


def _rows(drive_root: pathlib.Path, chat_id: int) -> list[dict]:
    return Memory(drive_root).read_jsonl_tail("chat.jsonl", CHAT_WINDOW, chat_id=chat_id)


def search_chat(
    drive_root: pathlib.Path, chat_id: int, query: str, top_k: int, *,
    history_root: pathlib.Path | None = None,
) -> str:
    history_root = history_root or drive_root
    rows = _rows(history_root, chat_id)
    passages = _passages(rows)
    if not passages:
        return "Semantic chat search: no messages in the current chat window."
    encoder = E5Encoder(_model_dir(drive_root))
    vectors, total = _vectors(history_root, chat_id, encoder, passages, limit=_ONLINE_BATCH_LIMIT)
    keys = list(vectors)
    scores = np.stack([vectors[key] for key in keys]) @ encoder.encode([query[:2000]], query=True)[0]
    by_key = dict(zip(keys, scores))
    best: dict[int, tuple[float, str]] = {}
    for part in passages:
        score = float(by_key.get(part.key, -1))
        if score > best.get(part.row_index, (-1, ""))[0]:
            best[part.row_index] = (score, part.text)
    matches = sorted(best, key=lambda index: (best[index][0], index), reverse=True)
    lines = [f"Semantic chat search | {len(rows)} recent records | open with chat_history(message_id=…)."]
    if len(vectors) < total:
        lines.append(f"Partial index ({len(vectors)}/{total} passages). Results cover the indexed passages.")
    seen: set[str] = set()
    for index in matches:
        score, text = best[index]
        row = rows[index]
        message_id = chat_message_id(row)
        if message_id in seen:
            continue
        excerpt = " ".join(text.split())[:155]
        lines.append(f"- {message_id} | {chat_message_label(row)} | {score:.3f} | {excerpt}")
        seen.add(message_id)
        if len(seen) == min(top_k, 8):
            break
    return "\n".join(lines)


def _main() -> None:
    parser = argparse.ArgumentParser(description="Prepare the local model and pre-index the owner's recent chat.")
    parser.add_argument("--drive-root", type=pathlib.Path, required=True)
    parser.add_argument("--chat-id", type=int, help="Defaults to owner_chat_id in state/state.json")
    args = parser.parse_args()
    chat_id = args.chat_id or owner_chat_id(args.drive_root)
    if chat_id is None:
        parser.error("Set --chat-id or use a drive with an owner chat.")
    model = _model_dir(args.drive_root)
    prepare_model(model)
    passages = _passages(_rows(args.drive_root, chat_id))
    if not passages:
        print("Model ready; no dialogue passages in this chat yet.")
        return
    vectors, _ = _vectors(args.drive_root, chat_id, E5Encoder(model), passages, limit=None)
    print(f"Model ready; {len(vectors)} dialogue passages indexed locally.")


if __name__ == "__main__":
    _main()
