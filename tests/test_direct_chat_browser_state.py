import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def test_direct_chat_browser_state_persists_between_messages():
    from ouroboros.agent import make_agent
    from ouroboros.tools.registry import BrowserState, ToolContext

    with tempfile.TemporaryDirectory() as tmp:
        drive_root = Path(tmp)
        (drive_root / 'logs').mkdir(parents=True, exist_ok=True)
        (drive_root / 'memory').mkdir(parents=True, exist_ok=True)
        (drive_root / 'state').mkdir(parents=True, exist_ok=True)
        (drive_root / 'logs' / 'chat.jsonl').write_text('', encoding='utf-8')
        (drive_root / 'memory' / 'scratchpad.md').write_text('# Scratchpad\n', encoding='utf-8')
        (drive_root / 'memory' / 'identity.md').write_text('# Identity\n', encoding='utf-8')
        (drive_root / 'state' / 'state.json').write_text('{}', encoding='utf-8')

        agent = make_agent(repo_dir='/opt/veles', drive_root=str(drive_root), event_queue=None)

        seen = []

        def fake_prepare(task):
            ctx = ToolContext(repo_dir=Path('/opt/veles'), drive_root=drive_root)
            ctx.current_chat_id = int(task.get('chat_id') or 0) or None
            ctx.current_task_type = str(task.get('type') or '')
            ctx.task_id = str(task.get('id') or '')
            ctx.is_direct_chat = bool(task.get('_is_direct_chat'))
            agent.tools.set_context(ctx)
            seen.append(ctx)
            return ctx, [], {'budget_remaining': None}

        def fake_run_llm_loop(**kwargs):
            return 'ok', {}, {'tool_calls': []}

        with patch.object(agent, '_prepare_task_context', side_effect=fake_prepare), \
             patch('ouroboros.agent.run_llm_loop', side_effect=fake_run_llm_loop):
            first_task = {'id': 't1', 'type': 'task', 'chat_id': 1, 'text': 'first', '_is_direct_chat': True}
            agent.handle_task(first_task)
            first_ctx = seen[-1]
            first_ctx.browser_state.last_screenshot_b64 = 'abc123'
            first_ctx.browser_state.active_session_name = 'acmp'

            second_task = {'id': 't2', 'type': 'task', 'chat_id': 1, 'text': 'second', '_is_direct_chat': True}
            agent.handle_task(second_task)
            second_ctx = seen[-1]

        assert second_ctx.browser_state is first_ctx.browser_state
        assert second_ctx.browser_state.last_screenshot_b64 == 'abc123'
        assert second_ctx.browser_state.active_session_name == 'acmp'


def test_worker_task_still_cleans_up_browser_state():
    from ouroboros.agent import make_agent
    from ouroboros.tools.registry import ToolContext

    with tempfile.TemporaryDirectory() as tmp:
        drive_root = Path(tmp)
        (drive_root / 'logs').mkdir(parents=True, exist_ok=True)
        (drive_root / 'memory').mkdir(parents=True, exist_ok=True)
        (drive_root / 'state').mkdir(parents=True, exist_ok=True)
        (drive_root / 'logs' / 'chat.jsonl').write_text('', encoding='utf-8')
        (drive_root / 'memory' / 'scratchpad.md').write_text('# Scratchpad\n', encoding='utf-8')
        (drive_root / 'memory' / 'identity.md').write_text('# Identity\n', encoding='utf-8')
        (drive_root / 'state' / 'state.json').write_text('{}', encoding='utf-8')

        agent = make_agent(repo_dir='/opt/veles', drive_root=str(drive_root), event_queue=None)

        def fake_prepare(task):
            ctx = ToolContext(repo_dir=Path('/opt/veles'), drive_root=drive_root)
            ctx.current_chat_id = int(task.get('chat_id') or 0) or None
            ctx.current_task_type = str(task.get('type') or '')
            ctx.task_id = str(task.get('id') or '')
            ctx.browser_state.last_screenshot_b64 = 'abc123'
            agent.tools.set_context(ctx)
            return ctx, [], {'budget_remaining': None}

        def fake_run_llm_loop(**kwargs):
            return 'ok', {}, {'tool_calls': []}

        with patch.object(agent, '_prepare_task_context', side_effect=fake_prepare), \
             patch('ouroboros.agent.run_llm_loop', side_effect=fake_run_llm_loop):
            task = {'id': 'w1', 'type': 'task', 'chat_id': 1, 'text': 'worker'}
            agent.handle_task(task)

        assert agent._direct_chat_browser_state is None
        assert agent.tools._ctx.browser_state.last_screenshot_b64 is None
