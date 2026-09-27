"""纯字段边界与晚到元数据集成回归，不发网络请求。"""
import copy
import json
import time
import unittest
from unittest import mock

from metadata_fields import extract_metadata, sanitize_metadata, is_truncated_title, title_prefix
from tests.test_security_reliability import server, TestClient


class MetadataFieldsTests(unittest.TestCase):
    def test_upgrade_notice_is_partial_and_prefix_can_match_full_text(self):
        title = '更多完整剧情细节，很多故事发展其实都……版本过低，升级后可展示全部信息'
        self.assertTrue(is_truncated_title(title))
        self.assertEqual(title_prefix(title), '更多完整剧情细节，很多故事发展其实都')
        self.assertFalse(is_truncated_title('故事里说版本过低也是一种遗憾'))

    def test_only_display_fields_and_valid_zero_survive(self):
        result = sanitize_metadata({
            'author': '作者', 'stats': {'digg': 0, 'comment': False, 'share': -1, 'collect': '12', 'play': 0},
            'video': {'width': 1920, 'height': 824, 'url': 'https://bad.test/secret.mp4'},
            'avatar': 'https://p3.douyinpic.com/avatar.jpg', 'cover': 'https://127.0.0.1/private',
            'author_url': 'javascript:alert(1)', 'cookie': 'secret', '_link': 'secret',
            'duration_ms': float('inf'), 'create_time': True,
        })
        self.assertEqual(result['stats'], {'digg': 0, 'collect': 12})
        self.assertEqual(result['video'], {'width': 1920, 'height': 824})
        for key in ('cookie', '_link', 'cover', 'author_url', 'duration_ms', 'create_time'):
            self.assertNotIn(key, result)

    def test_raw_detail_units_and_whole_body(self):
        detail = {'desc': '完整作品正文', 'author': {'nickname': '作者', 'sec_uid': 'MS4wLjAB'},
                  'create_time': 1790404437, 'statistics': {'digg_count': 0, 'comment_count': 2},
                  'video': {'duration': 1704833, 'width': 1920, 'height': 824},
                  'text_extra': [{'hashtag_name': 'AI'}, {'hashtag_name': 'AI'}]}
        data = extract_metadata(detail)
        self.assertEqual(data['duration_ms'], 1704833)
        self.assertEqual(data['stats'], {'digg': 0, 'comment': 2})
        self.assertEqual(data['tags'], ['AI'])
        self.assertEqual(data['author_url'], 'https://www.douyin.com/user/MS4wLjAB')
        self.assertEqual(data['content_status'], 'available')

    def test_content_limit_is_explicit(self):
        data = sanitize_metadata({'content': '字' * 10001})
        self.assertEqual(len(data['content']), 10000)
        self.assertEqual(data['content_status'], 'partial')


class MetadataMergeTests(unittest.TestCase):
    item_id = '7689724147420630306'
    url = 'https://www.douyin.com/video/7689724147420630306/'

    def setUp(self):
        self.data = {'item_id': self.item_id, 'kind': 'video', 'platform': 'douyin', 'source': 'parser',
                     'title': '故事发展……版本过低，升级后可展示全部信息', 'title_source': 'provider',
                     'title_status': 'available', 'author': '未知作者', 'stats': {'digg': 0},
                     'video': {'url': 'https://v3.douyinvod.com/original.mp4', 'width': 1080}, '_link': self.url}
        self.fresh = {'item_id': self.item_id, 'kind': 'video', 'title': '故事发展其实还有更多细节',
                      'title_source': 'http_structured', 'title_status': 'available', '_link': self.url,
                      'content': '故事发展其实还有更多细节', 'author': '公开作者',
                      'stats': {'digg': 99, 'comment': 0, 'share': 8, 'collect': 3},
                      'create_time': 1790404437, 'duration_ms': 1704833,
                      'video': {'width': 1920, 'height': 824, 'url': 'https://bad.test/new.mp4'},
                      'cookie': 'must-not-persist'}
        for cache in (server._browser_title_jobs, server._browser_title_results, server._cache):
            patcher = mock.patch.dict(cache, {}, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_real_truncation_upgrades_and_fills_metadata_without_media_or_zero_changes(self):
        original = copy.deepcopy(self.data)
        result = server._browser_title_apply(self.data, self.fresh)
        self.assertEqual(result['title'], self.fresh['title'])
        self.assertEqual(result['title_status'], 'available')
        self.assertEqual(result['author'], '公开作者')
        self.assertEqual(result['stats'], {'digg': 0, 'comment': 0, 'share': 8, 'collect': 3})
        self.assertEqual(result['duration_ms'], 1704833)
        self.assertEqual(result['video']['url'], original['video']['url'])
        self.assertEqual(result['video']['width'], 1080)
        self.assertEqual(result['video']['height'], 824)
        self.assertNotIn('cookie', result)
        self.assertEqual(self.data, original)

    def test_custom_title_protects_only_title_and_can_receive_missing_metadata(self):
        data = dict(self.data, title='用户自定', title_source='custom', title_status='complete')
        result = server._browser_title_apply(data, self.fresh)
        self.assertEqual(result['title'], '用户自定')
        self.assertEqual(result['author'], '公开作者')

    def test_explicit_partial_body_and_empty_tags_can_be_completed(self):
        data = dict(self.data, content='故事发展', content_status='partial', tags=[])
        result = server._browser_title_apply(data, dict(self.fresh, tags=['短片', 'AI']))
        self.assertEqual(result['content'], self.fresh['content'])
        self.assertEqual(result['content_status'], 'available')
        self.assertEqual(result['tags'], ['短片', 'AI'])

    def test_wrong_identity_cannot_fill_any_field(self):
        for changes in ({'item_id': '7689724147420630307'}, {'kind': 'note'}):
            self.assertEqual(server._browser_title_apply(self.data, dict(self.fresh, **changes)), self.data)

    def test_long_full_body_is_preserved_but_short_title_is_partial(self):
        data = dict(self.data, title='', title_source='provider')
        fresh = dict(self.fresh, title='字' * 1500, content='字' * 1500, title_status='complete')
        result = server._browser_title_apply(data, fresh)
        self.assertEqual(len(result['title']), 1000)
        self.assertEqual(result['title_status'], 'partial')
        self.assertEqual(len(result['content']), 1500)
        self.assertEqual(result['content_status'], 'available')

    def test_callback_persists_only_metadata_and_signed_poll_is_read_only(self):
        server._save_parse_snapshot(copy.deepcopy(self.data), self.url)
        before = dict(server.db_exec('SELECT * FROM parse_snapshots WHERE item_id=?', (self.item_id,), 'one'))
        self.addCleanup(server.db_exec, 'DELETE FROM parse_snapshots WHERE item_id=?', (self.item_id,))
        with mock.patch.object(server, 'reserve_quota', side_effect=AssertionError('重复扣费')), \
                mock.patch.object(server, 'open_url', side_effect=AssertionError('回调发网络')):
            server._browser_title_complete(self.url, self.fresh)
        after = dict(server.db_exec('SELECT * FROM parse_snapshots WHERE item_id=?', (self.item_id,), 'one'))
        self.assertEqual(after['expires_at'], before['expires_at'])
        payload = json.loads(after['payload'])
        self.assertEqual(payload['stats']['comment'], 0)
        self.assertNotIn('cookie', payload)
        exp, sig = server._media_token('parse_metadata', 'video:' + self.item_id, ttl=600)
        with mock.patch.object(server, '_browser_title_start', side_effect=AssertionError('GET启动网络')):
            response = server.api_parse_metadata(self.item_id, 'video', exp, sig)
        public = json.loads(response.body)['data']
        self.assertEqual(public['author'], '公开作者')
        self.assertIn('stats.comment', public['metadata_fields'])
        self.assertNotIn('url', public['video'])
        self.assertNotIn('_link', public)
        self.assertEqual(public['metadata_status'], 'ready')

    def test_strict_proxy_accepts_only_an_explicit_sidecar_proxy(self):
        service = mock.Mock(proxy='http://127.0.0.1:7897')
        service.submit.return_value = True
        with mock.patch.object(server, '_browser_title_service', service), \
                mock.patch.dict(server.proxy_mgr.settings, {'force_proxy': True}):
            self.assertTrue(server._browser_title_start(self.url))
        service.submit.assert_called_once()


if __name__ == '__main__':
    unittest.main()
