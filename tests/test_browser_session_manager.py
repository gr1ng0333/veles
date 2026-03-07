"""Tests for BrowserSessionManager — persistent browser sessions across direct chat messages."""

import time
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.tools.registry import BrowserState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_manager():
    """Import and return a *clean* BrowserSessionManager (clear sessions)."""
    from ouroboros.tools.browser_runtime import BrowserSessionManager
    BrowserSessionManager._sessions.clear()
    return BrowserSessionManager


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBrowserSessionManager:

    def test_get_or_create_returns_same_state_for_same_chat(self):
        mgr = _get_manager()
        bs1, ex1 = mgr.get_or_create(123)
        bs2, ex2 = mgr.get_or_create(123)
        assert bs1 is bs2, "BrowserState must be the same object for the same chat_id"
        assert ex1 is ex2, "Executor must be the same object for the same chat_id"

    def test_get_or_create_returns_different_state_for_different_chats(self):
        mgr = _get_manager()
        bs1, _ = mgr.get_or_create(100)
        bs2, _ = mgr.get_or_create(200)
        assert bs1 is not bs2

    def test_touch_updates_last_used(self):
        mgr = _get_manager()
        mgr.get_or_create(42)
        t_before = mgr._sessions[42]["last_used"]
        time.sleep(0.01)
        mgr.touch(42)
        t_after = mgr._sessions[42]["last_used"]
        assert t_after > t_before

    def test_cleanup_removes_session(self):
        mgr = _get_manager()
        mgr.get_or_create(55)
        assert mgr.has_session(55)
        mgr.cleanup(55)
        assert not mgr.has_session(55)

    def test_cleanup_nonexistent_is_noop(self):
        mgr = _get_manager()
        mgr.cleanup(999)  # should not raise

    def test_cleanup_stale_removes_old_sessions(self):
        mgr = _get_manager()
        mgr.get_or_create(1)
        mgr.get_or_create(2)
        # Make session 1 stale by backdating its last_used
        mgr._sessions[1]["last_used"] = time.monotonic() - 600
        closed = mgr.cleanup_stale(max_idle_seconds=300)
        assert closed == 1
        assert not mgr.has_session(1)
        assert mgr.has_session(2)

    def test_cleanup_stale_keeps_fresh_sessions(self):
        mgr = _get_manager()
        mgr.get_or_create(10)
        closed = mgr.cleanup_stale(max_idle_seconds=300)
        assert closed == 0
        assert mgr.has_session(10)

    def test_validate_browser_dead_when_none(self):
        mgr = _get_manager()
        bs = BrowserState()
        assert mgr.validate(bs) == "browser_dead"

    def test_validate_browser_dead_when_disconnected(self):
        mgr = _get_manager()
        bs = BrowserState()
        mock_browser = MagicMock()
        mock_browser.is_connected.return_value = False
        bs.browser = mock_browser
        assert mgr.validate(bs) == "browser_dead"

    def test_validate_context_dead(self):
        mgr = _get_manager()
        bs = BrowserState()
        mock_browser = MagicMock()
        mock_browser.is_connected.return_value = True
        bs.browser = mock_browser
        bs.context = None
        assert mgr.validate(bs) == "context_dead"

    def test_validate_page_dead_when_none(self):
        mgr = _get_manager()
        bs = BrowserState()
        mock_browser = MagicMock()
        mock_browser.is_connected.return_value = True
        bs.browser = mock_browser
        bs.context = MagicMock()
        bs.page = None
        assert mgr.validate(bs) == "page_dead"

    def test_validate_page_dead_when_exception(self):
        mgr = _get_manager()
        bs = BrowserState()
        mock_browser = MagicMock()
        mock_browser.is_connected.return_value = True
        bs.browser = mock_browser
        bs.context = MagicMock()
        mock_page = MagicMock()
        mock_page.url = property(lambda self: (_ for _ in ()).throw(Exception("dead")))
        # Use a property that raises
        type(mock_page).url = property(lambda self: (_ for _ in ()).throw(Exception("dead")))
        bs.page = mock_page
        assert mgr.validate(bs) == "page_dead"

    def test_validate_ok(self):
        mgr = _get_manager()
        bs = BrowserState()
        mock_browser = MagicMock()
        mock_browser.is_connected.return_value = True
        bs.browser = mock_browser
        bs.context = MagicMock()
        mock_page = MagicMock()
        type(mock_page).url = property(lambda self: "https://example.com")
        bs.page = mock_page
        assert mgr.validate(bs) == "ok"


class TestBrowserStatePersistenceAcrossTasks:
    """Simulate two sequential handle_task calls with is_direct_chat=True
    and verify the BrowserState is shared (the same object) across both."""

    def test_shared_browser_state_across_direct_chat_tasks(self):
        """The same BrowserState object should be attached to both ToolContexts."""
        mgr = _get_manager()
        chat_id = 777

        # Simulate first message: _prepare_task_context gets BrowserState from manager
        bs1, ex1 = mgr.get_or_create(chat_id)

        # Simulate second message: same chat_id → same object
        bs2, ex2 = mgr.get_or_create(chat_id)

        assert bs1 is bs2
        assert ex1 is ex2

    def test_non_direct_chat_does_not_use_manager(self):
        """Non-direct-chat tasks should NOT store or retrieve from manager."""
        mgr = _get_manager()
        # No sessions should exist since we cleared
        assert len(mgr._sessions) == 0
        # A non-direct task wouldn't call get_or_create → manager stays empty
        bs = BrowserState()  # fresh, standalone
        assert len(mgr._sessions) == 0

    def test_page_url_preserved_in_persistent_state(self):
        """When BrowserState is persisted, the page URL should not be about:blank
        on the second access (assuming page is alive)."""
        mgr = _get_manager()
        bs, _ = mgr.get_or_create(42)

        # Simulate: first task navigated to a URL
        mock_browser = MagicMock()
        mock_browser.is_connected.return_value = True
        mock_page = MagicMock()
        type(mock_page).url = property(lambda self: "https://logged-in.example.com/dashboard")
        bs.browser = mock_browser
        bs.context = MagicMock()
        bs.page = mock_page

        # Second task retrieves the same state
        bs2, _ = mgr.get_or_create(42)
        assert bs2 is bs
        assert mgr.validate(bs2) == "ok"
        assert bs2.page.url != "about:blank"
