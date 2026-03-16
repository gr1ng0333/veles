import json

from ouroboros.artifacts import save_artifact
from ouroboros.tools.registry import ToolContext


def test_save_artifact_persists_text_and_meta(tmp_path):
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, current_chat_id=99, task_id='plan-42')
    meta = save_artifact(
        ctx,
        content='# approved plan\n- keep files\n',
        filename='approved_plan.md',
        content_kind='plan',
        mime_type='text/markdown',
        source='unit-test',
        related_message='agreed with owner',
        metadata={'kind': 'plan'},
    )
    path = tmp_path / meta['relative_path']
    assert path.exists()
    assert path.read_text(encoding='utf-8') == '# approved plan\n- keep files\n'
    meta_json = json.loads(path.with_suffix(path.suffix + '.meta.json').read_text(encoding='utf-8'))
    assert meta_json['task_id'] == 'plan-42'
    assert meta_json['chat_id'] == 99
    assert meta_json['content_kind'] == 'plan'
    assert meta_json['metadata']['kind'] == 'plan'
