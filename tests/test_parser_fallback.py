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
        job = {'item_id': self.item_id, 'work_url': self.work_url, 'purpose': 'play'}
        with mock.patch.object(server, '_atc_request', return_value={
                'code': 200, 'data': {**self.primary, 'status': 'SUCCESS'}}) as request, \
                mock.patch.object(server, '_atc_claim_update') as update:
            server._atc_submit_claimed(job, {})
        request.assert_called_once()
        self.direct.assert_called_once()
        update.assert_called_once_with(job, 'done', error=None)
        self.assertIsNotNone(server._douyin_cached_media(self.item_id))

    def test_background_rejection_uses_official_media_without_leaking_upstream(self):
        job = {'item_id': self.item_id, 'work_url': self.work_url, 'purpose': 'play'}
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


if __name__ == '__main__':
    unittest.main()
