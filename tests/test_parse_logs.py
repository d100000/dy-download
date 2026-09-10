"""管理员解析时间线：真实链路决策、鉴权、并发隔离、脱敏与保留期。"""
import copy
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from tests.test_security_reliability import server, TestClient, make_request


class ParseLogTests(unittest.TestCase):
    url = 'https://v.douyin.com/qFnZK0HPwBo/'

    def setUp(self):
        server.db_exec('DELETE FROM parse_logs')
        patch = mock.patch.dict(server._cache, {}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.result = {'item_id': '12345678912345678', 'kind': 'video', 'source': 'parser',
                       'platform': 'douyin', 'title': '真实标题', 'author': '作者',
                       'stats': {'digg': 0, 'comment': 0, 'share': 0, 'collect': 0},
                       'video': {'url': 'https://v3.douyinvod.com/video?secret=sensitive'},
                       'duration_ms': 1000}

    def logs(self):
        return [server._parse_log_public(r, True) for r in server.db_exec('SELECT * FROM parse_logs ORDER BY rowid', (), 'all')]

    def codes(self, row):
        return [e['code'] for e in row['events']]

    def test_zero_counts_are_success_and_input_and_media_secrets_are_never_stored(self):
        with mock.patch.object(server, '_parse_share', return_value=self.result), \
                mock.patch.object(server, '_save_parse_snapshot'), mock.patch.object(server, '_backfill_share_metadata'):
            result = server._logged_parse('private text ' + self.url + '?token=secret', 'web', 7)
        row = self.logs()[0]
        self.assertEqual(row['status'], 'success')
        self.assertEqual(row['missing'], [])
        self.assertEqual(row['user_id'], 7)
        self.assertEqual(result['stats']['digg'], 0)
        self.assertIn('snapshot', self.codes(row))
        raw = str(dict(server.db_exec('SELECT * FROM parse_logs', (), 'one')))
        for token in ('private text', 'qFnZK0HPwBo', 'token=secret', 'sensitive', 'douyinvod'):
            self.assertNotIn(token, raw)

    def test_primary_auth_failure_and_official_fallback_are_separate_events(self):
        with mock.patch.object(server, '_atc_extract', side_effect=server._ParserServiceError(503, 'secret brand', reason='auth')), \
                mock.patch.object(server, '_parse_douyin_share_direct', return_value=self.result):
            server._atc_parse_work_url(self.url)
        row = self.logs()[0]
        self.assertEqual(row['status'], 'success')
        codes = self.codes(row)
        self.assertLess(codes.index('auth'), codes.index('fallback'))
        self.assertIn('fallback_ok', codes)
        self.assertNotIn('secret brand', str(row))

    def test_supplement_failure_preserves_video_and_marks_partial(self):
        partial = copy.deepcopy(self.result)
        partial.update(title='', author='', stats={})
        with mock.patch.object(server, '_atc_extract', return_value={}), \
                mock.patch.object(server, '_atc_result_to_parse', return_value=partial), \
                mock.patch.object(server, '_douyin_resolve_share_url', side_effect=TimeoutError('private url secret')):
            result = server._atc_parse_work_url(self.url)
        row = self.logs()[0]
        self.assertEqual(result['video'], partial['video'])
        self.assertEqual(row['status'], 'partial')
        self.assertIn('digg', row['missing'])
        failure = next(e for e in row['events'] if e['code'] == 'supplement_failed')
        self.assertEqual(failure['reason'], 'timeout')
        self.assertNotIn('supplement_ok', self.codes(row))
        self.assertNotIn('private url secret', str(row))

    def test_exception_and_nested_context_reset(self):
        with mock.patch.object(server, '_parse_share', side_effect=RuntimeError('anytocopy API_SECRET=secret')):
            with self.assertRaises(RuntimeError):
                server._logged_parse(self.url, 'api', reference='job123:0')
        self.assertIsNone(server._parse_trace.get())
        self.assertEqual(len(self.logs()), 1)
        self.assertEqual(self.logs()[0]['code'], 'internal')
        self.assertEqual(self.logs()[0]['reference'], 'job123:0')
        self.assertNotIn('anytocopy', str(self.logs()))

    def test_cache_hit_does_not_call_primary_and_is_logged(self):
        server._cache_put(self.url, self.result)
        with mock.patch.object(server, '_atc_url_fresh', return_value=True), \
                mock.patch.object(server, '_parse_share', side_effect=AssertionError('paid upstream')):
            server._logged_parse(self.url, 'batch')
        self.assertIn('cache_hit', self.codes(self.logs()[0]))
        self.assertNotIn('primary_start', self.codes(self.logs()[0]))

    def test_parallel_requests_do_not_mix_events(self):
        barrier = threading.Barrier(2)
        def work(number):
            with server._parse_log_scope('api', self.url, number) as trace:
                barrier.wait(2)
                server._parse_event('network', 'proxy', attempt=number)
                trace['result'] = dict(self.result, item_id=str(number) * 18)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(work, (1, 2)))
        rows = self.logs()
        self.assertEqual(len(rows), 2)
        for row in rows:
            event = next(x for x in row['events'] if x['code'] == 'proxy')
            self.assertEqual(event['attempt'], row['user_id'])
            self.assertEqual(row['item_id'], str(row['user_id']) * 18)

    def test_quota_rejection_does_not_parse_or_claim_success(self):
        with mock.patch.object(server, 'reserve_quota', return_value={'ok': False, 'limit': 3}), \
                mock.patch.object(server, '_parse_cached') as parse:
            with self.assertRaises(server.ApiError):
                server.api_parse(server.ParseBody(text=self.url), make_request('/api/parse'))
        parse.assert_not_called()
        self.assertEqual(self.logs()[0]['code'], 'quota_denied')
        self.assertEqual(self.logs()[0]['status'], 'failed')

    def test_parse_failure_keeps_existing_refund(self):
        reservation = {'ok': True}
        with mock.patch.object(server, 'reserve_quota', return_value=reservation), \
                mock.patch.object(server, '_parse_cached', side_effect=server.ApiError(504, 'timed out')), \
                mock.patch.object(server, 'release_quota') as release, mock.patch.object(server, 'log_request'):
            with self.assertRaises(server.ApiError):
                server.api_parse(server.ParseBody(text=self.url), make_request('/api/parse'))
        release.assert_called_once_with(reservation)
        self.assertIn('quota_released', self.codes(self.logs()[0]))
        self.assertEqual(self.logs()[0]['code'], 'timeout')

    def test_events_are_bounded_and_final_failure_is_retained(self):
        with self.assertRaises(TimeoutError):
            with server._parse_log_scope('internal', self.url):
                for _ in range(200):
                    server._parse_event('primary', 'poll', key='must-not-persist', missing=['digg', 'evil'])
                raise TimeoutError('sensitive')
        row = self.logs()[0]
        self.assertEqual(len(row['events']), 160)
        self.assertTrue(row['events'][-1]['truncated'])
        self.assertEqual(row['events'][-1]['code'], 'timeout')
        self.assertNotIn('must-not-persist', str(row))
        self.assertNotIn('evil', str(row))

    def test_diagnostic_database_failure_does_not_fail_parsing(self):
        with mock.patch.object(server, 'db_exec', side_effect=OSError('disk unavailable')):
            with server._parse_log_scope('web', self.url) as trace:
                server._parse_event('cache', 'cache_hit')
                trace['result'] = self.result
        self.assertIsNone(server._parse_trace.get())

    def test_admin_only_pagination_filters_and_detail(self):
        for user in (1, 2, 2):
            with server._parse_log_scope('web', self.url, user) as trace:
                trace['result'] = self.result
        with TestClient(server.app) as client:
            for path in ('/api/admin/parse-logs', '/api/admin/parse-logs/'+'0'*24):
                self.assertEqual(client.get(path).status_code, 401)
            client.post('/api/admin/login', json={'password': server.ADMIN_PASSWORD})
            response = client.get('/api/admin/parse-logs?size=1&user_id=2&status=success')
            self.assertEqual(response.status_code, 200)
            self.assertIn('no-store', response.headers['cache-control'])
            data = response.json()
            self.assertEqual(data['total'], 2)
            self.assertEqual(len(data['logs']), 1)
            self.assertNotIn('events', data['logs'][0])
            detail = client.get('/api/admin/parse-logs/'+data['logs'][0]['id']).json()['log']
            self.assertTrue(detail['events'])
            self.assertEqual(client.get('/api/admin/parse-logs?status=invalid').status_code, 400)
            self.assertEqual(client.get('/api/admin/parse-logs?size=1000').status_code, 422)
            self.assertEqual(client.get('/api/admin/parse-logs?search=%25').json()['total'], 0)
            self.assertEqual(client.get('/api/admin/parse-logs/not-found').status_code, 404)

    def test_retention_cleans_old_logs_and_hides_expired_details(self):
        with server._parse_log_scope('web', self.url):
            pass
        ident = self.logs()[0]['id']
        server.db_exec('UPDATE parse_logs SET ts=1 WHERE id=?', (ident,))
        with mock.patch.object(server, '_require_admin'):
            with self.assertRaises(server.ApiError):
                server.admin_parse_log_detail(ident, make_request())
        server._cleanup_retained_data(force=True)
        self.assertEqual(self.logs(), [])

    def test_startup_marks_interrupted_logs(self):
        with server._parse_log_scope('web', self.url):
            pass
        server.db_exec("UPDATE parse_logs SET status='running'")
        with TestClient(server.app) as client:
            self.assertEqual(client.get('/healthz').status_code, 200)
        self.assertEqual(self.logs()[0]['status'], 'interrupted')

    def test_media_queue_reuses_same_log_across_submit_and_poll(self):
        job_id = server.db_exec("INSERT INTO atc_jobs(item_id,work_url,purpose,status,created,updated,lease_owner) VALUES(?,?,?,'submitting',?,?,'test')",
                               (self.result['item_id'], self.url, 'download', int(time.time()), int(time.time())))
        self.addCleanup(server.db_exec, 'DELETE FROM atc_jobs WHERE id=?', (job_id,))
        job = dict(server.db_exec('SELECT * FROM atc_jobs WHERE id=?', (job_id,), 'one'))
        with mock.patch.object(server, '_atc_request', return_value={'code': 200, 'data': {'taskId':'sensitive-task-id','status':'WAITING'}}):
            server._atc_submit_claimed(job, {})
        first = self.logs()[0]
        self.assertEqual(first['status'], 'running')
        server.db_exec("UPDATE atc_jobs SET lease_owner='test' WHERE id=?", (job_id,))
        job = dict(server.db_exec('SELECT * FROM atc_jobs WHERE id=?', (job_id,), 'one'))
        with mock.patch.object(server, '_atc_request', return_value={'code':200,'data':{'status':'SUCCESS','videoUrl':'https://v3.douyinvod.com/secret'}}), \
                mock.patch.object(server, '_atc_store_job_result'):
            server._atc_poll_claimed(job, {})
        self.assertEqual(len(self.logs()), 1)
        self.assertEqual(self.logs()[0]['id'], first['id'])
        self.assertEqual(self.logs()[0]['status'], 'success')
        self.assertEqual(self.logs()[0]['code'], 'media_ready')
        self.assertNotIn('sensitive-task-id', str(self.logs()))
