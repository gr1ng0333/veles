import json
from unittest.mock import patch

from ouroboros.tools.registry import ToolContext
from ouroboros.tools.short_video_pack import _short_video_pack_download


def _ctx(tmp_path):
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path, current_chat_id=12345)


def _fake_download(item, workspace, downloader_cmd):
    path = workspace / f'{item.index:02d}.mp4'
    path.write_bytes(f'video-{item.index}'.encode('utf-8'))
    return type('Downloaded', (), {
        'normalized': item,
        'status': 'ok',
        'filename': f'{item.index:02d}-clip.mp4',
        'file_path': str(path),
        'artifact_path': '',
        'error': '',
    })()


def test_short_video_pack_contract_requires_exactly_one_manifest_source(tmp_path):
    ctx = _ctx(tmp_path)
    result = json.loads(_short_video_pack_download(ctx, items=[], manifest_path='manifest.json'))
    assert result['status'] == 'failed'
    assert result['error_kind'] == 'contract'


def test_short_video_pack_normalizes_dedupes_and_archives_manifest(tmp_path):
    ctx = _ctx(tmp_path)
    items = [
        {'url': ' https://www.tiktok.com/@moto/video/1?lang=en ', 'title': 'Night Ride'},
        {'url': 'https://www.tiktok.com/@moto/video/1?lang=en', 'title': 'Duplicate'},
        {'url': ''},
        {'url': 'https://www.tiktok.com/@moto/video/2/', 'notes': 'city lights'},
    ]

    def fake_save_artifact(_ctx, **kwargs):
        filename = kwargs['filename']
        rel = f"artifacts/outbox/2026/03/23/direct-chat/{filename}"
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        data = kwargs.get('data')
        if data is None:
            data = kwargs.get('content', '').encode('utf-8')
        target.write_bytes(data)
        return {'relative_path': rel, 'bytes': len(data)}

    with patch('ouroboros.tools.short_video_pack._resolve_yt_dlp', return_value=['python', '-m', 'yt_dlp']), \
         patch('ouroboros.tools.short_video_pack._download_one', side_effect=_fake_download), \
         patch('ouroboros.tools.short_video_pack.save_artifact', side_effect=fake_save_artifact), \
         patch('ouroboros.tools.short_video_pack._send_documents', return_value='queued documents') as send_documents:
        result = json.loads(_short_video_pack_download(ctx, items=items, max_items=10, dedupe=True, archive_manifest=True))

    assert result['status'] == 'ok'
    assert result['requested'] == 4
    assert result['normalized'] == 2
    assert result['dropped'] == 2
    assert result['downloaded'] == 2
    assert result['delivery']['mode'] == 'documents'
    assert result['delivery']['sent_files'] == 2
    assert result['delivery']['tool_result'] == 'queued documents'
    assert result['manifest_archive_path'].endswith('short-video-manifest.json')
    assert send_documents.called
    assert result['items'][0]['url'] == 'https://www.tiktok.com/@moto/video/1?lang=en'
    assert result['items'][1]['url'] == 'https://www.tiktok.com/@moto/video/2'
    assert all(not item['file_path'] for item in result['items'])
    assert all(item['artifact_path'].endswith('.mp4') for item in result['items'])


def test_short_video_pack_supports_manifest_path_and_zip_delivery(tmp_path):
    ctx = _ctx(tmp_path)
    manifest_path = tmp_path / 'manifest.json'
    manifest_path.write_text(json.dumps([
        {'url': 'https://www.tiktok.com/@moto/video/11', 'title': 'night'},
        {'url': 'https://www.tiktok.com/@moto/video/12', 'notes': 'city'},
    ]), encoding='utf-8')

    def fake_save_artifact(_ctx, **kwargs):
        filename = kwargs['filename']
        rel = f"artifacts/outbox/2026/03/23/direct-chat/{filename}"
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        data = kwargs.get('data')
        if data is None:
            data = kwargs.get('content', '').encode('utf-8')
        target.write_bytes(data)
        return {'relative_path': rel, 'bytes': len(data)}

    with patch('ouroboros.tools.short_video_pack._resolve_yt_dlp', return_value=['python', '-m', 'yt_dlp']), \
         patch('ouroboros.tools.short_video_pack._download_one', side_effect=_fake_download), \
         patch('ouroboros.tools.short_video_pack.save_artifact', side_effect=fake_save_artifact), \
         patch('ouroboros.tools.short_video_pack._send_local_file', return_value='queued zip') as send_local_file:
        result = json.loads(_short_video_pack_download(ctx, manifest_path=str(manifest_path), delivery_mode='zip', archive_manifest=False))

    assert result['status'] == 'ok'
    assert result['requested'] == 2
    assert result['normalized'] == 2
    assert result['downloaded'] == 2
    assert result['manifest_archive_path'] == ''
    assert result['delivery']['mode'] == 'zip'
    assert result['delivery']['sent_files'] == 2
    assert result['delivery']['tool_result'] == 'queued zip'
    assert result['delivery']['zip_path'].endswith('short-video-pack.zip')
    sent_path = send_local_file.call_args.kwargs['path']
    assert sent_path.endswith('short-video-pack.zip')
    assert (tmp_path / result['delivery']['zip_path']).exists()


def test_short_video_pack_returns_backend_unavailable_when_yt_dlp_missing(tmp_path):
    ctx = _ctx(tmp_path)
    with patch('ouroboros.tools.short_video_pack._resolve_yt_dlp', side_effect=RuntimeError('yt-dlp is not installed in the runtime.')):
        result = json.loads(_short_video_pack_download(ctx, items=[{'url': 'https://www.tiktok.com/@moto/video/1'}]))
    assert result['status'] == 'failed'
    assert result['error_kind'] == 'backend_unavailable'
