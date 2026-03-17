import pathlib
import tempfile
from types import SimpleNamespace

import pytest

from ouroboros.tools.core import _send_documents, _send_local_file
from ouroboros.tools.registry import ToolContext
from supervisor.events import _handle_send_documents


@pytest.mark.parametrize("files, caption, expected", [([], "", "requires a non-empty files list")])
def test_send_documents_and_local_file_flow(tmp_path, files, caption, expected):
    tmp = pathlib.Path(tempfile.mkdtemp())
    ctx = ToolContext(repo_dir=tmp, drive_root=tmp, current_chat_id=12345)
    result = _send_documents(
        ctx,
        files=[
            {"filename": "a.txt", "content": "alpha", "mime_type": "text/plain"},
            {"filename": "b.py", "content": "print(1)", "mime_type": "text/x-python"},
        ],
        caption="shared",
    )

    assert "2 documents queued" in result
    assert len(ctx.pending_events) == 1
    event = ctx.pending_events[0]
    assert event["type"] == "send_documents"
    assert event["caption"] == "shared"
    assert len(event["files"]) == 2
    assert event["files"][0]["filename"] == "a.txt"
    assert event["files"][0]["caption"] == ""
    assert event["files"][1]["filename"] == "b.py"

    empty_ctx = ToolContext(repo_dir=tmp, drive_root=tmp, current_chat_id=12345)
    empty = _send_documents(empty_ctx, files=files, caption=caption)
    assert expected in empty

    calls = []

    class DummyTG:
        def send_document(self, chat_id, file_bytes, filename, caption="", mime_type="application/octet-stream"):
            calls.append({
                "chat_id": chat_id,
                "filename": filename,
                "caption": caption,
                "mime_type": mime_type,
                "payload": file_bytes.decode("utf-8"),
            })
            return True, "ok"

    log_rows = []
    supervisor_ctx = SimpleNamespace(
        TG=DummyTG(),
        DRIVE_ROOT=pathlib.Path(tempfile.mkdtemp()),
        append_jsonl=lambda path, row: log_rows.append((path, row)),
    )

    evt = {
        "type": "send_documents",
        "chat_id": 12345,
        "caption": "shared",
        "files": [
            {
                "file_base64": "YWxwaGE=",
                "filename": "a.txt",
                "mime_type": "text/plain",
            },
            {
                "file_base64": "YmV0YQ==",
                "filename": "b.txt",
                "caption": "own",
                "mime_type": "text/plain",
            },
        ],
    }

    _handle_send_documents(evt, supervisor_ctx)

    assert len(calls) == 2
    assert calls[0]["filename"] == "a.txt"
    assert calls[0]["caption"] == "shared"
    assert calls[0]["payload"] == "alpha"
    assert calls[1]["filename"] == "b.txt"
    assert calls[1]["caption"] == "own"
    assert calls[1]["payload"] == "beta"
    assert log_rows == []

    from ouroboros.tools.core import _send_document

    doc_ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, current_chat_id=42, task_id="task-doc")
    single = _send_document(doc_ctx, content="print(\'ok\')\n", filename="solution.py", mime_type="text/x-python")
    assert "archived at artifacts/outbox" in single
    single_evt = doc_ctx.pending_events[-1]
    archived = tmp_path / single_evt["artifact_archive_path"]
    assert archived.exists()
    assert archived.read_text(encoding="utf-8") == "print('ok')\n"
    assert archived.with_suffix(archived.suffix + ".meta.json").exists()

    batch_ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, current_chat_id=42, task_id="task-batch")
    batch = _send_documents(batch_ctx, files=[
        {"content": "# plan\n- a\n", "filename": "plan.md", "mime_type": "text/markdown"},
        {"content": "hello", "filename": "notes.txt", "mime_type": "text/plain"},
    ])
    assert "archived under artifacts/outbox" in batch
    batch_evt = batch_ctx.pending_events[-1]
    assert batch_evt["type"] == "send_documents"
    for item in batch_evt["files"]:
        persisted = tmp_path / item["artifact_archive_path"]
        assert persisted.exists()
        assert persisted.with_suffix(persisted.suffix + ".meta.json").exists()

    local = tmp_path / "report.txt"
    local.write_text("ready\n", encoding="utf-8")
    local_ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, current_chat_id=12345, task_id="task-local")

    local_result = _send_local_file(local_ctx, path="report.txt", caption="done")
    assert "Local file queued for delivery" in local_result
    assert len(local_ctx.pending_events) == 1
    local_event = local_ctx.pending_events[0]
    assert local_event["type"] == "send_document"
    assert local_event["filename"] == "report.txt"
    assert local_event["caption"] == "done"
    local_archived = tmp_path / local_event["artifact_archive_path"]
    assert local_archived.exists()
    assert local_archived.read_text(encoding="utf-8") == "ready\n"
    meta = local_archived.with_suffix(local_archived.suffix + ".meta.json").read_text(encoding="utf-8")
    assert "send_local_file_tool" in meta

    missing = _send_local_file(local_ctx, path="missing.txt")
    assert "local file not found" in missing

    outside = _send_local_file(local_ctx, path="/etc/hosts")
    assert "outside allowed roots" in outside
