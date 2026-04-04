import json, pathlib, unittest
from unittest.mock import patch
from ouroboros.tools.tg_channel_read import get_tools, _fetch_channel_posts, _parse_posts, _strip_tags
from ouroboros.tools.registry import ToolContext


def make_ctx():
    return ToolContext(repo_dir=pathlib.Path('/tmp'), drive_root=pathlib.Path('/tmp'))


class TestTgTools(unittest.TestCase):
    def test_get_tools_count(self):
        tools = get_tools()
        self.assertEqual(len(tools), 2)
        names = {t.name for t in tools}
        self.assertIn('tg_channel_read', names)
        self.assertIn('tg_digest', names)

    def test_schema_structure(self):
        for t in get_tools():
            schema = t.schema
            self.assertIn('name', schema)
            self.assertIn('parameters', schema)
            self.assertIn('properties', schema['parameters'])

    def test_strip_tags_basic(self):
        html = '<b>Hello</b> &amp; <i>World</i>'
        result = _strip_tags(html)
        self.assertEqual(result, 'Hello & World')

    def test_parse_posts_empty(self):
        posts = _parse_posts('')
        self.assertEqual(posts, [])

    def test_fetch_channel_posts_network_error(self):
        with patch('ouroboros.tools.tg_channel_read._fetch_page', side_effect=Exception('timeout')):
            result = _fetch_channel_posts('nonexistent_channel_xyz', limit=5)
        self.assertIn('error', result)
        self.assertEqual(result.get('posts', []), [])

    def test_tg_channel_read_empty_channel(self):
        ctx = make_ctx()
        tools = {t.name: t for t in get_tools()}
        result = json.loads(tools['tg_channel_read'].handler(ctx, channel=''))
        self.assertIn('error', result)

    def test_tg_digest_empty_channels(self):
        ctx = make_ctx()
        tools = {t.name: t for t in get_tools()}
        result = json.loads(tools['tg_digest'].handler(ctx, channels=[]))
        self.assertIn('error', result)

    def test_tg_digest_merges_channels(self):
        fake_posts = {
            'ch1': [{'id': 1, 'date': '2026-04-03T10:00:00+00:00', 'text': 'A', 'views': 0, 'links': []}],
            'ch2': [{'id': 2, 'date': '2026-04-03T09:00:00+00:00', 'text': 'B', 'views': 0, 'links': []}],
        }
        def mock_fetch(channel, **kw):
            posts = fake_posts.get(channel, [])
            return {'channel': channel, 'posts': posts, 'posts_count': len(posts)}
        with patch('ouroboros.tools.tg_channel_read._fetch_channel_posts', side_effect=mock_fetch):
            ctx = make_ctx()
            tools = {t.name: t for t in get_tools()}
            result = json.loads(tools['tg_digest'].handler(ctx, channels=['ch1', 'ch2']))
        self.assertEqual(result['total_posts'], 2)
        self.assertEqual(result['channels_queried'], 2)
        # ch2 is 09:00, ch1 is 10:00 — so ch2 comes first
        self.assertEqual(result['posts'][0]['channel'], 'ch2')
        self.assertEqual(result['posts'][1]['channel'], 'ch1')

    def test_tg_digest_channel_tagged(self):
        fake_posts = [
            {'id': 5, 'date': '2026-04-01T12:00:00+00:00', 'text': 'X', 'views': 100, 'links': []}
        ]
        def mock_fetch(channel, **kw):
            return {'channel': channel, 'posts': fake_posts, 'posts_count': len(fake_posts)}
        with patch('ouroboros.tools.tg_channel_read._fetch_channel_posts', side_effect=mock_fetch):
            ctx = make_ctx()
            tools = {t.name: t for t in get_tools()}
            result = json.loads(tools['tg_digest'].handler(ctx, channels=['testchan']))
        self.assertTrue(all('channel' in p for p in result['posts']))
        self.assertEqual(result['posts'][0]['channel'], 'testchan')

    def test_tg_digest_since_hours(self):
        """since_hours=0 means no cutoff; should not filter anything."""
        fake_posts = [
            {'id': 10, 'date': '2020-01-01T00:00:00+00:00', 'text': 'old', 'views': 0, 'links': []}
        ]
        def mock_fetch(channel, **kw):
            return {'channel': channel, 'posts': fake_posts, 'posts_count': len(fake_posts)}
        with patch('ouroboros.tools.tg_channel_read._fetch_channel_posts', side_effect=mock_fetch):
            ctx = make_ctx()
            tools = {t.name: t for t in get_tools()}
            # since_hours=0 = no filter
            result = json.loads(tools['tg_digest'].handler(ctx, channels=['ch'], since_hours=0))
        self.assertEqual(result['total_posts'], 1)

if __name__ == '__main__':
    unittest.main()
