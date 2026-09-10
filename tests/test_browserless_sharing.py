"""HTTP-only fallback and saved original-work links; no external services."""
import unittest
from pathlib import Path
from unittest import mock

from tests.test_security_reliability import server, make_request


class BrowserlessSharingTests(unittest.TestCase):
    item = '7677684373646753142'

    def test_http_fallback_and_failure_never_start_subprocess(self):
        with mock.patch.dict(server._douyin_result_cache, {}, clear=True), \
                mock.patch.object(server.subprocess, 'Popen', side_effect=AssertionError('browser started')), \
                mock.patch.object(server, '_douyin_fetch_html', return_value=('', [{'desc': 'saved'}])) as fetch, \
                mock.patch.object(server, '_douyin_native_result', return_value={
                    'kind': 'video', 'title': 'saved', 'video': {'media_available': False}}):
            result = server._parse_douyin_item_direct('video', self.item, allow_metadata=True)
            self.assertEqual(result['title'], 'saved')
            self.assertTrue(result['metadata_only'])
            fetch.return_value = ('', [])
            with self.assertRaises(server.ApiError):
                server._parse_douyin_item_direct('video', self.item, refresh=True)
        source = Path('server.py').read_text()
        self.assertNotIn('--headless', source)
        self.assertNotIn('_douyin_browser_extract', source)
        self.assertNotIn('DOUYIN_BROWSER_', source)

    def test_original_work_comes_from_saved_source_without_network(self):
        row = {'item_id': 'item_old_hash', 'kind': 'note', 'source_url':
               f'https://www.douyin.com/note/{self.item}?tracking=secret'}
        with mock.patch.object(server, '_saved_parse_source', return_value=''), \
                mock.patch.object(server, 'open_url', side_effect=AssertionError('network requested')):
            self.assertEqual(server._share_original_url(row, {'platform': 'douyin'}),
                             f'https://www.douyin.com/note/{self.item}/')
            row['source_url'] = ''
            row['item_id'] = self.item
            self.assertEqual(server._share_original_url(row, {'platform': 'douyin'}),
                             f'https://www.douyin.com/note/{self.item}/')

    def test_legacy_douyin_share_without_platform_has_original_button(self):
        with mock.patch.object(server, '_saved_parse_source', return_value=''):
            self.assertEqual(server._share_original_url(
                {'item_id': self.item, 'kind': 'video'}, {'source': 'douyin_direct'}),
                f'https://www.douyin.com/video/{self.item}/')

    def test_short_link_and_tiktok_sources_are_sanitized(self):
        row = {'item_id': 'item_hash', 'kind': 'video'}
        with mock.patch.object(server, '_saved_parse_source', return_value='https://v.douyin.com/demo/?track=1'):
            self.assertEqual(server._share_original_url(row, {'platform': 'douyin'}),
                             'https://v.douyin.com/demo/')
        with mock.patch.object(server, '_saved_parse_source', return_value=''):
            for url in ('javascript:alert(1)', 'https://www.douyin.com.evil.test/video/12345678',
                        'https://v3.douyinvod.com/media.mp4?secret=1', 'https://127.0.0.1/',
                        'https://user:password@www.douyin.com/video/12345678'):
                with self.subTest(url=url):
                    self.assertEqual(server._share_original_url(dict(row, source_url=url), {}), '')
            url = 'https://www.tiktok.com/@creator/video/6718335390845095173'
            self.assertEqual(server._share_original_url(dict(row, source_url=url + '?tracking=1'),
                             {'platform': 'tiktok'}), url)

    def test_share_view_exposes_original_only_for_accessible_share(self):
        data = {'item_id': self.item, 'kind': 'note', 'platform': 'douyin',
                'source': 'parser', 'title': 'Saved title', 'author': 'Saved author',
                'images': [], '_link': f'https://www.douyin.com/note/{self.item}/'}
        with mock.patch.object(server, 'current_user', return_value=None):
            created = server._share_create(make_request('/api/share'), data)
        sid = created['sid']
        self.addCleanup(server.db_exec, 'DELETE FROM shares WHERE id=?', (sid,))
        row = dict(server.db_exec('SELECT * FROM shares WHERE id=?', (sid,), 'one'))
        with mock.patch.object(server, 'open_url', side_effect=AssertionError('network requested')):
            view = server._share_view(row)
            self.assertEqual(view['data']['original_url'], data['_link'])
            self.assertNotIn('_link', view['data'])
            self.assertNotIn('source_url', view['data'])
            # Expired or blocked shares may not disclose even the public original URL.
            with mock.patch.object(server, '_share_state', return_value='expired'), \
                    mock.patch.object(server, '_saved_parse_source', side_effect=AssertionError('read inaccessible source')):
                expired = server._share_view(row)
                self.assertNotIn('original_url', expired['data'])
