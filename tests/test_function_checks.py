"""部署功能检查、缺失元数据缓存重试与旧分享快照修复。"""
import copy
import io
import json
import threading
import time
import unittest
from unittest import mock

from tests.test_security_reliability import server, TestClient, make_request


class FunctionChecksTests(unittest.TestCase):
    url = 'https://v.douyin.com/qFnZK0HPwBo/'

    def setUp(self):
        for cache in (server._cache, server._author_cache, server._metadata_retries,
                      server._metadata_last_failure, server._function_check_job):
            patch = mock.patch.dict(cache, {}, clear=True)
            patch.start()
            self.addCleanup(patch.stop)
        for table in ('shares', 'parse_snapshots', 'blocked_share_items'):
            server.db_exec('DELETE FROM ' + table)
        self.item = server._atc_item_id(self.url, {})
        self.old = {'item_id': self.item, 'kind': 'video', 'platform': 'douyin',
                    'source': 'parser', 'title': '暂无标题', 'author': '',
                    'stats': dict.fromkeys(('digg', 'comment', 'collect', 'share')),
                    'video': {'url': 'https://v3.douyinvod.com/primary.mp4',
                              'source': 'parser', 'filename': '暂无标题.mp4',
                              'width': 1080, 'height': 1920}, '_link': self.url}
        self.full = copy.deepcopy(self.old)
        self.full.update(title='真实标题', author='真实作者', snapshot_at=int(time.time()),
                         stats={'digg': 0, 'comment': 12, 'collect': 5, 'share': 8})

    def share(self, data=None, custom='自定义标题'):
        return server._share_create(make_request('/api/share'), data or self.old, custom)

    def test_admin_routes_require_admin(self):
        with TestClient(server.app, raise_server_exceptions=False) as client:
            for method, path, body in [
                ('GET', '/api/admin/checks', None),
                ('POST', '/api/admin/checks/run', {'text': self.url}),
                ('POST', '/api/admin/checks/shares/test/repair', {})]:
                r = client.request(method, path, json=body)
                self.assertIn(r.status_code, (401, 403))

    def test_environment_needs_no_browser_and_reports_strict_empty_pool(self):
        with mock.patch.object(server.proxy_mgr, 'candidates', return_value=[]), \
                mock.patch.object(type(server.proxy_mgr), 'force_proxy', new_callable=mock.PropertyMock, return_value=True), \
                mock.patch.object(server, '_atc_cfg', return_value={'enabled': True, 'key': 'secret-key', 'secret': 'secret-value'}):
            result = server._function_check_environment()
        checks = {x['id']: x for x in result['checks']}
        self.assertNotIn('browser', checks)
        self.assertEqual(checks['proxy']['code'], 'proxy_required')
        self.assertEqual(checks['parser']['status'], 'pending')
        self.assertNotIn('secret-key', json.dumps(result))
        self.assertNotIn('secret-value', json.dumps(result))

    def test_diagnostic_urls_reject_private_unknown_and_multiple(self):
        for text in ('https://127.0.0.1/', 'https://example.com/', 'https://www.tiktok.com.evil.test/video/12345678',
                     self.url + ' https://v.douyin.com/another/'):
            with self.subTest(text=text), self.assertRaises(server.ApiError):
                server._function_check_url(text)
        self.assertEqual(server._function_check_url('示例 ' + self.url), self.url)
        self.assertEqual(server._function_check_url('https://www.tiktok.com/@demo/video/7350000000000000001'),
                         'https://www.tiktok.com/@demo/video/7350000000000000001')

    def test_zero_is_complete_and_existing_counts_and_media_are_preserved(self):
        self.assertEqual(server._metadata_missing(self.full), [])
        old = copy.deepcopy(self.old)
        old['stats']['digg'] = 0
        full = copy.deepcopy(self.full)
        full['stats']['digg'] = 999
        full['video']['url'] = 'https://v3.douyinvod.com/other-source.mp4'
        merged = server._merge_metadata_snapshot(old, full)
        self.assertEqual(merged['stats']['digg'], 0)
        self.assertEqual(merged['video']['url'], old['video']['url'])
        self.assertEqual(merged['title'], '真实标题')
        self.assertEqual(old['title'], '暂无标题')

    def test_wrong_item_or_kind_cannot_be_merged(self):
        for values in ({'item_id': 'different'}, {'kind': 'note'}, {'platform': 'tiktok'}):
            with self.assertRaises(server.ApiError):
                server._merge_metadata_snapshot(self.old, dict(self.full, **values))

    def test_successful_parse_backfills_old_shares_without_changing_expiry_or_custom_title(self):
        share = self.share()
        row = dict(server.db_exec('SELECT * FROM shares WHERE id=?', (share['sid'],), 'one'))
        result = server._remember_parse_result(self.url, self.full, time.time())
        after = dict(server.db_exec('SELECT * FROM shares WHERE id=?', (share['sid'],), 'one'))
        self.assertEqual(after['title'], '真实标题')
        self.assertEqual(after['custom_title'], '自定义标题')
        self.assertEqual(after['expires_at'], row['expires_at'])
        self.assertEqual(after['refreshed_at'], row['refreshed_at'])
        self.assertNotIn('url', json.loads(after['payload'])['video'])
        with mock.patch.object(server, '_atc_extract', side_effect=AssertionError('upstream called')), \
                mock.patch.object(server, '_parse_douyin_share_direct', side_effect=AssertionError('upstream called')):
            view = server.api_share_get(share['sid'], make_request())
        self.assertEqual(view['data']['stats'], result['stats'])

    def test_inactive_or_other_work_shares_are_not_changed(self):
        a, b = self.share(), self.share(dict(self.old, item_id='other-work-id'))
        server.db_exec("UPDATE shares SET status='takedown' WHERE id=?", (a['sid'],))
        self.assertEqual(server._backfill_share_metadata(self.full), 0)
        self.assertEqual(server.db_exec('SELECT title FROM shares WHERE id=?', (b['sid'],), 'one')['title'], '暂无标题')

    def test_cache_retry_uses_official_supplement_once_and_updates_aliases(self):
        server._cache_put(self.url, self.old)
        server._cache_put('share text alias', self.old)
        with mock.patch.object(server, '_atc_cache_get', return_value={'work_url': self.url}), \
                mock.patch.object(server, '_atc_url_fresh', return_value=True), \
                mock.patch.object(server, '_complete_douyin_result', return_value=self.full) as complete, \
                mock.patch.object(server, '_parse_share', side_effect=AssertionError('primary resubmitted')):
            result = server._parse_cached(self.url)
            again = server._parse_cached(self.url)
        complete.assert_called_once()
        self.assertEqual(result['title'], again['title'])
        self.assertEqual(server._cache_get('share text alias')[1]['title'], '真实标题')

    def test_failed_cache_retry_keeps_media_and_cools_down(self):
        with mock.patch.object(server, '_complete_douyin_result', side_effect=TimeoutError('private upstream secret')) as complete:
            self.assertEqual(server._retry_cached_metadata(self.url, self.old), self.old)
            self.assertEqual(server._retry_cached_metadata(self.url, self.old), self.old)
        complete.assert_called_once()

    def test_parallel_cache_retry_does_not_duplicate_requests(self):
        started, release = threading.Event(), threading.Event()
        def wait(*args):
            started.set()
            release.wait(5)
            return self.full
        with mock.patch.object(server, '_complete_douyin_result', side_effect=wait) as complete:
            thread = threading.Thread(target=server._retry_cached_metadata, args=(self.url, self.old))
            thread.start()
            try:
                self.assertTrue(started.wait(2))
                self.assertEqual(server._retry_cached_metadata(self.url, self.old), self.old)
            finally:
                release.set()
                thread.join(5)
            complete.assert_called_once()

    def test_repair_reuses_snapshot_without_upstream_calls(self):
        share = self.share()
        server._save_parse_snapshot(self.full)
        with mock.patch.object(server, '_atc_extract', side_effect=AssertionError('paid call')), \
                mock.patch.object(server, '_douyin_resolve_share_url', side_effect=AssertionError('official call')):
            result = server._function_repair_share(share['sid'])
        self.assertEqual(result['updated'], 1)
        self.assertEqual(result['missing'], [])
        self.assertEqual(server._function_repair_share(share['sid'])['updated'], 0)

    def test_repair_cannot_revive_expired_share(self):
        share = self.share()
        server.db_exec('UPDATE shares SET expires_at=1 WHERE id=?', (share['sid'],))
        with self.assertRaises(server.ApiError):
            server._function_repair_share(share['sid'])

    def test_only_one_admin_job_can_run(self):
        server._function_check_job.update(state='running')
        with self.assertRaises(server.ApiError) as cm:
            server._start_function_check(work_url=self.url)
        self.assertEqual(cm.exception.status, 409)

    def test_media_probe_reads_one_byte_and_closes_response(self):
        response = mock.Mock()
        response.read.return_value = b'v'
        with mock.patch.object(server, '_open_primary_video_upstream', return_value=response) as opener, \
                mock.patch.object(server, '_close_upstream') as close:
            result = server._function_media_probe(self.full)
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(opener.call_args.args[1]['Range'], 'bytes=0-0')
        response.read.assert_called_once_with(1)
        close.assert_called_once_with(response)

    def test_empty_media_fails_and_closes_response(self):
        response = mock.Mock()
        response.read.return_value = b''
        with mock.patch.object(server, '_open_primary_video_upstream', return_value=response), \
                mock.patch.object(server, '_close_upstream') as close, self.assertRaises(server.ApiError):
            server._function_media_probe(self.full)
        close.assert_called_once_with(response)

    def test_worker_reports_primary_failure_separately_from_successful_fallback(self):
        native = dict(self.full, source='douyin_direct')
        with mock.patch.object(server, '_atc_extract', side_effect=server._ParserServiceError(503, 'hidden', reason='auth')), \
                mock.patch.object(server, '_douyin_resolve_share_url', return_value=('video', '7670572727590577894', self.url)), \
                mock.patch.object(server, '_parse_douyin_item_direct', return_value=native), \
                mock.patch.object(server, '_function_media_probe', return_value=server._check_item('media','pass','media_byte_ok')):
            server._function_check_worker(self.url, '')
        checks = {x['id']:x for x in server._function_check_job['checks']}
        self.assertEqual(checks['parser']['code'], 'parser_auth')
        self.assertEqual(checks['metadata']['status'], 'pass')
        self.assertEqual(checks['snapshot']['status'], 'pass')
        self.assertEqual(server._function_check_job['state'], 'done')

    def test_unknown_exception_never_leaks_from_worker(self):
        with mock.patch.object(server, '_function_repair_share', side_effect=RuntimeError('secret-key anytocopy https://private')):
            server._function_check_worker('', 'test-share')
        encoded = json.dumps(server._function_check_job)
        for private in ('secret-key','anytocopy','https://private'):
            self.assertNotIn(private, encoded)
        self.assertEqual(server._function_check_job['checks'][0]['status'], 'fail')


if __name__ == '__main__':
    unittest.main()
