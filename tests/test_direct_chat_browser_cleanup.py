import pathlib
from unittest.mock import patch

from ouroboros.agent import OuroborosAgent, Env
from ouroboros.tools.registry import BrowserState, ToolContext


def test_direct_chat_handle_task_touches_persistent_browser_session_in_finally(tmp_path):
    env = Env(repo_dir=pathlib.Path('/opt/veles'), drive_root=tmp_path, branch_dev='veles')
    agent = OuroborosAgent(env, event_queue=None)

    preserved_ctx = ToolContext(
        repo_dir=env.repo_dir,
        drive_root=env.drive_root,
        current_chat_id=123,
        current_task_type='task',
        is_direct_chat=True,
    )
    preserved_ctx.browser_state = BrowserState(page=object(), browser=object(), context=object())

    def fake_prepare(task):
        agent.tools.set_context(preserved_ctx)
        return preserved_ctx, [{"role": "user", "content": "hi"}], {"budget_remaining": None}

    with patch.object(agent, '_prepare_task_context', side_effect=fake_prepare), \
         patch('ouroboros.agent.run_llm_loop', return_value=('ok', {}, {'assistant_notes': [], 'tool_calls': []})), \
         patch('ouroboros.tools.browser_runtime.BrowserSessionManager.touch') as touch_mock, \
         patch('ouroboros.tools.browser_runtime.cleanup_browser') as cleanup_mock:
        agent.handle_task({'id': 't1', 'type': 'task', 'chat_id': 123, 'text': 'hello', '_is_direct_chat': True})

    touch_mock.assert_called_once_with(123)
    cleanup_mock.assert_not_called()
