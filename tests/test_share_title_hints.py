"""分享文案提示的持久化、请求隔离与批量归属。"""
import copy
import json
import unittest
from unittest import mock

from tests.test_security_reliability import server, make_request


class ShareTitleHintTests(unittest.TestCase):
    link = 'https://v.douyin.com/HintVideo01/'
    item_id = '7689724147420630306'

    def setUp(self):
        for table in ('shares', 'share_submissions', 'blocked_share_items',
                      'blocked_share_sources', 'quota_reservations', 'usage_daily',
                      'parse_snapshots', 'atc_cache'):
            server.db_exec('DELETE FROM ' + table)
        server._share_hits.clear()
        self.cache = mock.patch.dict(server._cache, {}, clear=True)
        self.cache.start()
        self.addCleanup(self.cache.stop)
        self.parsed = {
            'item_id': self.item_id, 'kind': 'video', 'platform': 'douyin',
            'title': '（无标题）', 'author': '', 'cover': '',
            'video': {'url': 'https://www.iesdouyin.com/aweme/v1/play/?video_id=hint_video'},
        }

    def create(self, text, key='hint-request'):
        request = make_request('/api/shares', headers={'Idempotency-Key': key})
        with mock.patch.object(server, '_wake_share_parse_workers'):
            response = server.api_share_create_async(server.AsyncShareBody(text=text), request)
        return json.loads(response.body)['data']

    def row(self, sid):
        return dict(server.db_exec('SELECT * FROM shares WHERE id=?', (sid,), 'one'))

    def finish(self, data=None):
        item = server._claim_share_parse('hint-test-worker')
        self.assertTrue(server._finish_share_parse_success(item, data or self.parsed))

    def test_async_hint_survives_worker_without_becoming_global_metadata(self):
        text = '《无敌超人》希望生活多一些浪漫... ' + self.link
        created = self.create(text)
        pending = json.loads(self.row(created['sid'])['payload'])
        self.assertEqual(pending['title_hint'], '《无敌超人》希望生活多一些浪漫...')
        self.finish()
        row = self.row(created['sid'])
        saved = json.loads(row['payload'])
        self.assertEqual(saved['title_source'], 'share_text')
        self.assertEqual(saved['title_status'], 'partial')
        self.assertEqual(saved['title'], pending['title_hint'])
        self.assertEqual(row['title'], '（无标题）')
        self.assertEqual(server._share_view(row)['title'], pending['title_hint'])
        self.assertEqual(server._share_async_payload(row, '')['title'], pending['title_hint'])
        server._save_parse_snapshot(saved, self.link)
        snapshot = server._get_parse_snapshot(self.item_id)
        self.assertNotIn('title_hint', snapshot)
        self.assertFalse(server._valid_title(snapshot.get('title')))

    def test_provider_title_wins_and_other_share_gets_no_hint(self):
        first = self.create('本次请求的标题提示 ' + self.link)
        self.finish(dict(self.parsed, title='接口完整原标题'))
        self.assertEqual(server._share_view(self.row(first['sid']))['title'], '接口完整原标题')
        second = self.create(self.link, key='second-request')
        self.finish()
        self.assertNotIn('title_hint', json.loads(self.row(second['sid'])['payload']))
        self.assertNotEqual(server._share_view(self.row(second['sid']))['title'], '本次请求的标题提示')

    def test_parse_hint_stays_out_of_item_cache_and_persisted_snapshot(self):
        with mock.patch.object(server, '_parse_share', return_value=self.parsed):
            response = server._parse_cached('此请求的分享片段... ' + self.link)
            self.assertEqual(response['title'], '此请求的分享片段...')
            self.assertNotIn('title_hint', server._cache_get(self.item_id)[1])
            self.assertNotIn('title_hint', server._get_parse_snapshot(self.item_id))
            other = server._parse_cached(self.link)
            self.assertNotIn('title_hint', other)
            self.assertNotEqual(other['title'], response['title'])

    def test_idempotency_does_not_replay_a_different_title_hint(self):
        self.create('第一条提示 ' + self.link)
        with self.assertRaises(server.ApiError) as error:
            self.create('第二条提示 ' + self.link)
        self.assertEqual(error.exception.status, 409)

    def test_hint_request_hash_has_unambiguous_field_boundaries(self):
        self.assertNotEqual(server._share_request_hash(self.link, '甲\n乙', ''),
                            server._share_request_hash(self.link, '甲', '乙'))
        self.assertNotEqual(server._share_request_hash(self.link, '甲\n乙', '丙'),
                            server._share_request_hash(self.link, '甲', '乙\n丙'))
        self.assertEqual(server._share_request_hash(self.link, '旧标题'),
                         server._privacy_hash('async-share-request', self.link + '\n旧标题'))

    def test_sync_share_does_not_inherit_someone_elses_hint(self):
        first = self.create('仅属于第一个分享的提示 ' + self.link)
        self.finish()
        before = copy.deepcopy(self.row(first['sid']))
        result = server.api_share_create(
            server.ShareBody(item_id=self.item_id), make_request('/api/share'))
        self.assertNotEqual(result['title'], '仅属于第一个分享的提示')
        self.assertEqual(self.row(first['sid'])['payload'], before['payload'])
        self.assertNotIn('title_hint', result['data'])

    def test_sync_long_hint_is_not_truncated_or_promoted_to_original(self):
        hint = '分享文案' * 90 + '...'
        with mock.patch.object(server, '_cache_get', return_value=(server.time.time(), self.parsed)):
            result = server.api_share_create(
                server.ShareBody(item_id=self.item_id, text=hint + ' ' + self.link),
                make_request('/api/share'))
        self.assertEqual(result['title'], hint)
        self.assertEqual(result['data']['title_source'], 'share_text')
        self.assertEqual(result['data']['title_status'], 'partial')
        self.assertEqual(self.row(result['sid'])['title'], '（无标题）')

    def test_verified_title_backfill_upgrades_hint_and_preserves_custom_title(self):
        created = self.create('用户分享片段... ' + self.link)
        self.finish()
        server.db_exec('UPDATE shares SET custom_title=? WHERE id=?',
                       ('管理员编辑标题', created['sid']))
        server._backfill_share_metadata(dict(self.parsed, title='完整官方原标题'))
        row = self.row(created['sid'])
        self.assertEqual(json.loads(row['payload'])['title'], '完整官方原标题')
        self.assertEqual(server._share_view(row)['title'], '管理员编辑标题')

    def test_batch_hints_are_associated_only_with_unambiguous_lines(self):
        other = 'https://v.douyin.com/HintVideo02/'
        text = '第一条标题 ' + self.link + '\n第二条标题 ' + other
        inputs = server._batch_parse_inputs(text, [self.link, other])
        self.assertEqual(server._extract_title_hint(inputs[self.link]), '第一条标题')
        self.assertEqual(server._extract_title_hint(inputs[other]), '第二条标题')
        ambiguous = '无法判断归属 ' + self.link + ' ' + other
        self.assertEqual(server._batch_parse_inputs(ambiguous, [self.link, other]),
                         {self.link: self.link, other: other})


if __name__ == '__main__':
    unittest.main()
