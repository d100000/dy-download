"""浏览器标题旁路与媒体解析、持久快照的协作；不访问网络或真实浏览器。"""
import copy
import json
import time
import unittest
from unittest import mock

from tests.test_security_reliability import TestClient, make_request, server


class BrowserTitleIntegrationTests(unittest.TestCase):
    item_id = '7689777123456789012'
    work_url = 'https://www.douyin.com/video/7689777123456789012/'
    full_title = '这个完整标题由原作品浏览器页面补齐'

    def setUp(self):
        for table in ('shares', 'share_submissions', 'blocked_share_items',
                      'blocked_share_sources', 'quota_reservations', 'usage_daily',
                      'parse_snapshots', 'atc_cache'):
            server.db_exec('DELETE FROM ' + table)
        server._share_hits.clear()
        for cache in (server._cache, server._browser_title_jobs,
                      server._browser_title_results, server._douyin_note_media_cache):
            patcher = mock.patch.dict(cache, {}, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.data = {
            'item_id': self.item_id, 'kind': 'video', 'platform': 'douyin',
            'title': '（无标题）', 'author': '原作者', 'source': 'parser',
            'cover': 'https://p3.douyinpic.com/original.jpeg',
            'stats': {'digg': 0, 'comment': 1, 'share': 2, 'collect': 3},
            '_link': self.work_url,
            'video': {'url': 'https://v3.douyinvod.com/original.mp4',
                      'direct_url': 'https://v3.douyinvod.com/original.mp4',
                      'width': 1080, 'height': 1920, 'media_available': True},
        }
        self.result = {
            'item_id': self.item_id, 'kind': 'video',
            'title': self.full_title, 'title_source': 'browser_structured',
            'title_status': 'complete', '_link': self.work_url,
        }

    def complete(self, result=None):
        server._browser_title_complete(self.work_url, copy.deepcopy(
            self.result if result is None else result))

    def row(self, sid):
        return dict(server.db_exec('SELECT * FROM shares WHERE id=?', (sid,), 'one'))

    def share(self, data=None, custom_title=''):
        with mock.patch.object(server, 'current_user', return_value=None):
            return server._share_create(make_request('/api/share'),
                                        copy.deepcopy(data or self.data), custom_title)

    def pending_share(self, hint='本次分享的标题片段…'):
        request = make_request('/api/shares', headers={
            'Idempotency-Key': 'browser-title-' + str(time.monotonic_ns())})
        with mock.patch.object(server, '_wake_share_parse_workers'):
            response = server.api_share_create_async(server.AsyncShareBody(
                text=hint + ' https://v.douyin.com/BROWSERTEST/'), request)
        created = json.loads(response.body)['data']
        return created, server._claim_share_parse('browser-title-test-worker')

    def test_browser_starts_before_api_and_pending_title_does_not_delay_media(self):
        calls = []

        def start(url):
            self.assertEqual(url, self.work_url)
            calls.append('browser')
            return True

        def extract(*args, **kwargs):
            calls.append('api')
            self.assertEqual(calls, ['browser', 'api'])
            return {}

        with mock.patch.object(server, '_browser_title_start', side_effect=start), \
                mock.patch.object(server, '_atc_extract', side_effect=extract), \
                mock.patch.object(server, '_atc_result_to_parse', return_value=copy.deepcopy(self.data)), \
                mock.patch.object(server, '_complete_douyin_result',
                                  side_effect=AssertionError('串行元数据请求延迟媒体')):
            result = server._atc_parse_work_url(self.work_url)
        self.assertEqual(result['video']['url'], self.data['video']['url'])
        self.assertFalse(server._valid_title(result['title']))

    def test_browser_finishing_during_api_is_merged_into_immediate_response(self):
        def extract(*args, **kwargs):
            self.complete()
            return {}

        with mock.patch.object(server, '_browser_title_start', return_value=True), \
                mock.patch.object(server, '_atc_extract', side_effect=extract), \
                mock.patch.object(server, '_atc_result_to_parse', return_value=copy.deepcopy(self.data)), \
                mock.patch.object(server, '_complete_douyin_result', return_value=copy.deepcopy(self.data)):
            result = server._atc_parse_work_url(self.work_url)
        self.assertEqual(result['title'], self.full_title)
        self.assertEqual(result['video']['url'], self.data['video']['url'])

    def test_unavailable_browser_preserves_http_fallback(self):
        completed = dict(self.data, title='纯 HTTP 补充标题')
        with mock.patch.object(server, '_browser_title_start', return_value=False), \
                mock.patch.object(server, '_atc_extract', return_value={}), \
                mock.patch.object(server, '_atc_result_to_parse', return_value=copy.deepcopy(self.data)), \
                mock.patch.object(server, '_complete_douyin_result', return_value=completed) as fallback:
            result = server._atc_parse_work_url(self.work_url)
        fallback.assert_called_once()
        self.assertEqual(result['title'], completed['title'])

    def test_title_success_cannot_turn_media_failure_into_parse_success(self):
        self.complete()
        with mock.patch.object(server, '_browser_title_start', return_value=True), \
                mock.patch.object(server, '_atc_extract', side_effect=server.ApiError(503, '解析失败')), \
                mock.patch.object(server, '_parse_douyin_share_direct',
                                  side_effect=server.ApiError(503, '媒体未找到')):
            with self.assertRaises(server.ApiError):
                server._atc_parse_work_url(self.work_url)

    def test_enabled_browser_does_not_skip_known_wrong_media_identity_validation(self):
        wrong = dict(self.data, item_id='7689777123456789099')
        with mock.patch.object(server, '_browser_title_start', return_value=True), \
                mock.patch.object(server, '_atc_extract', return_value={}), \
                mock.patch.object(server, '_atc_result_to_parse', return_value=wrong), \
                mock.patch.object(server, '_complete_douyin_result',
                                  side_effect=server.ApiError(503, '错作品媒体无法使用')) as validation:
            with self.assertRaises(server.ApiError):
                server._atc_parse_work_url(self.work_url)
        validation.assert_called_once()

    def test_enabled_browser_keeps_album_media_normalization(self):
        work_url = self.work_url.replace('/video/', '/note/')
        album = dict(self.data, kind='note', _link=work_url,
                     images=[{'url': 'https://p3.douyinpic.com/original.jpeg'}])
        with mock.patch.object(server, '_browser_title_start', return_value=True), \
                mock.patch.object(server, '_atc_extract', return_value={}), \
                mock.patch.object(server, '_atc_result_to_parse', return_value=album), \
                mock.patch.object(server, '_douyin_needs_supplement', return_value=False), \
                mock.patch.object(server, '_douyin_resolve_share_url',
                                  return_value=('note', self.item_id, work_url)), \
                mock.patch.object(server, '_parse_douyin_item_direct',
                                  side_effect=AssertionError('完整图集无需额外元数据请求')):
            result = server._atc_parse_work_url(work_url)
        self.assertEqual(result['source'], 'douyin_direct')
        self.assertTrue(result['images'][0]['download_url'].startswith('/api/douyin/image/'))

    def test_browser_resolved_short_link_validates_primary_media_identity_when_ready_first(self):
        short_url = 'https://v.douyin.com/BROWSERTEST/'
        wrong = dict(self.data, item_id='7689777123456789099', _link=short_url)

        def extract(*args, **kwargs):
            server._browser_title_complete(short_url, copy.deepcopy(self.result))
            return {}

        with mock.patch.object(server, '_browser_title_start', return_value=True), \
                mock.patch.object(server, '_atc_extract', side_effect=extract), \
                mock.patch.object(server, '_atc_result_to_parse', return_value=wrong), \
                mock.patch.object(server, '_complete_douyin_result',
                                  side_effect=server.ApiError(503, '错作品媒体无法使用')) as validation:
            with self.assertRaises(server.ApiError):
                server._atc_parse_work_url(short_url)
        validation.assert_called_once_with(self.work_url, wrong)

    def test_failed_browser_does_not_change_successful_media_snapshot(self):
        server._remember_parse_result(self.work_url, self.data, time.time())
        before = server._get_parse_snapshot(self.item_id)
        server._browser_title_complete(self.work_url, None)
        self.assertEqual(server._get_parse_snapshot(self.item_id), before)
        self.assertEqual(server._browser_title_merge(self.work_url, self.data), self.data)

    def test_late_title_updates_snapshot_shares_and_cache_without_rebilling_or_expiry_extension(self):
        server._remember_parse_result(self.work_url, self.data, time.time())
        created = self.share(custom_title='管理员自行编辑的标题')
        before_row = self.row(created['sid'])
        before_snapshot = dict(server.db_exec('SELECT * FROM parse_snapshots WHERE item_id=?',
                                             (self.item_id,), 'one'))
        before_payload = json.loads(before_row['payload'])
        with mock.patch.object(server, 'reserve_quota', side_effect=AssertionError('重复预占额度')), \
                mock.patch.object(server, 'settle_quota', side_effect=AssertionError('重复结算额度')), \
                mock.patch.object(server, 'open_url', side_effect=AssertionError('回调不应发起网络请求')):
            self.complete()
        after_row = self.row(created['sid'])
        after_snapshot = dict(server.db_exec('SELECT * FROM parse_snapshots WHERE item_id=?',
                                            (self.item_id,), 'one'))
        payload = json.loads(after_row['payload'])
        self.assertEqual(payload['title'], self.full_title)
        self.assertEqual(after_row['title'], self.full_title)
        self.assertEqual(server._get_parse_snapshot(self.item_id)['title'], self.full_title)
        self.assertEqual(server._cache_get(self.work_url)[1]['title'], self.full_title)
        self.assertEqual(server._cache_get(self.work_url)[1]['video']['url'], self.data['video']['url'])
        self.assertEqual(server._share_view(after_row)['title'], '管理员自行编辑的标题')
        self.assertEqual(after_row['expires_at'], before_row['expires_at'])
        self.assertEqual(after_snapshot['expires_at'], before_snapshot['expires_at'])
        self.assertEqual(after_snapshot['created'], before_snapshot['created'])
        self.assertEqual(after_snapshot['source_url'], before_snapshot['source_url'])
        self.assertEqual(payload['cover'], before_payload['cover'])
        self.assertEqual(payload['stats'], before_payload['stats'])
        self.assertNotIn('url', payload['video'])

    def test_late_title_does_not_recreate_or_revive_expired_records(self):
        server._remember_parse_result(self.work_url, self.data, time.time())
        created = self.share()
        expired = int(time.time()) - 20
        server.db_exec('UPDATE parse_snapshots SET expires_at=? WHERE item_id=?', (expired, self.item_id))
        server.db_exec('UPDATE shares SET expires_at=? WHERE id=?', (expired, created['sid']))
        snapshot_before = dict(server.db_exec('SELECT * FROM parse_snapshots WHERE item_id=?',
                                             (self.item_id,), 'one'))
        share_before = self.row(created['sid'])
        self.complete()
        self.assertEqual(dict(server.db_exec('SELECT * FROM parse_snapshots WHERE item_id=?',
                                            (self.item_id,), 'one')), snapshot_before)
        self.assertEqual(self.row(created['sid']), share_before)
        server.db_exec('DELETE FROM parse_snapshots WHERE item_id=?', (self.item_id,))
        self.complete()
        self.assertIsNone(server.db_exec('SELECT * FROM parse_snapshots WHERE item_id=?',
                                        (self.item_id,), 'one'))

    def test_complete_browser_title_upgrades_partial_provider_title_and_its_filename(self):
        partial = dict(self.data, title='这个完整标题…', title_source='provider', title_status='partial')
        server._remember_parse_result(self.work_url, partial, time.time())
        created = self.share(partial)
        self.complete()
        payload = json.loads(self.row(created['sid'])['payload'])
        self.assertEqual(payload['title'], self.full_title)
        self.assertEqual(payload['title_status'], 'complete')
        self.assertEqual(payload['video']['filename'], server._short_download_title(self.full_title) + '.mp4')
        self.assertEqual(self.row(created['sid'])['title'], self.full_title)
        self.assertEqual(server._get_parse_snapshot(self.item_id)['title'], self.full_title)

    def test_available_provider_title_has_priority_over_browser_title(self):
        original = dict(self.data, title='接口可信原标题', title_source='provider', title_status='available')
        server._remember_parse_result(self.work_url, original, time.time())
        created = self.share(original)
        self.complete()
        self.assertEqual(server._get_parse_snapshot(self.item_id)['title'], original['title'])
        self.assertEqual(server._share_view(self.row(created['sid']))['title'], original['title'])
        self.assertEqual(server._browser_title_merge(self.work_url, original)['title'], original['title'])

    def test_late_title_upgrades_local_share_hint_without_leaking_it_into_shared_cache(self):
        created, item = self.pending_share()
        server._remember_parse_result(self.work_url, self.data, time.time())
        self.assertTrue(server._finish_share_parse_success(item, self.data))
        self.assertEqual(json.loads(self.row(created['sid'])['payload'])['title_source'], 'share_text')
        self.complete()
        saved = json.loads(self.row(created['sid'])['payload'])
        self.assertEqual(saved['title'], self.full_title)
        self.assertNotEqual(saved['title_source'], 'share_text')
        self.assertNotIn('title_hint', server._get_parse_snapshot(self.item_id))
        self.assertNotIn('title_hint', server._cache_get(self.item_id)[1])

    def test_title_between_snapshot_save_and_share_finish_survives_ready_transition(self):
        created, item = self.pending_share()
        stale = server._remember_parse_result(self.work_url, self.data, time.time())
        self.complete()
        self.assertTrue(server._finish_share_parse_success(item, stale))
        self.assertEqual(server._share_view(self.row(created['sid']))['title'], self.full_title)
        self.assertEqual(self.row(created['sid'])['parse_status'], 'ready')

    def test_synchronous_share_creation_merges_title_arriving_after_parse_returned(self):
        stale = server._remember_parse_result(self.work_url, self.data, time.time())
        self.complete()
        created = self.share(stale)
        self.assertEqual(created['title'], self.full_title)

    def test_later_stale_snapshot_and_cache_writes_cannot_erase_completed_browser_title(self):
        server._remember_parse_result(self.work_url, self.data, time.time())
        self.complete()
        returned = server._remember_parse_result(self.work_url, copy.deepcopy(self.data), time.time())
        self.assertEqual(returned['title'], self.full_title)
        self.assertEqual(server._get_parse_snapshot(self.item_id)['title'], self.full_title)
        self.assertEqual(server._cache_get(self.work_url)[1]['title'], self.full_title)

    def test_browser_metadata_never_crosses_item_kind_or_platform_identity(self):
        self.complete()
        for changes in ({'item_id': '7689777123456789099'}, {'kind': 'note'},
                        {'platform': 'tiktok'}, {'platform': ''}):
            with self.subTest(changes=changes):
                other = dict(self.data, **changes)
                self.assertEqual(server._browser_title_merge(self.work_url, other), other)

    def test_public_share_reads_never_start_browser_or_reserve_quota(self):
        created = self.share()
        self.complete()
        client = TestClient(server.app)
        self.addCleanup(client.close)
        with mock.patch.object(server, '_browser_title_start', side_effect=AssertionError('GET 启动了浏览器')), \
                mock.patch.object(server, 'reserve_quota', side_effect=AssertionError('GET 重复扣费')), \
                mock.patch.object(server, 'open_url', side_effect=AssertionError('GET 发起外部请求')):
            for path in ('/api/share/', '/api/shares/', '/s/'):
                response = client.get(path + created['sid'])
                self.assertEqual(response.status_code, 200)
                self.assertIn(self.full_title, response.text)


if __name__ == '__main__':
    unittest.main()
