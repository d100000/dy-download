"""标题来源、同作品 HTML 和有界补全回归；不访问真实平台。"""
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote

from tests.test_security_reliability import server


class TitleFallbackTests(unittest.TestCase):
    item = '7689724147420630306'
    other = '7689724147420630307'
    link = 'https://www.douyin.com/video/' + item + '/'
    short = 'https://v.douyin.com/TitleHint01/'

    def setUp(self):
        for cache in (server._author_cache, server._douyin_result_cache,
                      server._douyin_media_cache, server._douyin_note_media_cache, server._metadata_retries):
            patcher = mock.patch.dict(cache, {}, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.primary = {
            'item_id': self.item, 'kind': 'video', 'platform': 'douyin', 'source': 'parser',
            'title': '（无标题）', 'video': {'url': 'https://v3.douyinvod.com/original.mp4'},
        }

    def test_provider_nonempty_aliases_and_existing_title_precedence(self):
        with mock.patch.object(server, '_atc_save_result'):
            for alias in ('content', 'video_description', 'description', 'desc', 'caption'):
                with self.subTest(alias=alias):
                    payload = {'title': '  ', alias: '有效作品标题',
                               'videoUrl': self.primary['video']['url'], 'workId': self.item}
                    data = server._atc_result_to_parse(self.link, payload)
                    self.assertEqual(data['title'], '有效作品标题')
                    self.assertEqual(data['title_source'], 'provider')
                    payload['title'] = '原有标题'
                    self.assertEqual(server._atc_result_to_parse(self.link, payload)['title'], '原有标题')

    def test_native_empty_description_does_not_hide_valid_title(self):
        payload = {'aweme_id': self.item, 'desc': ' ', 'title': '有效原文',
                   'description': '完整作品介绍', 'video': {}}
        result = server._douyin_native_result(payload, self.link, self.item)
        self.assertEqual(result['title'], '有效原文')
        self.assertEqual(result['content'], '完整作品介绍')

    def test_render_data_finds_expected_work_after_recommendation(self):
        payload = {'items': [{'aweme_id': self.other, 'desc': '推荐作品'},
                             {'aweme_id': self.item, 'desc': '当前作品'}]}
        html = '<script id="RENDER_DATA" type="application/json">' + quote(json.dumps(payload)) + '</script>'
        extracted = server._douyin_html_payloads(html, self.item)
        self.assertEqual(server._douyin_native_result(extracted[0], self.link, self.item)['title'], '当前作品')

    def test_meta_requires_consistent_work_identity_and_filters_shell_titles(self):
        html = '<link rel="canonical" href="%s"><meta property="og:title" content="作品 &amp; 标题 - 抖音">'
        payloads = server._douyin_html_payloads(html % self.link, self.item)
        self.assertEqual(server._douyin_native_result(payloads[0], self.link, self.item)['title'], '作品 & 标题')
        self.assertEqual(server._douyin_html_payloads(html % self.link.replace(self.item, self.other), self.item), [])
        self.assertEqual(server._douyin_html_payloads('<meta property="og:title" content="无法确认归属">', self.item), [])
        shell = html.replace('作品 &amp; 标题', '在抖音记录美好生活20260927') % self.link
        self.assertEqual(server._douyin_html_payloads(shell, self.item), [])
        conflicting = (html % self.link) + '<meta property="og:url" content="' + self.link.replace(self.item, self.other) + '">'
        self.assertEqual(server._douyin_html_payloads(conflicting, self.item), [])

    def test_json_ld_must_identify_same_work(self):
        payload = {'@type': 'VideoObject', 'name': '结构化标题', 'url': self.link}
        self.assertEqual(server._douyin_native_result(payload, self.link, self.item)['title'], '结构化标题')
        payload['url'] = self.link.replace(self.item, self.other)
        self.assertIsNone(server._douyin_native_result(payload, self.link, self.item))
        payload.pop('url')
        self.assertIsNone(server._douyin_native_result(payload, self.link, self.item))

    def test_later_meta_fills_missing_ssr_title_without_replacing_media(self):
        for with_media in (True, False):
            with self.subTest(with_media=with_media):
                server._douyin_result_cache.clear()
                video = {'play_addr': {'url_list': [self.primary['video']['url']]}} if with_media else {}
                payloads = [{'aweme_id': self.item, 'desc': '', 'video': video},
                            {'aweme_id': self.item, 'desc': '补充的页面标题', 'video': {},
                             '_title_source': 'official_meta'}]
                with mock.patch.object(server, '_douyin_fetch_html', return_value=('', payloads)):
                    result = server._parse_douyin_item_direct('video', self.item, self.link, allow_metadata=True)
                self.assertEqual(result['title'], '补充的页面标题')
                self.assertEqual(result['title_source'], 'official_meta')
                if with_media:
                    self.assertEqual(result['video']['url'], self.primary['video']['url'])
                else:
                    self.assertTrue(result['metadata_only'])

    def test_later_meta_cannot_overwrite_valid_structured_title(self):
        payloads = [{'aweme_id': self.item, 'desc': '已有结构化标题', 'video': {}},
                    {'aweme_id': self.item, 'desc': '页面摘要', 'video': {}, '_title_source': 'official_meta'}]
        with mock.patch.object(server, '_douyin_fetch_html', return_value=('', payloads)):
            result = server._parse_douyin_item_direct('video', self.item, self.link, allow_metadata=True)
        self.assertEqual(result['title'], '已有结构化标题')

    def test_later_video_candidate_does_not_replace_selected_media_cache(self):
        first, second = 'https://v3.douyinvod.com/first.mp4', 'https://v3.douyinvod.com/second.mp4'
        payloads = [{'aweme_id': self.item, 'desc': '', 'video': {'play_addr': {'url_list': [first]}}},
                    {'aweme_id': self.item, 'desc': '后续可信标题', 'video': {'play_addr': {'url_list': [second]}}}]
        with mock.patch.object(server, '_douyin_fetch_html', return_value=('', payloads)):
            result = server._parse_douyin_item_direct('video', self.item, self.link)
        self.assertEqual(result['video']['url'], first)
        self.assertEqual(server._douyin_cached_media(self.item)['urls'], [first])
        self.assertEqual(result['title'], '后续可信标题')

    def test_later_note_candidate_does_not_replace_selected_image_cache(self):
        first = ['https://p3.douyinpic.com/first.jpg', 'https://p3.douyinpic.com/second.jpg']
        second = ['https://p3.douyinpic.com/unselected.jpg']
        payloads = [{'aweme_id': self.item, 'desc': '', 'images': first},
                    {'aweme_id': self.item, 'desc': '图集标题', 'images': second}]
        with mock.patch.object(server, '_douyin_fetch_html', return_value=('', payloads)):
            result = server._parse_douyin_item_direct('note', self.item, self.link)
        self.assertEqual([image['url'] for image in result['images']], first)
        self.assertEqual(server._douyin_cached_note_media(self.item)['urls'], first)
        self.assertEqual(result['title'], '图集标题')

    def test_share_text_is_partial_and_remains_request_local(self):
        text = '3.45 复制打开抖音，看看【作者的作品】《标题》有些浪漫... ' + self.short + ' 09/27 ABC:/ @user'
        hint = server._extract_title_hint(text)
        self.assertEqual(hint, '《标题》有些浪漫...')
        result = server._apply_title_hint(self.primary, hint)
        self.assertEqual((result['title_source'], result['title_status']), ('share_text', 'partial'))
        self.assertEqual(self.primary['title'], '（无标题）')
        stripped = server._without_title_hint(result)
        self.assertNotIn('title_hint', stripped)
        self.assertNotIn('浪漫', stripped['video']['filename'])
        verified = server._merge_metadata_snapshot(result, dict(self.primary, title='完整可信标题', title_source='provider'))
        self.assertEqual(verified['title'], '完整可信标题')
        self.assertEqual(verified['title_source'], 'provider')
        self.assertEqual(server._merge_metadata_snapshot(result, self.primary)['title'], hint)
        self.assertEqual(server._extract_title_hint(self.short), '')
        self.assertEqual(server._extract_title_hint('3.87 复制 ' + self.short + ' 打开抖音'), '')
        self.assertEqual(server._extract_title_hint('English title ' + self.short), 'English title')
        self.assertEqual(server._extract_title_hint('片段 ' + self.short + ' ' + self.link), '')

    def test_hint_cannot_override_trusted_description(self):
        data = dict(self.primary, content='接口完整描述')
        result = server._apply_title_hint(data, '用户片段...')
        self.assertEqual(result['title'], '接口完整描述')
        self.assertNotEqual(result.get('title_source'), 'share_text')

    def test_metadata_timeout_returns_primary_and_late_worker_cannot_change_it(self):
        release, exited = threading.Event(), threading.Event()
        slots = threading.BoundedSemaphore(1)

        def delayed(*args, **kwargs):
            try:
                release.wait(1)
                return dict(self.primary, title='迟到的元数据')
            finally:
                exited.set()

        with mock.patch.object(server, '_metadata_slots', slots), \
                mock.patch.object(server, 'DOUYIN_METADATA_BUDGET_SECONDS', .03), \
                mock.patch.object(server, '_douyin_resolve_share_url', return_value=('video', self.item, self.link)), \
                mock.patch.object(server, '_parse_douyin_item_direct', side_effect=delayed):
            try:
                start = time.monotonic()
                result = server._complete_douyin_result(self.short, self.primary)
                self.assertLess(time.monotonic() - start, .2)
                self.assertEqual(result['title'], '（无标题）')
                self.assertEqual(result['video'], self.primary['video'])
            finally:
                release.set()
                self.assertTrue(exited.wait(1))
                self.assertTrue(slots.acquire(timeout=1))
                slots.release()
        self.assertEqual(self.primary['title'], '（无标题）')

    def test_known_mismatch_never_falls_back_to_wrong_primary_after_timeout(self):
        release, exited = threading.Event(), threading.Event()
        slots = threading.BoundedSemaphore(1)

        def delayed(*args, **kwargs):
            try:
                release.wait(1)
                raise TimeoutError()
            finally:
                exited.set()

        with mock.patch.object(server, '_metadata_slots', slots), \
                mock.patch.object(server, 'DOUYIN_METADATA_BUDGET_SECONDS', .03), \
                mock.patch.object(server, '_douyin_resolve_share_url', return_value=('video', self.other, self.link.replace(self.item, self.other))), \
                mock.patch.object(server, '_parse_douyin_item_direct', side_effect=delayed):
            try:
                with self.assertRaises(server.ApiError) as raised:
                    server._complete_douyin_result(self.short, self.primary)
                self.assertEqual(raised.exception.status, 503)
            finally:
                release.set()
                self.assertTrue(exited.wait(1))
                self.assertTrue(slots.acquire(timeout=1))
                slots.release()

    def test_proxy_retries_share_one_deadline(self):
        clock = [10.0]
        proxies = [{'url': 'http://proxy1.test'}, {'url': 'http://proxy2.test'}]

        def slow_attempt(url, follow, headers, timeout, proxy):
            self.assertEqual(timeout, 2.0)
            clock[0] += 2.1
            raise TimeoutError()

        token = server._metadata_deadline.set(12.0)
        try:
            with mock.patch.object(server.time, 'monotonic', side_effect=lambda: clock[0]), \
                    mock.patch.object(server.proxy_mgr, 'candidates', return_value=proxies), \
                    mock.patch.object(server.proxy_mgr, 'mark_fail'), \
                    mock.patch.object(server, '_raw_open', side_effect=slow_attempt) as raw:
                with self.assertRaises(TimeoutError):
                    server.open_url(self.link, timeout=20)
                self.assertEqual(raw.call_count, 1)
        finally:
            server._metadata_deadline.reset(token)

    def test_slow_body_reads_cannot_restart_metadata_budget(self):
        clock = [0.0]
        sock = mock.Mock()

        def read(size):
            clock[0] += .3
            return b'x'

        response = SimpleNamespace(read1=read, read=read, fp=SimpleNamespace(raw=SimpleNamespace(_sock=sock)))
        token = server._metadata_deadline.set(1.0)
        try:
            with mock.patch.object(server.time, 'monotonic', side_effect=lambda: clock[0]):
                with self.assertRaises(TimeoutError):
                    server._metadata_read_body(response, 100)
            timeouts = [call.args[0] for call in sock.settimeout.call_args_list]
            self.assertEqual(len(timeouts), 4)
            self.assertAlmostEqual(timeouts[-1], .1)
        finally:
            server._metadata_deadline.reset(token)


if __name__ == '__main__':
    unittest.main()
