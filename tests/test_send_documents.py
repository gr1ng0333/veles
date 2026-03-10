import pathlib
import tempfile
from types import SimpleNamespace

from ouroboros.tools.core import _send_documents
from ouroboros.tools.registry import ToolContext
from supervisor.events import _handle_send_documents


def make_ctx():
    tmp = pathlib.Path(tempfile.mkdtemp())
    return ToolContext(repo_dir=tmp, drive_root=tmp, current_chat_id=12345)


def test_send_documents_queues_single_bulk_event_with_default_caption():
    ctx = make_ctx()
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


def test_send_documents_rejects_empty_files_list():
    ctx = make_ctx()
    result = _send_documents(ctx, files=[])
    assert "requires a non-empty files list" in result


def test_handle_send_documents_sends_each_file_with_caption_fallback():
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
