"""下载命名应覆盖新结果、历史快照、缓存与同源响应头。"""
import copy
import io
import json
import time
import unittest
from urllib.parse import parse_qs, urlsplit, unquote
from unittest import mock

from tests.test_security_reliability import server, TestClient, make_request


class DownloadFilenameTests(unittest.TestCase):
    item = '7683504400719073115'

    def video(self, title='替你自由'):
        return {'item_id': self.item, 'kind': 'video', 'platform': 'douyin',
                'source': 'parser', 'title': title,
                'video': {'filename': '（无标题）.mp4', 'url': 'https://v3.douyinvod.com/file.mp4?name=upstream&sig=unchanged',
                          'download_url': server._atc_video_download_url(self.item, 'old.mp4')}}

    def test_short_title_sanitizes_controls_and_keeps_full_metadata(self):
        title = '这是一段非常长的视频标题用于检查文件名不会过长 #旅行 # 风景'
        data = self.video(title)
        before = copy.deepcopy(data)
        result = server._download_filenames(data)
        self.assertEqual(result['video']['filename'], title[:12] + '.mp4')
        self.assertEqual(result['title'], title)
        self.assertEqual(data, before)
        self.assertEqual(server._short_download_title('落日 / 海边 #旅行'), '落日_海边')
        for title in ('../CON', 'CON', 'LPT1', 'A\x00B\r\nC', '记录\u202e生活', '<>?*|'):
            filename = server._download_filenames(self.video(title))['video']['filename']
            self.assertNotRegex(filename, r'[\\/:*?"<>|\x00-\x1f\u202e]')
            self.assertNotIn(filename.lower(), ('con.mp4', 'lpt1.mp4'))
            self.assertFalse(filename.startswith('.'))
        self.assertEqual(server._short_download_title('e\u0301' * 20), 'é' * 12)
        self.assertEqual(server._short_download_title('😀' * 20), '😀' * 12)

    def test_missing_titles_use_readable_stable_platform_names(self):
        for title in ('', None, '（无标题）', '暂无标题', '暂无标题.mp4', 'Untitled', ' #街拍 #旅行', '...'):
            with self.subTest(title=title):
                self.assertEqual(server._download_filenames(self.video(title))['video']['filename'],
                                 '抖音视频_073115.mp4')
        self.assertEqual(server._download_filenames(dict(self.video(''), platform='tiktok'))['video']['filename'],
                         'TikTok视频_073115.mp4')
        self.assertEqual(server._download_filenames(dict(self.video(''), platform='other'))['video']['filename'],
                         '视频_073115.mp4')
        data = dict(self.video('暂无标题'), content='真实文案 #话题')
        self.assertEqual(server._download_filenames(data)['video']['filename'], '真实文案.mp4')

    def test_renaming_preserves_cdn_urls_and_media_signatures(self):
        data = self.video()
        result = server._download_filenames(data)
        self.assertEqual(result['video']['url'], data['video']['url'])
        query = parse_qs(urlsplit(result['video']['download_url']).query)
        self.assertEqual(query['name'], ['替你自由.mp4'])
        self.assertEqual(query['dl'], ['1'])
        server._require_media_token('atc_video', self.item, int(query['exp'][0]), query['sig'][0])
        data['video']['download_url'] = data['video']['url']
        self.assertEqual(server._download_filenames(data)['video']['download_url'], data['video']['url'])

    def test_albums_keep_numbering_and_image_format(self):
        data = {'item_id': self.item, 'kind': 'note', 'platform': 'douyin', 'title': '日落',
                'images': [{'filename': '旧名字.png'}, {'filename': '旧名字.jpeg',
                           'download_url': server._douyin_image_proxy_url(self.item, 2, 'old.jpeg', download=True)}]}
        result = server._download_filenames(data)
        self.assertEqual([x['filename'] for x in result['images']], ['日落_01.png', '日落_02.jpeg'])
        query = parse_qs(urlsplit(result['images'][1]['download_url']).query)
        self.assertEqual(query['name'], ['日落_02.jpeg'])
        server._require_media_token('douyin_image', self.item + ':2', int(query['exp'][0]), query['sig'][0])
        data['title'] = ''
        self.assertEqual(server._download_filenames(data)['images'][0]['filename'], '抖音图片_073115_01.png')

    def test_old_share_reads_new_names_without_parsing_or_rewriting_snapshot(self):
        data = self.video('这是旧分享保存下来的完整视频标题')
        with mock.patch.object(server, 'current_user', return_value=None):
            share = server._share_create(make_request(), data, '不应用于下载的自定义标题')
        sid = share['sid']
        self.addCleanup(server.db_exec, 'DELETE FROM shares WHERE id=?', (sid,))
        # 模拟升级前保存的长名称，回读不能原样采用旧文件名。
        old = copy.deepcopy(data)
        old['video']['filename'] = '旧文件名' * 20 + '.mp4'
        server.db_exec('UPDATE shares SET payload=? WHERE id=?', (json.dumps(old), sid))
        row = dict(server.db_exec('SELECT * FROM shares WHERE id=?', (sid,), 'one'))
        with mock.patch.object(server, '_atc_extract', side_effect=AssertionError('no reparse')):
            view = server._share_view(row)
        self.assertEqual(view['data']['video']['filename'], data['title'][:12] + '.mp4')
        self.assertEqual(view['title'], '不应用于下载的自定义标题')
        self.assertEqual(server.db_exec('SELECT payload FROM shares WHERE id=?', (sid,), 'one')['payload'], row['payload'])

    def test_cache_hit_returns_new_names_without_modifying_cached_value(self):
        data = self.video('缓存作品标题')
        with mock.patch.object(server, '_cache_get', return_value=(time.time(), data)), \
                mock.patch.object(server, '_atc_url_fresh', return_value=True), \
                mock.patch.object(server, '_retry_cached_metadata', side_effect=lambda key, d: d), \
                mock.patch.object(server, '_parse_share', side_effect=AssertionError('no reparse')):
            result = server._parse_cached('existing-link')
        self.assertEqual(result['video']['filename'], '缓存作品标题.mp4')
        self.assertEqual(data['video']['filename'], '（无标题）.mp4')

    def test_download_response_header_uses_the_generated_name(self):
        result = server._download_filenames(self.video())
        media = io.BytesIO(b'video-bytes')
        media.headers = {'Content-Type': 'video/mp4', 'Content-Length': '11'}
        media.status = 200
        media.geturl = lambda: 'https://v3.douyinvod.com/file.mp4'
        with mock.patch.object(server, '_open_primary_video_upstream', return_value=media), \
                TestClient(server.app) as client:
            response = client.get(result['video']['download_url'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'video-bytes')
        self.assertIn('替你自由.mp4', unquote(response.headers['content-disposition']))
