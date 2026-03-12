import pathlib
from concurrent.futures import TimeoutError as FuturesTimeoutError

from ouroboros.loop import _StatefulToolExecutor, _execute_with_timeout, _handle_tool_calls


class DummyTools:
    CODE_TOOLS = set()

    def execute(self, fn_name, args):
        return "ok"

    def get_timeout(self, fn_name):
        return 1


def _tool_call(name="repo_read", tool_call_id="call-1"):
    return {
        "id": tool_call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": "{}",
        },
    }


def test_execute_with_timeout_returns_structured_error_when_future_result_crashes(monkeypatch, tmp_path):
    class DummyFuture:
        def result(self, timeout=None):
            raise RuntimeError("executor blew up")

    class DummyExecutor:
        def submit(self, fn, *args, **kwargs):
            return DummyFuture()

        def shutdown(self, wait=False, cancel_futures=True):
            return None

    monkeypatch.setattr("ouroboros.loop.ThreadPoolExecutor", lambda max_workers=1: DummyExecutor())

    result = _execute_with_timeout(
        DummyTools(),
        _tool_call(),
        pathlib.Path(tmp_path),
        timeout_sec=3,
        task_id="task-x",
        stateful_executor=None,
    )

    assert result["is_error"] is True
    assert result["fn_name"] == "repo_read"
    assert "TOOL_EXECUTION_ERROR (repo_read)" in result["result"]
    assert "during result" in result["result"]


def test_execute_with_timeout_resets_stateful_executor_on_unexpected_result_error(tmp_path):
    class DummyFuture:
        def result(self, timeout=None):
            raise RuntimeError("browser future crashed")

    class DummyStatefulExecutor:
        def __init__(self):
            self.reset_called = False

        def submit(self, fn, *args, **kwargs):
            return DummyFuture()

        def reset(self):
            self.reset_called = True

    stateful = DummyStatefulExecutor()
    result = _execute_with_timeout(
        DummyTools(),
        _tool_call(name="browse_page"),
        pathlib.Path(tmp_path),
        timeout_sec=3,
        task_id="task-browser",
        stateful_executor=stateful,
    )

    assert stateful.reset_called is True
    assert result["is_error"] is True
    assert "TOOL_EXECUTION_ERROR (browse_page)" in result["result"]
    assert "Browser state has been reset" in result["result"]


def test_handle_tool_calls_parallel_converts_future_failure_into_tool_error(monkeypatch, tmp_path):
    class ExplodingFuture:
        def result(self):
            return None


    class DummyExecutor:
        def __init__(self, *args, **kwargs):
            self.futures = []

        def submit(self, fn, *args, **kwargs):
            future = ExplodingFuture()
            self.futures.append(future)
            return future

        def shutdown(self, wait=False, cancel_futures=True):
            return None

    def fake_as_completed(futures):
        for future in list(futures.keys()):
            yield future

    def fake_future_result(self):
        raise FuturesTimeoutError("parallel future timeout")

    monkeypatch.setattr("ouroboros.loop.ThreadPoolExecutor", DummyExecutor)
    monkeypatch.setattr("ouroboros.loop.as_completed", fake_as_completed)
    monkeypatch.setattr(ExplodingFuture, "result", fake_future_result, raising=True)

    messages = []
    llm_trace = {"assistant_notes": []}
    tool_calls = [_tool_call(name="repo_read", tool_call_id="call-a"), _tool_call(name="drive_read", tool_call_id="call-b")]

    error_count, made_progress = _handle_tool_calls(
        tool_calls,
        DummyTools(),
        pathlib.Path(tmp_path),
        "task-parallel",
        _StatefulToolExecutor(),
        messages,
        llm_trace,
        lambda _text: None,
    )

    assert error_count == 2
    assert made_progress is False
    assert len(messages) == 2
    assert all(msg["role"] == "tool" for msg in messages)
    assert all("TOOL_EXECUTION_ERROR" in msg["content"] for msg in messages)
