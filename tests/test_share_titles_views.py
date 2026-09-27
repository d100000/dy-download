"""分享浏览按页面打开计数，管理员标题覆盖只作用于当前分享。"""
import json
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit
from unittest import mock

from tests.test_security_reliability import server, TestClient, make_request


class ShareTitlesAndViewsTests(unittest.TestCase):
    item_id = '7683504400719073115'

    def setUp(self):
        self.sids = []
        self.admin = TestClient(server.app)
        self.token = server._new_session()
        self.admin.cookies.set('admin_session', self.token)
        self.public = TestClient(server.app)
        with server._rate_lock:
            server._share_event_hits.clear()

    def tearDown(self):
        for sid in self.sids:
            server.db_exec('DELETE FROM share_events WHERE sid=?', (sid,))
            server.db_exec('DELETE FROM shares WHERE id=?', (sid,))
        server._sessions.pop(self.token, None)
        self.admin.close()
        self.public.close()

    def share(self, kind='video', parse_status='ready', title='原来的作品标题'):
        sid = 'title-test-' + uuid.uuid4().hex
        now = int(time.time())
        data = {'kind': kind, 'item_id': self.item_id, 'title': title,
                'source': 'douyin_direct', 'platform': 'douyin',
                'video': {'source': 'douyin_direct', 'filename': 'old.mp4'},
                'images': [{'filename': 'old.jpeg'}] if kind == 'note' else []}
        server.db_exec(
            'INSERT INTO shares(id,item_id,kind,title,author,avatar,cover,vid,payload,'
            'custom_title,status,parse_status,expires_at,created) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (sid, self.item_id, kind, title, '作者', '', '', '',
             json.dumps(data, ensure_ascii=False), '', 'ok', parse_status, now + 3600, now))
        self.sids.append(sid)
        return sid

    def row(self, sid):
        return dict(server.db_exec('SELECT * FROM shares WHERE id=?', (sid,), 'one'))

    def patch_title(self, sid, title, client=None):
        return (client or self.admin).patch('/api/admin/shares/' + sid + '/title',
                                            json={'title': title})

    def page_view(self, sid, page_id=None):
        return self.public.post('/api/share/' + sid + '/event', json={
            'kind': 'page_view', 'page_view_id': page_id or str(uuid.uuid4())})

    def test_title_edit_requires_admin_validates_length_and_reports_missing_share(self):
        sid = self.share()
        self.assertEqual(self.patch_title(sid, '未授权', self.public).status_code, 401)
        self.assertEqual(self.patch_title(sid, '字' * 301).status_code, 422)
        self.assertEqual(self.patch_title('does-not-exist', '标题').status_code, 404)
        self.assertEqual(self.row(sid)['custom_title'], '')

    def test_title_edit_updates_metadata_filenames_and_search_without_rewriting_original(self):
        sid = self.share()
        other = self.share()
        before = self.row(sid)
        title = '后来补上的好标题'
        with mock.patch.object(server, 'open_url', side_effect=AssertionError('network requested')), \
                mock.patch.object(server, '_save_parse_snapshot', side_effect=AssertionError('snapshot rewritten')):
            response = self.patch_title(sid, title)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'ok': True, 'title': title,
                'custom_title': title, 'original_title': '原来的作品标题'})
            view = self.public.get('/api/share/' + sid).json()
            self.assertEqual(view['title'], title)
            self.assertEqual(view['data']['title'], title)
            self.assertEqual(view['data']['title_source'], 'custom')
            filename = title + '.mp4'
            self.assertEqual(view['data']['video']['filename'], filename)
            query = parse_qs(urlsplit(view['data']['video']['download_url']).query)
            self.assertEqual(query['name'], [filename])
            page = self.public.get('/s/' + sid).text
            self.assertIn('<meta property="og:title" content="' + title + '">', page)
            listed = self.admin.get('/api/admin/shares', params={'q': title}).json()['shares']
            self.assertEqual([x['id'] for x in listed], [sid])
            self.assertEqual(listed[0]['original_title'], before['title'])
            self.assertNotIn('payload', listed[0])
        after = self.row(sid)
        self.assertEqual(after['title'], before['title'])
        self.assertEqual(after['payload'], before['payload'])
        self.assertEqual(server._share_view(self.row(other))['title'], before['title'])

    def test_clear_title_restores_original_and_album_filenames_follow_custom_title(self):
        sid = self.share(kind='note')
        self.patch_title(sid, '新图集标题')
        view = server._share_view(self.row(sid))
        self.assertEqual(view['data']['images'][0]['filename'], '新图集标题_01.jpeg')
        response = self.patch_title(sid, '  ')
        self.assertEqual(response.json()['custom_title'], '')
        self.assertEqual(response.json()['title'], '原来的作品标题')
        self.assertEqual(server._share_view(self.row(sid))['data']['title'], '原来的作品标题')

    def test_special_characters_are_escaped_in_html_and_preserved_in_json(self):
        sid = self.share()
        title = '</script><script>alert("x")</script> & "标题"'
        self.assertEqual(self.patch_title(sid, title).json()['title'], title)
        response = self.public.get('/s/' + sid)
        self.assertNotIn(title, response.text)
        self.assertIn('&lt;/script&gt;&lt;script&gt;alert(&quot;x&quot;)', response.text)
        self.assertIn('\\u003c/script>', response.text)
        self.assertEqual(self.public.get('/api/share/' + sid).json()['title'], title)

    def test_pending_custom_title_survives_ready_transition_and_updates_card(self):
        sid = self.share(parse_status='pending')
        self.patch_title(sid, '提前补充的标题')
        self.assertIn('<meta property="og:title" content="提前补充的标题">',
                      self.public.get('/s/' + sid).text)
        server.db_exec("UPDATE shares SET parse_status='ready',title=? WHERE id=?",
                       ('后台新获取的标题', sid))
        self.assertEqual(self.public.get('/api/share/' + sid).json()['title'], '提前补充的标题')

    def test_saved_real_title_overrides_share_text_hint_and_removes_partial_marker(self):
        sid = self.share(title='可信的作品标题')
        row = self.row(sid)
        data = json.loads(row['payload'])
        data.update(title='分享文案的截断提示…', title_hint='分享文案的截断提示…',
                    title_source='share_text', title_status='partial')
        server.db_exec('UPDATE shares SET payload=? WHERE id=?',
                       (json.dumps(data, ensure_ascii=False), sid))
        view = server._share_view(self.row(sid))
        self.assertEqual(view['title'], '可信的作品标题')
        self.assertEqual(view['data']['title_status'], 'complete')
        self.assertNotEqual(view['data']['title_source'], 'share_text')
        self.patch_title(sid, '管理员补的标题')
        self.assertEqual(server._share_view(self.row(sid))['data']['title_source'], 'custom')
        self.patch_title(sid, '')
        self.assertEqual(server._share_view(self.row(sid))['title'], '可信的作品标题')

    def test_views_count_browser_opens_and_not_page_gets_cards_or_polling(self):
        sid = self.share(parse_status='pending')
        for path in ('/s/', '/api/share/', '/api/shares/'):
            self.assertEqual(self.public.get(path + sid).status_code, 200)
        self.assertEqual(self.row(sid)['page_views'], 0)
        self.assertEqual(self.row(sid)['views'], 0)
        page_id = str(uuid.uuid4())
        self.assertFalse(self.page_view(sid, page_id).json()['duplicate'])
        server.db_exec("UPDATE shares SET parse_status='ready' WHERE id=?", (sid,))
        self.public.get('/s/' + sid)
        self.assertTrue(self.page_view(sid, page_id).json()['duplicate'])
        self.assertEqual(self.row(sid)['page_views'], 1)
        self.assertFalse(self.page_view(sid).json()['duplicate'])
        self.assertEqual(self.row(sid)['page_views'], 2)
        self.assertEqual(self.row(sid)['views'], 1)
        listing = self.admin.get('/api/admin/shares', params={'q': sid}).json()
        self.assertEqual(listing['shares'][0]['page_views'], 2)
        self.assertGreaterEqual(listing['page_views'], 2)
        for event in server.db_exec('SELECT ip,fp,referer,event_key FROM share_events WHERE sid=?', (sid,), 'all'):
            self.assertEqual((event['ip'], event['fp'], event['referer']), ('', '', ''))
            self.assertNotIn(page_id, event['event_key'])

    def test_page_event_replays_across_minutes_and_concurrent_requests_only_count_once(self):
        sid = self.share()
        page_id = str(uuid.uuid4())
        request = make_request('/api/share/' + sid + '/event')
        now = int(time.time())
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: server._share_event(
                request, sid, 'page_view', page_view_id=page_id), range(5)))
        self.assertEqual(results.count('inserted'), 1)
        self.assertEqual(results.count('duplicate'), 4)
        with mock.patch.object(server.time, 'time', return_value=now + 120):
            self.assertEqual(server._share_event(request, sid, 'page_view', page_view_id=page_id), 'duplicate')
        self.assertEqual(self.row(sid)['page_views'], 1)
        self.assertEqual(self.row(sid)['views'], 1)

    def test_invalid_missing_and_expired_page_events_do_not_increment(self):
        sid = self.share()
        for page_id in ('', 'short', 'a' * 97, 'a' * 16 + '/bad'):
            response = self.public.post('/api/share/' + sid + '/event', json={
                'kind': 'page_view', 'page_view_id': page_id})
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.page_view('missing-share').status_code, 404)
        server.db_exec('UPDATE shares SET expires_at=? WHERE id=?', (int(time.time()) - 1, sid))
        self.assertEqual(self.page_view(sid).status_code, 404)
        self.assertEqual(self.row(sid)['page_views'], 0)


if __name__ == '__main__':
    unittest.main()
