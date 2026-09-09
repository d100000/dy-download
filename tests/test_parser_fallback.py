"""主解析优先、按需补全与公开错误边界回归；所有上游请求均隔离。"""
import copy
import json
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.test_security_reliability import server, TestClient, make_request


class ParserFallbackTests(unittest.TestCase):
    item_id = '7682023366300556537'
    work_url = 'https://v.douyin.com/OwlnZayZZU8/'
    canonical = 'https://www.douyin.com/video/7682023366300556537/'

    def setUp(self):
        for cache in (server._author_cache, server._douyin_result_cache,
                      server._douyin_media_cache, server._douyin_note_media_cache):
            patcher = mock.patch.dict(cache, {}, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        server.db_exec('DELETE FROM atc_cache')
        self.primary = {
            'workId': self.item_id, 'title': '主服务标题',
            'videoUrl': 'https://v3.douyinvod.com/primary.mp4',
            'duration': 9, 'width': 1920, 'height': 1080,
            'cover': 'https://p3.douyinpic.com/primary.jpeg',
            'author': {'nickname': '主服务作者', 'sec_uid': 'author-sec',
                       'unique_id': 'author-id', 'avatar': 'https://p3.douyinpic.com/avatar.jpeg',
                       'follower_count': 620201, 'total_favorited': '11000000'},
            'stats': {'digg': 0, 'comment': 7, 'collect': 8, 'share': 9},
        }
        self.native = {
            'aweme_id': self.item_id, 'desc': '官方补充标题',
            'author': {'nickname': '官方补充作者', 'sec_uid': 'author-sec',
                       'unique_id': 'author-id', 'avatar_thumb': {'url_list': ['https://p3.douyinpic.com/avatar.jpeg']},
                       'follower_count': 620000, 'signature': '作者简介'},
            'statistics': {'digg_count': 123, 'comment_count': 17,
                           'collect_count': 18, 'share_count': 19},
            'video': {'play_addr': {'url_list': ['https://v3.douyinvod.com/official.mp4']},
                      'duration': 12000, 'width': 1280, 'height': 720},
        }
        self.calls = []
        self.extract = mock.patch.object(server, '_atc_extract', side_effect=self.primary_response).start()
        self.resolve = mock.patch.object(server, '_douyin_resolve_share_url',
            return_value=('video', self.item_id, self.canonical)).start()
        self.direct = mock.patch.object(server, '_parse_douyin_item_direct', side_effect=self.official_response).start()
        self.addCleanup(mock.patch.stopall)

    def primary_response(self, *args, **kwargs):
        self.calls.append('primary')
        return copy.deepcopy(self.primary)

    def official_response(self, *args, **kwargs):
        self.calls.append('official')
        return server._douyin_native_result(copy.deepcopy(self.native), self.canonical, self.item_id)

    def test_complete_primary_does_not_call_official(self):
        result = server._parse_share(self.work_url)
        self.assertEqual(self.calls, ['primary'])
        self.assertEqual(result['source'], 'parser')
        self.assertEqual(result['stats']['digg'], 0)
        self.assertIn('/api/media/video/', result['video']['download_url'])
        self.resolve.assert_not_called()
        self.extract.assert_called_once_with(self.work_url, include_text=False)

    def test_missing_author_and_stats_are_filled_without_replacing_primary_media(self):
        self.primary.pop('author')
        self.primary['stats']['comment'] = None
        result = server._parse_share(self.work_url)
        self.assertEqual(self.calls, ['primary', 'official'])
        self.assertEqual(result['author'], '官方补充作者')
        self.assertEqual(result['stats']['comment'], 17)
        self.assertEqual(result['stats']['digg'], 0)
        self.assertEqual(result['title'], '主服务标题')
        self.assertEqual(result['video']['url'], self.primary['videoUrl'])
        self.assertEqual(result['video']['width'], 1920)
        self.assertIn('/api/media/video/', result['video']['download_url'])
        self.assertNotIn('video_id', result['video'])

    def test_missing_media_uses_official_route_but_preserves_primary_metadata(self):
        self.primary.pop('videoUrl')
        result = server._parse_share(self.work_url)
        self.assertEqual(self.calls, ['primary', 'official'])
        self.assertEqual(result['source'], 'douyin_direct')
        self.assertEqual(result['title'], '主服务标题')
        self.assertEqual(result['author'], '主服务作者')
        self.assertIn('/api/douyin/video/', result['video']['download_url'])
        self.assertEqual(server._author_cache[self.item_id][1]['follower_count'], 620201)

    def test_missing_duration_and_dimensions_are_filled(self):
        self.primary.update(duration=0, width=None, height=0)
        result = server._parse_share(self.work_url)
        self.assertEqual(result['duration_ms'], 12000)
        self.assertEqual((result['video']['width'], result['video']['height']), (1280, 720))

    def test_placeholder_title_is_replaced_by_official_caption(self):
        for title in ('暂无标题', '无标题', '（无标题）'):
            self.primary['title'] = title
            result = server._parse_share(self.work_url)
            self.assertEqual(result['title'], '官方补充标题')
            self.assertEqual(result['video']['url'], self.primary['videoUrl'])

    def test_byte_video_cdn_keeps_signed_download_when_supplement_fails(self):
        self.primary.update(title='暂无标题', author={},
                            videoUrl='https://v26-default.365yg.com/test/video/')
        self.primary.pop('workId')
        self.direct.side_effect = TimeoutError('official unavailable')
        result = server._parse_share(self.work_url)
        self.assertTrue(result['item_id'].startswith('item_'))
        self.assertEqual(result['video']['url'], self.primary['videoUrl'])
        self.assertIn('/api/media/video/', result['video']['download_url'])
        query = server.urlparse.parse_qs(server.urlparse.urlsplit(result['video']['download_url']).query)
        server._require_media_token('atc_video', result['item_id'], int(query['exp'][0]), query['sig'][0])

    def test_primary_failure_falls_back(self):
        self.extract.side_effect = server.ApiError(503, '视频解析服务暂时不可用')
        result = server._parse_share(self.work_url)
        self.assertEqual(result['source'], 'douyin_direct')
        self.direct.assert_called_once()

    def test_supplement_failure_keeps_usable_primary(self):
        self.primary.pop('author')
        self.direct.side_effect = TimeoutError('network detail')
        result = server._parse_share(self.work_url)
        self.assertEqual(result['source'], 'parser')
        self.assertEqual(result['video']['url'], self.primary['videoUrl'])

    def test_both_missing_media_paths_fail_instead_of_false_success(self):
        self.primary.pop('videoUrl')
        self.native['video'].pop('play_addr')
        with self.assertRaises(server.ApiError) as raised:
            server._parse_share(self.work_url)
        self.assertEqual(raised.exception.status, 503)

    def test_metadata_only_official_response_can_complete_primary(self):
        self.primary.pop('author')
        self.native['video'].pop('play_addr')
        result = server._parse_share(self.work_url)
        self.assertEqual(result['author'], '官方补充作者')
        self.assertEqual(result['video']['url'], self.primary['videoUrl'])

    def test_mismatched_work_is_never_merged_even_when_official_fails(self):
        self.primary.update(workId='7682023366300556599', author={})
        self.direct.side_effect = TimeoutError()
        with self.assertRaises(server.ApiError):
            server._parse_share(self.work_url)

    def test_other_platform_never_uses_douyin_supplement(self):
        self.primary.pop('author')
        result = server._parse_share('https://b23.tv/Abc123/')
        self.assertEqual(result['source'], 'parser')
        self.resolve.assert_not_called()
        self.direct.assert_not_called()

    def test_item_refresh_prioritizes_primary(self):
        result = server._parse_item('video', self.item_id)
        self.assertEqual(result['source'], 'parser')
        self.assertEqual(self.calls, ['primary'])

    def test_warm_primary_cache_does_not_force_official(self):
        key = self.work_url + ' cache-test'
        server._cache.pop(key, None)
        self.addCleanup(server._cache.pop, key, None)
        result = server._parse_cached(key)
        again = server._parse_cached(key)
        self.assertEqual(again['source'], 'parser')
        self.assertEqual(again['video']['url'], result['video']['url'])
        self.extract.assert_called_once()

    def test_author_enrichment_preserves_precision_and_zero_and_normalizes_counts(self):
        server._parse_share(self.work_url)
        server._author_cache[self.item_id][1]['following_count'] = 0
        with mock.patch.object(server, '_fetch_user_info', return_value={
                'follower_count': 620000, 'total_favorited': '11111111',
                'following_count': 200, 'aweme_count': '72', 'signature': ''}) as enrich:
            first = server.api_author(self.item_id)
            second = server.api_author(self.item_id)
        self.assertEqual(first['follower_count'], 620201)
        self.assertEqual(first['total_favorited'], 11000000)
        self.assertEqual(first['following_count'], 0)
        self.assertEqual(first['aweme_count'], 72)
        self.assertEqual(first, second)
        enrich.assert_called_once()

    def test_douyin_note_primary_uses_refreshable_signed_images(self):
        self.primary.pop('videoUrl')
        self.primary.update(workType='note', imageUrlList=['https://p3.douyinpic.com/note.jpeg'])
        result = server._parse_share(self.work_url)
        self.assertEqual(result['source'], 'douyin_direct')
        self.assertIn('/api/douyin/image/', result['images'][0]['download_url'])
        self.direct.assert_not_called()


    def test_background_media_submission_keeps_primary_first_and_completes_missing_media(self):
        self.primary.pop('videoUrl')
        job = {'item_id': self.item_id, 'work_url': self.work_url, 'purpose': 'play', 'created': int(time.time())}
        with mock.patch.object(server, '_atc_request', return_value={
                'code': 200, 'data': {**self.primary, 'status': 'SUCCESS'}}) as request, \
                mock.patch.object(server, '_atc_claim_update') as update:
            server._atc_submit_claimed(job, {})
        request.assert_called_once()
        self.direct.assert_called_once()
        update.assert_called_once_with(job, 'done', error=None)
        self.assertIsNotNone(server._douyin_cached_media(self.item_id))

    def test_background_rejection_uses_official_media_without_leaking_upstream(self):
        job = {'item_id': self.item_id, 'work_url': self.work_url, 'purpose': 'play', 'created': int(time.time())}
        with mock.patch.object(server, '_atc_request', return_value={
                'code': 500, 'msg': 'AnyToCopy upstream failed'}) as request, \
                mock.patch.object(server, '_atc_claim_update') as update:
            server._atc_submit_claimed(job, {})
        request.assert_called_once()
        update.assert_called_once_with(job, 'done', error=None)
        self.assertEqual(server._atc_cache_get(self.item_id)['work_url'], self.work_url)

    def test_expired_main_media_can_use_official_without_relaxing_range_validation(self):
        server._atc_save_result(self.item_id, {}, work_url=self.work_url)
        response = object()
        validator = lambda value: value
        with mock.patch.object(server, '_open_douyin_video_upstream', return_value=response) as official:
            result = server._open_atc_video_upstream(self.item_id, {'Range': 'bytes=0-0'}, validator)
        self.assertIs(result, response)
        official.assert_called_once_with(self.item_id, {'Range': 'bytes=0-0'}, validator)
        with mock.patch.object(server, '_open_primary_video_upstream', side_effect=server.ApiError(416, 'Range 无效')), \
                mock.patch.object(server, '_open_douyin_video_upstream') as official:
            with self.assertRaises(server.ApiError) as error:
                server._open_atc_video_upstream(self.item_id, {})
        self.assertEqual(error.exception.status, 416)
        official.assert_not_called()


class ProviderErrorBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)
        self.admin = server._new_session()
        self.client.cookies.set('admin_session', self.admin)
        self.addCleanup(server._sessions.pop, self.admin, None)

    def test_error_variants_and_urls_are_not_public(self):
        for raw in ('AnyToCopy token invalid', 'any two copy error', 'ANY_TO_COPY error',
                    'https://api.anytocopy.com/key?secret=sensitive', 'ATC failed'):
            with self.subTest(raw=raw):
                exc = server.ApiError(502, raw)
                self.assertEqual(exc.message, '服务暂时不可用，请稍后重试')
        self.assertEqual(server.ApiError(429, '今日免费次数已用完').message, '今日免费次数已用完')

    def test_admin_rejection_and_historical_errors_are_sanitized(self):
        with mock.patch.object(server, '_atc_cfg', return_value={
                'key':'test-key', 'secret':'test-secret'}), mock.patch.object(
                server, '_atc_request', return_value={'code':500,'msg':'AnyToCopy https://private.invalid secret=x'}):
            response = self.client.post('/api/admin/parser/test', json={})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('AnyToCopy', response.text)
        server.set_app_setting('atc_test_state', json.dumps({'state':'failed','error':'any two copy broken'}))
        result = self.client.get('/api/admin/parser').json()
        self.assertNotIn('base_url', result)
        self.assertNotRegex(json.dumps(result), r'(?i)any.?to.?copy|any two copy|api\.anytocopy')

    def test_legacy_paid_job_errors_are_sanitized_when_read(self):
        result = server._job_item_result({'status':'failed','error':'AnyToCopy secret=x'})
        self.assertEqual(result['error'], '解析失败，请稍后重试')

    def test_legacy_and_neutral_media_routes_enforce_same_signature(self):
        for path in ('/api/media/video/1234567890123456789', '/api/atc/video/1234567890123456789'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 403)

    def test_served_static_files_and_openapi_have_no_provider_brand(self):
        for path in Path('static').glob('*.html'):
            self.assertNotRegex(path.read_text(), r'(?i)any[\W_]*(?:to|two|2)[\W_]*copy')
        schema = self.client.get('/openapi.json').text
        self.assertNotRegex(schema, r'(?i)anytocopy|/api/atc/')


class DownloadRefreshTests(unittest.TestCase):
    item_id = 'item_download_refresh'
    work_url = 'https://v.douyin.com/OwlnZayZZU8/'
    old_url = 'https://v26-default.365yg.com/old.mp4'
    new_url = 'https://v26-default.365yg.com/new.mp4'

    def setUp(self):
        self.client = TestClient(server.app)
        for table in ('atc_jobs', 'atc_cache', 'blocked_share_items'):
            server.db_exec(f'DELETE FROM {table}')
        for cache in (server._media_hits, server._media_active):
            patcher = mock.patch.dict(cache, {}, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cfg = dict(server._atc_cfg(), enabled=True, key='test-key', secret='test-secret')
        patcher = mock.patch.object(server, '_atc_cfg', return_value=self.cfg)
        patcher.start()
        self.addCleanup(patcher.stop)
        for table in ('parse_snapshots', 'shares'):
            server.db_exec(f'DELETE FROM {table} WHERE item_id=?', (self.item_id,))
        self.endpoint = server._video_download_refresh_url(self.item_id)
        server._atc_save_result(self.item_id, {'videoUrl': self.old_url}, work_url=self.work_url)

    def expire(self):
        server.db_exec('UPDATE atc_cache SET url_fetched_at=? WHERE item_id=?',
                       (int(time.time()) - 7200, self.item_id))

    def submit(self, failed_url=''):
        return self.client.post(self.endpoint, json={'failed_url': failed_url})

    def finish_job(self, payload=None):
        job = server._atc_claim_pending('download-test')
        self.assertIsNotNone(job)
        with mock.patch.object(server, '_atc_request', return_value={
                'code': 200, 'data': payload or {'status': 'SUCCESS', 'videoUrl': self.new_url}}) as api, \
             mock.patch.object(server, '_complete_douyin_result', side_effect=AssertionError('no official calls')):
            server._atc_submit_claimed(job, self.cfg)
        api.assert_called_once_with('POST', '/video/extract', {'workUrl': self.work_url}, self.cfg)

    def test_fresh_cache_needs_no_job_or_quota(self):
        response = self.submit()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['url'], self.old_url)
        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        self.assertEqual(server.db_exec('SELECT COUNT(*) AS n FROM atc_jobs', fetch='one')['n'], 0)

    def test_expired_link_is_regenerated_from_saved_source_and_cached(self):
        self.expire()
        self.assertEqual(self.submit().status_code, 202)
        self.finish_job()
        response = self.submit()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['url'], self.new_url)
        self.assertTrue(server._atc_url_fresh(server._atc_cache_get(self.item_id), 3600))
        self.assertNotIn(self.work_url, response.text)
        self.assertNotIn('test-secret', response.text)

    def save_snapshot(self):
        data = {'item_id': self.item_id, 'kind': 'video', 'platform': 'douyin',
                'source': 'parser', 'title': '已保存的标题', 'author': '作者',
                'stats': {'digg': 0, 'comment': 12}, 'video': {'source': 'parser',
                'url': self.old_url, 'filename': 'original.mp4'}}
        return server._remember_parse_result('分享文案 ' + self.work_url, data, time.time())

    def test_saved_source_survives_media_cache_clear_and_is_not_public(self):
        self.save_snapshot()
        server.db_exec('DELETE FROM atc_cache WHERE item_id=?', (self.item_id,))
        server._cache.clear()
        with mock.patch.object(server, 'reserve_quota', side_effect=AssertionError('no charge')):
            response = self.submit()
        self.assertEqual(response.status_code, 202)
        job = server.db_exec('SELECT * FROM atc_jobs WHERE item_id=?', (self.item_id,), 'one')
        self.assertEqual(job['work_url'], self.work_url)
        self.finish_job()
        saved = server._get_parse_snapshot(self.item_id)
        self.assertEqual(saved['stats'], {'digg': 0, 'comment': 12})
        self.assertEqual(saved['title'], '已保存的标题')
        self.assertNotIn(self.work_url, json.dumps(saved))
        self.assertNotIn(self.old_url, json.dumps(saved))
        self.assertEqual(self.submit().json()['url'], self.new_url)

    def test_share_keeps_source_beyond_parse_snapshot_and_media_cache(self):
        data = self.save_snapshot()
        share = server._share_create(make_request(), data, '自定义标题')
        row = server.db_exec('SELECT * FROM shares WHERE id=?', (share['sid'],), 'one')
        self.assertEqual(row['source_url'], self.work_url)
        for table in ('parse_snapshots', 'atc_cache'):
            server.db_exec(f'DELETE FROM {table} WHERE item_id=?', (self.item_id,))
        with mock.patch.object(server, '_atc_extract', side_effect=AssertionError('GET is read only')):
            view = server.api_share_get(share['sid'], make_request())
        self.assertEqual(view['title'], '自定义标题')
        self.assertEqual(view['data']['stats']['comment'], 12)
        self.assertNotIn(self.work_url, json.dumps(view))
        self.assertEqual(self.submit().status_code, 202)

    def test_official_snapshot_reuses_refreshed_primary_media_on_next_visit(self):
        data = self.save_snapshot()
        data['source'] = data['video']['source'] = 'douyin_direct'
        share = server._share_create(make_request(), data)
        server._atc_save_result(self.item_id, {'videoUrl': self.new_url}, work_url=self.work_url)
        with mock.patch.object(server, '_parse_share', side_effect=AssertionError('read only')):
            view = server.api_share_get(share['sid'], make_request())
        self.assertEqual(view['data']['video']['source'], 'parser')
        self.assertEqual(view['data']['video']['direct_url'], self.new_url)
        self.assertEqual(view['data']['stats']['digg'], 0)

    def test_expired_snapshots_and_shares_cannot_restore_source(self):
        data = self.save_snapshot()
        share = server._share_create(make_request(), data)
        for table in ('shares', 'parse_snapshots'):
            server.db_exec(f'UPDATE {table} SET expires_at=1 WHERE item_id=?', (self.item_id,))
        server.db_exec('DELETE FROM atc_cache WHERE item_id=?', (self.item_id,))
        self.assertEqual(self.submit().status_code, 404)
        self.assertEqual(server.db_exec('SELECT COUNT(*) AS n FROM atc_jobs', fetch='one')['n'], 0)

    def test_refresh_with_sparse_metadata_does_not_erase_saved_counts(self):
        data = self.save_snapshot()
        data.update(title='', author='', stats={'digg': None})
        result = server._remember_parse_result(self.work_url, data, time.time())
        self.assertEqual(result['title'], '已保存的标题')
        self.assertEqual(result['stats']['digg'], 0)
        self.assertEqual(result['stats']['comment'], 12)

    def test_canonical_source_is_used_without_tracking_parameters(self):
        data = self.save_snapshot()
        data['_link'] = 'https://www.douyin.com/jingxuan?modal_id=7682023366300556537&private=secret'
        server._save_parse_snapshot(data)
        server.db_exec('DELETE FROM atc_cache WHERE item_id=?', (self.item_id,))
        self.assertEqual(self.submit().status_code, 202)
        job = server.db_exec('SELECT * FROM atc_jobs WHERE item_id=?', (self.item_id,), 'one')
        self.assertEqual(job['work_url'], 'https://www.douyin.com/video/7682023366300556537/')
        saved = server.db_exec('SELECT * FROM parse_snapshots WHERE item_id=?', (self.item_id,), 'one')
        self.assertEqual(saved['source_url'], self.work_url)
        self.assertNotIn('private=secret', saved['canonical_url'])

    def test_browser_failure_refreshes_even_when_ttl_has_not_expired(self):
        self.assertEqual(self.submit(self.old_url).status_code, 202)
        self.finish_job()
        for _ in range(3):
            self.assertEqual(self.submit(self.old_url).json()['url'], self.new_url)
        self.assertEqual(server.db_exec('SELECT COUNT(*) AS n FROM atc_jobs', fetch='one')['n'], 1)

    def test_concurrent_expiry_reports_create_one_job(self):
        from concurrent.futures import ThreadPoolExecutor
        self.expire()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: server._request_download_link(self.item_id, self.old_url), range(8)))
        self.assertTrue(all(r['status'] == 'processing' for r in results))
        self.assertEqual(server.db_exec('SELECT COUNT(*) AS n FROM atc_jobs', fetch='one')['n'], 1)

    def test_provider_returning_same_url_does_not_create_an_infinite_job_loop(self):
        self.assertEqual(self.submit(self.old_url).status_code, 202)
        self.finish_job({'status': 'SUCCESS', 'videoUrl': self.old_url})
        self.assertEqual(self.submit(self.old_url).json()['status'], 'ready')
        self.assertEqual(server.db_exec('SELECT COUNT(*) AS n FROM atc_jobs', fetch='one')['n'], 1)

    def test_failed_worker_has_cooldown_and_no_official_fallback(self):
        self.expire()
        self.assertEqual(self.submit().status_code, 202)
        self.finish_job({'status': 'SUCCESS', 'title': 'only metadata'})
        response = self.submit()
        self.assertEqual(response.status_code, 502)
        self.assertNotRegex(response.text, '(?i)anytocopy|test-secret')
        self.assertEqual(server.db_exec('SELECT COUNT(*) AS n FROM atc_jobs', fetch='one')['n'], 1)

    def test_timed_out_job_stops_polling_instead_of_recreating_jobs(self):
        self.expire()
        self.submit()
        server.db_exec('UPDATE atc_jobs SET created=?', (int(time.time()) - server.ATC_JOB_TIMEOUT - 1,))
        self.assertEqual(self.submit().status_code, 504)
        self.assertEqual(self.submit().status_code, 502)

    def test_signature_scope_source_and_takedown_are_enforced(self):
        base = self.endpoint.split('?')[0]
        self.assertEqual(self.client.post(base, json={}).status_code, 403)
        self.assertEqual(self.client.post(self.endpoint.replace(self.item_id, 'item_another_video'), json={}).status_code, 403)
        stream = server._atc_video_proxy_url(self.item_id)
        self.assertEqual(self.client.post(base + '?' + stream.split('?')[1], json={}).status_code, 403)
        server.db_exec("INSERT INTO blocked_share_items(kind,item_id,created) VALUES('video',?,?)",
                       (self.item_id, int(time.time())))
        self.assertEqual(self.submit().status_code, 451)

    def test_client_url_is_never_used_as_a_new_source(self):
        self.expire()
        self.assertEqual(self.submit('https://malicious.example/private').status_code, 202)
        job = server.db_exec('SELECT work_url FROM atc_jobs', fetch='one')
        self.assertEqual(job['work_url'], self.work_url)

    def test_no_saved_source_does_not_start_a_task(self):
        self.expire()
        server.db_exec("UPDATE atc_cache SET work_url='' WHERE item_id=?", (self.item_id,))
        self.assertEqual(self.submit().status_code, 404)
        self.assertEqual(server.db_exec('SELECT COUNT(*) AS n FROM atc_jobs', fetch='one')['n'], 0)

    def test_expired_share_exposes_click_refresh_but_never_refreshes_on_view(self):
        data = server._atc_result_to_parse(self.work_url, {'videoUrl': self.old_url,
            'title': '保留的标题', 'author': '原作者', 'stats': {'digg': 0, 'comment': 21}}, self.item_id)
        share = server._share_create(make_request('/api/share'), data)
        self.expire()
        before = server.db_exec('SELECT payload FROM shares WHERE id=?', (share['sid'],), 'one')['payload']
        with mock.patch.object(server, '_atc_enqueue', side_effect=AssertionError('no task on view')):
            view = self.client.get('/api/share/' + share['sid']).json()
        self.assertNotIn('direct_url', view['data']['video'])
        self.assertIn('download_refresh_url', view['data']['video'])
        self.assertEqual(server.db_exec('SELECT COUNT(*) AS n FROM atc_jobs', fetch='one')['n'], 0)
        self.submit()
        self.finish_job()
        self.assertEqual(server.db_exec('SELECT payload FROM shares WHERE id=?', (share['sid'],), 'one')['payload'], before)
        self.assertNotIn('download_refresh_url', json.loads(before)['video'])
        self.assertEqual(self.client.get('/api/share/' + share['sid']).json()['data']['video']['direct_url'], self.new_url)

    def test_async_provider_job_can_complete_through_the_worker_poll(self):
        self.expire()
        self.submit()
        job = server._atc_claim_pending('download-test')
        with mock.patch.object(server, '_atc_request', return_value={
                'code': 200, 'data': {'taskId': 'test-task', 'status': 'PROCESSING'}}):
            server._atc_submit_claimed(job, self.cfg)
        self.assertEqual(self.submit().json()['status'], 'processing')
        server.db_exec('UPDATE atc_jobs SET updated=?', (int(time.time()) - 10,))
        job = server._atc_claim_submitted('download-test')
        self.assertIsNotNone(job)
        with mock.patch.object(server, '_atc_request', return_value={'code': 200, 'data': {
                'status': 'SUCCESS', 'result': {'video': {'url': self.new_url}},
                'textContent': '不得请求或保存的转录'}}), \
             mock.patch.object(server, '_complete_douyin_result', side_effect=AssertionError('no official calls')):
            server._atc_poll_claimed(job, self.cfg)
        self.assertEqual(self.submit().json()['url'], self.new_url)
        self.assertFalse(server._atc_cache_get(self.item_id)['text_content'])

    def test_queue_bound_and_request_limits_do_not_leave_active_leases(self):
        self.expire()
        for i in range(32):
            server.db_exec("INSERT INTO atc_jobs(item_id,work_url,purpose,status,created,updated) "
                           "VALUES(?,?,'download','pending',?,?)",
                           (f'queue_item_{i}', self.work_url, int(time.time()), int(time.time())))
        self.assertEqual(self.submit().status_code, 503)
        self.assertFalse(server._media_active)
        server._media_hits.clear()
        with mock.patch.object(server, '_media_lease', side_effect=server.ApiError(429, '请求过于频繁')):
            self.assertEqual(self.submit().status_code, 429)

    def test_expired_queued_download_does_not_submit_a_late_task(self):
        self.expire()
        self.submit()
        server.db_exec('UPDATE atc_jobs SET created=?', (int(time.time()) - server.ATC_JOB_TIMEOUT - 1,))
        job = server._atc_claim_pending('download-test')
        with mock.patch.object(server, '_atc_request', side_effect=AssertionError('no late submit')):
            server._atc_submit_claimed(job, self.cfg)
        self.assertEqual(self.submit().status_code, 502)



class TikTokAndProxyTests(unittest.TestCase):
    url = 'https://www.tiktok.com/@creator/video/6718335390845095173'
    media = 'https://v16-webapp-prime.tiktok.com/video/test.mp4'

    def payload(self):
        return {'workId': '6718335390845095173', 'video_description': '测试标题 #original',
                'videoUrl': self.media, 'create_time': 1700000000, 'duration': 12,
                'width': 1080, 'height': 1920,
                'author': {'nickname': 'Original 作者', 'uniqueId': 'creator',
                           'follower_count': 0},
                'like_count': 1234, 'comment_count': 0, 'share_count': 8}

    def test_tiktok_normal_and_short_share_links(self):
        for url in [self.url, self.url + '?is_from_webapp=1',
                    'https://vm.tiktok.com/ZM123abc/', 'https://vt.tiktok.com/ZS123abc/']:
            self.assertEqual(server._normalize_share_short_link('分享 ' + url), url)
        for bad in ['https://tiktok.com.evil.example/@creator/video/6718335390845095173',
                    'https://user:secret@www.tiktok.com/@creator/video/6718335390845095173',
                    'https://www.tiktok.com/@creator', 'http://vm.tiktok.com/ZM123abc/']:
            with self.assertRaises(server.ApiError):
                server._normalize_share_short_link(bad)
        with self.assertRaises(server.ApiError):
            server._normalize_share_short_link(self.url + ' https://v.douyin.com/abc123/')

    def test_tiktok_parse_preserves_metadata_and_uses_primary_only(self):
        with mock.patch.object(server, '_atc_extract', return_value=self.payload()), \
             mock.patch.object(server, '_parse_douyin_item_direct', side_effect=AssertionError('not Douyin')):
            data = server._parse_share(self.url)
        self.assertEqual(data['platform'], 'tiktok')
        self.assertTrue(data['share_supported'])
        self.assertEqual(data['title'], '测试标题 #original')
        self.assertEqual(data['author_url'], 'https://www.tiktok.com/@creator')
        self.assertEqual(data['stats'], {'digg': 1234, 'comment': 0, 'share': 8, 'collect': None})
        self.assertEqual(data['video']['direct_url'], self.media)
        self.assertIn('/api/media/video/tiktok_', data['video']['download_url'])
        self.assertIn('/download-link?', data['video']['download_refresh_url'])
        self.assertEqual(data['original_url'], self.url)
        self.assertEqual(data['author_detail']['follower_count'], 0)

    def test_tiktok_share_reads_snapshot_without_upstream_and_localizes_page(self):
        data = server._atc_result_to_parse(self.url, self.payload())
        share = server._share_create(make_request(), data)
        with mock.patch.object(server, '_atc_extract', side_effect=AssertionError('snapshot only')), \
             mock.patch.object(server, '_atc_enqueue', side_effect=AssertionError('snapshot only')):
            response = TestClient(server.app).get('/s/' + share['sid'] + '?lang=en')
        self.assertEqual(response.status_code, 200)
        self.assertIn('<html lang="en">', response.text)
        self.assertIn("const LANG = 'en';", response.text)
        self.assertIn('TikTok video by @Original 作者', response.text)
        self.assertIn('测试标题 #original', response.text)
        self.assertIn('"comment": 0', response.text)
        self.assertIn('lang=en', response.headers['set-cookie'])

    def test_primary_cdn_allowlist_rejects_lookalikes_and_private_urls(self):
        for url in [self.media, 'https://v1.tiktokcdn.com/video',
                    'https://v1.tiktokcdn-us.com/video', 'https://v3.douyinvod.com/a.mp4']:
            self.assertTrue(server._primary_media_allowed(url), url)
        for url in ['http://v1.tiktokcdn.com/a', 'https://127.0.0.1/a',
                    'https://v1.tiktokcdn.com.evil.example/a', 'https://user:secret@v1.tiktokcdn.com/a',
                    'https://www.tiktok.com/@creator/video/6718335390845095173',
                    'https://v1.tiktokcdn.com:8443/a']:
            self.assertFalse(server._primary_media_allowed(url), url)

    def test_tiktok_server_media_preserves_range_and_platform_referer(self):
        data = server._atc_result_to_parse(self.url, self.payload())
        upstream = mock.Mock(status=206, headers={'Content-Type': 'video/mp4'})
        upstream.geturl.return_value = self.media
        with mock.patch.object(server, 'open_url', return_value=(upstream, None)) as opened:
            self.assertIs(server._open_primary_video_upstream(data['item_id'], {'Range':'bytes=0-0'}), upstream)
        self.assertEqual(opened.call_args.kwargs['headers']['Range'], 'bytes=0-0')
        self.assertEqual(opened.call_args.kwargs['headers']['Referer'], 'https://www.tiktok.com/')

    def test_proxy_empty_uses_direct_by_default(self):
        with mock.patch.dict(server.proxy_mgr.settings, {'force_proxy': False}), \
             mock.patch.object(server.proxy_mgr, 'candidates', return_value=[]), \
             mock.patch.object(server, '_raw_open', return_value='response') as raw:
            response, proxy = server.open_url(self.media)
        self.assertEqual(response, 'response')
        self.assertIsNone(proxy)
        self.assertIsNone(raw.call_args.args[-1])

    def test_proxy_failures_fall_back_to_direct_in_order(self):
        proxies = [{'url': 'http://one.example:80'}, {'url':'http://two.example:80'}]
        attempts = []
        def raw(url, follow, headers, timeout, proxy):
            attempts.append(proxy)
            if proxy: raise OSError('unavailable')
            return 'response'
        with mock.patch.dict(server.proxy_mgr.settings, {'force_proxy': False, 'retries': 2}), \
             mock.patch.object(server.proxy_mgr, 'candidates', return_value=proxies), \
             mock.patch.object(server.proxy_mgr, 'mark_fail'), \
             mock.patch.object(server, '_raw_open', side_effect=raw):
            self.assertEqual(server.open_url(self.media), ('response', None))
        self.assertEqual(attempts, [*proxies, None])

    def test_successful_proxy_never_uses_direct(self):
        proxy = {'url': 'http://one.example:80'}
        with mock.patch.dict(server.proxy_mgr.settings, {'force_proxy': False}), \
             mock.patch.object(server.proxy_mgr, 'candidates', return_value=[proxy]), \
             mock.patch.object(server.proxy_mgr, 'mark_ok'), \
             mock.patch.object(server, '_raw_open', return_value='response') as raw:
            self.assertEqual(server.open_url(self.media), ('response', proxy))
        self.assertEqual(raw.call_count, 1)

    def test_explicit_strict_mode_still_disallows_direct(self):
        with mock.patch.dict(server.proxy_mgr.settings, {'force_proxy': True}), \
             mock.patch.object(server.proxy_mgr, 'candidates', return_value=[]), \
             mock.patch.object(server, '_raw_open') as raw:
            with self.assertRaises(server.ApiError): server.open_url(self.media)
        raw.assert_not_called()

    def test_old_proxy_default_is_migrated_once(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(server, 'STORE_FILE', Path(folder) / 'config.json'):
            server.STORE_FILE.write_text(json.dumps({'proxies': [], 'settings': {'force_proxy': True}}))
            manager = server.ProxyManager()
            self.assertFalse(manager.force_proxy)
            manager.settings['force_proxy'] = True
            manager._save()
            self.assertTrue(server.ProxyManager().force_proxy)
            self.assertEqual(json.loads(server.STORE_FILE.read_text())['settings']['proxy_policy_version'], 1)

    def test_english_api_errors_do_not_expose_provider_or_credentials(self):
        import asyncio
        response = asyncio.run(server._api_error(make_request(headers={'Accept-Language': 'en'}),
                                   server.ApiError(502, '视频解析服务暂时不可用，请稍后重试')))
        message = json.loads(response.body)['error']
        self.assertEqual(message, 'The video service is unavailable. Please try again later.')


class TikTokDownloadRefreshTests(DownloadRefreshTests):
    """The same expiry/concurrency/snapshot contract must hold for TikTok."""
    item_id = 'tiktok_6718335390845095173'
    work_url = 'https://vm.tiktok.com/ZM123abc/'
    old_url = 'https://v16-webapp-prime.tiktok.com/old.mp4'
    new_url = 'https://v16-webapp-prime.tiktok.com/new.mp4'


if __name__ == '__main__':
    unittest.main()


class ParserDocumentContractTests(unittest.TestCase):
    """按 2026-09-09 官方视频文档构造响应，和真实联网验证分别报告。"""
    work_url = 'https://v.douyin.com/qFnZK0HPwBo/'
    media = 'https://v3.douyinvod.com/contract-final.mp4'

    def setUp(self):
        self.cfg = dict(server._atc_cfg(), enabled=True, key='contract-key', secret='contract-secret')
        patcher = mock.patch.object(server, '_atc_cfg', return_value=self.cfg)
        patcher.start()
        self.addCleanup(patcher.stop)
        for table in ('atc_jobs', 'atc_cache'):
            server.db_exec(f'DELETE FROM {table}')
        server.db_exec("DELETE FROM app_settings WHERE k='atc_test_state'")
        self.client = TestClient(server.app)
        self.assertEqual(self.client.post('/api/admin/login', json={
            'password': 'test-only-admin-password'}).status_code, 200)

    def payload(self, status='SUCCESS'):
        return {'status': status, 'taskId': 'contract-task', 'title': '作品标题',
                'content': '完整正文 #原文', 'videoUrl': self.media,
                'videoUrlList': [self.media], 'imageUrlList': [],
                'duration': 156.36 if status == 'SUCCESS' else None,
                'workType': 'video', 'createBy': '60227',
                'createTime': '2026-01-07 15:27:42', 'textContent': ''}

    def job(self, purpose='download'):
        now = int(time.time())
        server.db_exec('INSERT INTO atc_jobs(item_id,work_url,purpose,status,created,updated) '
                       "VALUES('contract-item',?,?,'pending',?,?)", (self.work_url, purpose, now, now))
        return server._atc_claim_pending('contract-worker')

    def poll_job(self):
        server.db_exec('UPDATE atc_jobs SET updated=?', (int(time.time()) - 10,))
        job = server._atc_claim_submitted('contract-worker')
        self.assertIsNotNone(job)
        return job

    def row(self):
        return dict(server.db_exec('SELECT * FROM atc_jobs ORDER BY id DESC LIMIT 1', fetch='one'))

    def test_query_parameters_headers_and_post_body_follow_video_contract(self):
        import io
        for method, path, params in (
                ('POST', '/video/extract', {'workUrl': self.work_url}),
                ('POST', '/video/extract', {'workUrl': self.work_url, 'taskType': 'TEXT'}),
                ('GET', '/video/query', {'taskId': 'contract-task'})):
            with self.subTest(method=method, params=params), mock.patch.object(
                    server.urlreq, 'urlopen', return_value=io.BytesIO(b'{"code":200,"data":"task"}')) as opening:
                self.assertEqual(server._atc_request(method, path, params, self.cfg)['code'], 200)
                req = opening.call_args.args[0]
                self.assertEqual(req.method, method)
                self.assertEqual(server.urlparse.parse_qs(server.urlparse.urlsplit(req.full_url).query),
                                 {k: [v] for k, v in params.items()})
                self.assertTrue(req.full_url.startswith(server.ATC_DEFAULT_BASE + path + '?'))
                self.assertEqual(req.get_header('X-api-key'), self.cfg['key'])
                self.assertEqual(req.get_header('X-api-secret'), self.cfg['secret'])
                self.assertIsNone(req.data)
                self.assertNotIn(self.cfg['secret'], req.full_url)

    def test_response_size_and_invalid_json_are_bounded_and_neutral(self):
        import io
        for raw in (b'x' * (server.ATC_MAX_RESPONSE_BYTES + 1), b'<html>anytocopy secret</html>',
                    b'[]', b'\xff'):
            with self.subTest(size=len(raw)), mock.patch.object(
                    server.urlreq, 'urlopen', return_value=io.BytesIO(raw)):
                with self.assertRaises(server.ApiError) as failed:
                    server._atc_request('GET', '/video/query', {'taskId': 't'}, self.cfg)
                self.assertNotRegex(failed.exception.message, r'(?i)anytocopy|secret|https?://')

    def test_http_auth_is_terminal_and_throttle_or_server_error_are_retryable(self):
        import io
        for code, retryable, reason in ((401, False, 'auth'), (403, False, 'auth'),
                                        (429, True, 'busy'), (502, True, 'failed'), (404, False, 'failed')):
            body = io.BytesIO(b'private response')
            error = server.urlerr.HTTPError('https://api.invalid/?secret=x', code, 'raw', {}, body)
            with self.subTest(code=code), mock.patch.object(server.urlreq, 'urlopen', side_effect=error):
                with self.assertRaises(server._ParserServiceError) as failed:
                    server._atc_request('GET', '/video/query', {}, self.cfg)
                self.assertEqual(failed.exception.retryable, retryable)
                self.assertEqual(failed.exception.reason, reason)
                self.assertTrue(body.closed)

    def test_waiting_and_processing_media_are_not_returned_before_success(self):
        responses = [{'code': 200, 'data': self.payload(status)}
                     for status in ('WAITING', 'PROCESSING', 'SUCCESS')]
        with mock.patch.object(server, '_atc_request', side_effect=responses) as api, \
                mock.patch.object(server.time, 'sleep'):
            result = server._atc_extract(self.work_url)
        self.assertEqual(result['duration'], 156.36)
        self.assertEqual(result['content'], '完整正文 #原文')
        self.assertEqual([c.args[0] for c in api.call_args_list], ['POST', 'GET', 'GET'])
        self.assertEqual(api.call_args_list[-1].args[2], {'taskId': 'contract-task'})

    def test_failure_with_media_is_rejected_on_submission_and_query(self):
        for status in ('FAILED', 'FAILURE', 'ERROR'):
            for immediate in (True, False):
                responses = [{'code': 200, 'data': self.payload(status)}]
                if not immediate:
                    responses.insert(0, {'code': 200, 'data': 'contract-task'})
                with self.subTest(status=status, immediate=immediate), mock.patch.object(
                        server, '_atc_request', side_effect=responses), mock.patch.object(server.time, 'sleep'):
                    with self.assertRaises(server.ApiError):
                        server._atc_extract(self.work_url)

    def test_pending_without_task_id_is_rejected_even_when_media_is_present(self):
        data = self.payload('WAITING')
        del data['taskId']
        with mock.patch.object(server, '_atc_request', return_value={'code': 200, 'data': data}):
            with self.assertRaises(server.ApiError):
                server._atc_extract(self.work_url)

    def test_legacy_stateless_result_remains_compatible_but_creator_status_is_ignored(self):
        data = self.payload()
        del data['status']
        data['author'] = {'status': 'FAILED', 'taskId': 'wrong-author-task'}
        self.assertTrue(server._atc_result_complete(data))
        self.assertFalse(server._atc_result_complete(data, include_text=True))
        self.assertEqual(server._atc_status_value(data), '')
        self.assertEqual(server._atc_task_id({'author': data['author']}), '')
        self.assertFalse(server._atc_result_complete({**data, 'status': 'NEW_PENDING_STATUS'}))

    def test_transient_query_failure_retries_same_task_without_new_post(self):
        responses = [{'code': 200, 'data': 'contract-task'},
                     server._ParserServiceError(502, '暂时不可用', retryable=True),
                     {'code': 429, 'msg': '限流'}, {'code': 200, 'data': self.payload()}]
        with mock.patch.object(server, '_atc_request', side_effect=responses) as api, \
                mock.patch.object(server.time, 'sleep') as sleep:
            self.assertEqual(server._atc_extract(self.work_url)['status'], 'SUCCESS')
        self.assertEqual([c.args[0] for c in api.call_args_list], ['POST', 'GET', 'GET', 'GET'])
        self.assertEqual(sleep.call_args_list[-1].args[0], 5)

    def test_polling_has_a_hard_attempt_limit(self):
        with mock.patch.object(server, '_atc_request', return_value={
                'code': 200, 'data': self.payload('WAITING')}) as api, \
                mock.patch.object(server.time, 'sleep'), mock.patch.object(server, 'ATC_MAX_POLLS', 2):
            with self.assertRaises(server.ApiError) as failed:
                server._atc_extract(self.work_url)
        self.assertEqual(failed.exception.status, 504)
        self.assertEqual(api.call_count, 3)

    def test_title_body_and_snapshot_preserve_content_without_task_creator_metadata(self):
        result = server._atc_result_to_parse(self.work_url, self.payload(), 'contract-item')
        stored = server._share_storage_payload(result)
        self.assertEqual(stored['title'], '作品标题')
        self.assertEqual(stored['content'], '完整正文 #原文')
        self.assertEqual(stored['author'], '')
        self.assertIsNone(stored['create_time'])
        self.assertTrue(all(v is None for v in stored['stats'].values()))
        self.assertNotIn('textContent', stored)
        self.assertNotIn('createBy', stored)

    def test_background_waiting_media_does_not_update_cache_then_success_does(self):
        job = self.job()
        with mock.patch.object(server, '_atc_request', return_value={
                'code': 200, 'data': self.payload('WAITING')}):
            server._atc_submit_claimed(job, self.cfg)
        self.assertEqual(self.row()['status'], 'submitted')
        self.assertIsNone(server._atc_cache_get('contract-item'))
        for status in ('PROCESSING', 'SUCCESS'):
            job = self.poll_job()
            with mock.patch.object(server, '_atc_request', return_value={
                    'code': 200, 'data': self.payload(status)}):
                server._atc_poll_claimed(job, self.cfg)
            if status == 'PROCESSING':
                self.assertIsNone(server._atc_cache_get('contract-item'))
        self.assertEqual(self.row()['status'], 'done')
        self.assertEqual(server._atc_cache_get('contract-item')['video_url'], self.media)

    def test_worker_submission_and_poll_reject_failure_media(self):
        for status in ('FAILED', 'FAILURE'):
            for poll in (False, True):
                for purpose in ('play', 'download', 'transcript'):
                    server.db_exec('DELETE FROM atc_jobs')
                    job = self.job(purpose)
                    with self.subTest(status=status, poll=poll, purpose=purpose), mock.patch.object(
                            server, '_atc_request', return_value={'code': 200, 'data': self.payload(status)}), \
                            mock.patch.object(server, '_atc_try_job_fallback', return_value=False), \
                            mock.patch.object(server, '_atc_store_job_result') as store:
                        (server._atc_poll_claimed if poll else server._atc_submit_claimed)(job, self.cfg)
                        self.assertEqual(self.row()['status'], 'failed')
                        store.assert_not_called()

    def test_explicit_busy_requeue_waits_before_next_submission(self):
        job = self.job()
        with mock.patch.object(server, '_atc_request', return_value={
                'code': 500, 'msg': '您的并发任务已达上限(5/5)，请等待任务完成后再试'}):
            server._atc_submit_claimed(job, self.cfg)
        self.assertEqual(self.row()['status'], 'pending')
        self.assertIsNone(server._atc_claim_pending('no-spin'))
        server.db_exec('UPDATE atc_jobs SET updated=?', (int(time.time()) - 6,))
        self.assertIsNotNone(server._atc_claim_pending('retry-after-delay'))

    def test_all_purposes_stop_expired_pending_jobs(self):
        for purpose in ('play', 'download', 'transcript'):
            server.db_exec('DELETE FROM atc_jobs')
            job = self.job(purpose)
            job['created'] = int(time.time()) - server.ATC_JOB_TIMEOUT - 1
            with mock.patch.object(server, '_atc_request') as api:
                server._atc_submit_claimed(job, self.cfg)
            api.assert_not_called()
            self.assertEqual(self.row()['status'], 'failed')

    def test_worker_permanent_rejection_stops_and_transient_query_keeps_task_id(self):
        for code, expected in ((401, 'failed'), (601, 'failed'), (429, 'submitted')):
            server.db_exec('DELETE FROM atc_jobs')
            job = self.job()
            server._atc_claim_update(job, 'submitted', task_id='existing-task')
            job = self.poll_job()
            with mock.patch.object(server, '_atc_request', return_value={'code': code, 'msg': 'rejected'}):
                server._atc_poll_claimed(job, self.cfg)
            self.assertEqual(self.row()['status'], expected)
            self.assertEqual(self.row()['task_id'], 'existing-task')

    def test_uncertain_post_failure_is_not_resubmitted(self):
        job = self.job()
        with mock.patch.object(server, '_atc_request', side_effect=server._ParserServiceError(
                502, '连接异常', retryable=True)) as api:
            server._atc_submit_claimed(job, self.cfg)
        self.assertEqual(self.row()['status'], 'failed')
        api.assert_called_once()

    def test_admin_waiting_does_not_claim_success_and_polling_is_throttled(self):
        with mock.patch.object(server, '_atc_request', return_value={
                'code': 200, 'data': self.payload('WAITING')}) as api:
            response = self.client.post('/api/admin/parser/test', json={'work_url': self.work_url})
            self.assertEqual(response.json()['state'], 'submitted')
            for _ in range(3):
                state = json.loads(self.client.get('/api/admin/parser').json()['test'])
                self.assertEqual(state['state'], 'submitted')
            self.assertEqual(api.call_count, 1)
        stored = json.loads(server.app_setting('atc_test_state'))
        stored['updated'] -= 6
        server.set_app_setting('atc_test_state', json.dumps(stored))
        with mock.patch.object(server, '_atc_request', return_value={'code': 200, 'data': self.payload()}):
            state = json.loads(self.client.get('/api/admin/parser').json()['test'])
        self.assertEqual(state['state'], 'success')
        self.assertEqual(state['duration'], 156.36)

    def test_admin_failure_media_is_rejected_at_both_stages(self):
        for status in ('FAILED', 'FAILURE'):
            with mock.patch.object(server, '_atc_request', return_value={
                    'code': 200, 'data': self.payload(status)}):
                response = self.client.post('/api/admin/parser/test', json={'work_url': self.work_url})
                self.assertEqual(response.status_code, 502)
                server.set_app_setting('atc_test_state', json.dumps({
                    'state': 'submitted', 'task_id': 't', 'created': int(time.time()), 'updated': 0}))
                state = json.loads(self.client.get('/api/admin/parser').json()['test'])
                self.assertEqual(state['state'], 'failed')

    def test_admin_query_timeout_does_not_send_another_request(self):
        server.set_app_setting('atc_test_state', json.dumps({
            'state': 'submitted', 'task_id': 't', 'updated': 0,
            'created': int(time.time()) - server.ATC_JOB_TIMEOUT - 1}))
        with mock.patch.object(server, '_atc_request') as api:
            state = json.loads(self.client.get('/api/admin/parser').json()['test'])
        self.assertEqual(state['state'], 'failed')
        api.assert_not_called()

    def test_admin_auth_diagnostic_is_actionable_without_raw_error(self):
        with mock.patch.object(server, '_atc_request', side_effect=server._ParserServiceError(
                503, '不可用', reason='auth')):
            response = self.client.post('/api/admin/parser/test', json={'work_url': self.work_url})
        self.assertEqual(response.status_code, 503)
        self.assertIn('鉴权失败', response.json()['error'])
        self.assertNotRegex(response.text, r'(?i)anytocopy|contract-secret|https?://')
