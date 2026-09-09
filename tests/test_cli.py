"""独立标准库 CLI 的视频、图集和失败路径；所有 HTTP 均用本地夹具。"""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import douyin_dl as cli


class CLITests(unittest.TestCase):
    def test_share_text_and_unsupported_input(self):
        self.assertEqual(cli.extract_short_link('替你自由 https://v.douyin.com/demo/ 复制'),
                         'https://v.douyin.com/demo/')
        with self.assertRaises(SystemExit):
            cli.extract_short_link('https://example.test/video')

    def test_resolve_video_note_and_invalid_redirect(self):
        for kind in ('video', 'note'):
            with mock.patch.object(cli, 'http_get', return_value=(302, f'https://www.iesdouyin.com/share/{kind}/12345678/', b'')):
                self.assertEqual(cli.resolve_item('short'), (kind, '12345678'))
        with mock.patch.object(cli, 'http_get', return_value=(200, '', b'')), self.assertRaises(SystemExit):
            cli.resolve_item('short')

    def test_router_data_and_changed_page(self):
        body = b'<script>window._ROUTER_DATA = {"loader":{"item_list":[{"desc":"test"}]}}</script>'
        with mock.patch.object(cli, 'http_get', return_value=(200, 'url', body)):
            data = cli.fetch_router_data('video', '12345678')
            self.assertEqual(next(cli.find_key(data, 'item_list'))[0]['desc'], 'test')
        with mock.patch.object(cli, 'http_get', return_value=(200, 'url', b'<html/>')), self.assertRaises(SystemExit):
            cli.fetch_router_data('video', '12345678')

    def test_filename_removes_path_characters_and_has_fallback(self):
        self.assertEqual(cli.safe_filename('标题 / 视频 #话题', '123'), '标题_视频')
        self.assertEqual(cli.safe_filename(' #话题 ', '123'), '123')
        self.assertEqual(len(cli.safe_filename('长' * 100, '123')), 60)

    def test_rejected_download_does_not_create_file(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(cli, 'http_get', return_value=(200, 'url', b'error')):
            dest = Path(folder) / 'video.mp4'
            with self.assertRaises(SystemExit):
                cli.download('url', dest)
            self.assertFalse(dest.exists())

    def test_complete_video_and_note_download_paths(self):
        for kind in ('video', 'note'):
            media = b'\x00\x00\x00\x18ftypmp42' + b'0' * 12000
            item = {'desc': '测试', 'nickname': '作者',
                    'play_addr': {'url_list': ['https://example.test/playwm/demo']},
                    'images': [{'url_list': ['https://example.test/1.jpg']},
                               {'url_list': ['https://example.test/2.jpg']}]}
            def fetch(url, follow=True):
                if not follow:
                    return 302, f'https://www.iesdouyin.com/share/{kind}/12345678/', b''
                if '/share/' in url:
                    html = '<script>window._ROUTER_DATA=' + json.dumps({'item_list': [item]}) + '</script>'
                    return 200, url, html.encode()
                self.assertNotIn('/playwm/', url)
                return 200, url, media
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder, \
                    mock.patch.object(cli, 'http_get', side_effect=fetch), \
                    mock.patch.object(cli.sys, 'argv', ['douyin_dl.py', 'https://v.douyin.com/demo/', folder]), \
                    mock.patch('sys.stdout', new_callable=io.StringIO):
                cli.main()
                files = sorted(Path(folder).iterdir())
                self.assertEqual(len(files), 1 if kind == 'video' else 2)
                self.assertTrue(all(path.read_bytes() == media for path in files))

    def test_cli_requires_arguments(self):
        with mock.patch.object(cli.sys, 'argv', ['douyin_dl.py']), self.assertRaises(SystemExit):
            cli.main()
