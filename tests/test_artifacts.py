import json

from ouroboros.artifacts import list_incoming_artifacts, save_artifact, save_incoming_artifact


PLAN_TEXT = "# plan\n- step\n"


def test_save_artifact_persists_text_and_meta(tmp_path):
    meta = save_artifact(
        tmp_path,
        filename="plan.md",
        content=PLAN_TEXT,
        content_kind="plan",
        mime_type="text/markdown",
        source="test",
        task_id="task-1",
        chat_id=42,
    )
    assert isinstance(meta, dict)
    path = tmp_path / meta["relative_path"]
    assert path.exists()
    assert path.read_text(encoding="utf-8") == PLAN_TEXT
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    assert meta_path.exists()
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    assert payload["content_kind"] == "plan"
    assert payload["chat_id"] == 42


def test_save_incoming_artifact_and_list_latest(tmp_path):
    first = save_incoming_artifact(
        tmp_path,
        filename="archive.zip",
        data=b"zipdata",
        content_kind="incoming",
        mime_type="application/zip",
        chat_id=42,
        caption="",
        metadata={"message_id": 10},
    )
    second = save_incoming_artifact(
        tmp_path,
        filename="notes.txt",
        content="hello",
        content_kind="incoming",
        mime_type="text/plain",
        chat_id=42,
        caption="read this later",
        metadata={"message_id": 11},
    )

    assert isinstance(first, dict)
    assert isinstance(second, dict)
    listed = list_incoming_artifacts(tmp_path, limit=10, chat_id=42)
    assert listed["status"] == "ok"
    assert listed["count"] == 2
    assert listed["items"][0]["filename"] == "notes.txt"
    assert listed["items"][1]["filename"] == "archive.zip"
