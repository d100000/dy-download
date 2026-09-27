"""双路解析失败保留主因；诊断脱敏且匿名预占失败后真实退款。"""
import copy
import io
import json
import unittest
from unittest import mock
from urllib import error as urlerr

from tests.test_security_reliability import TestClient, make_request, server


class ParserFailureDiagnosticsTests(unittest.TestCase):
    item_id = '7689777123456789012'
    url = 'https://v.douyin.com/FAILUREDIAGNOSTIC/'
    canonical = 'https://www.douyin.com/video/7689777123456789012/'
    auth_message = '视频解析服务配置异常，请联系管理员'
    private_message = 'anytocopy API_SECRET=private-secret https://private.example/token'

    def setUp(self):
        for table in ('parse_logs', 'quota_reservations', 'usage_daily'):
            server.db_exec('DELETE FROM ' + table)
        for cache in (server._cache, server._douyin_result_cache,
                      server._douyin_media_cache, server._author_cache):
            self.patch(mock.patch.dict(cache, {}, clear=True))
        self.patch(mock.patch.object(server, '_browser_title_start', return_value=False))
        self.patch(mock.patch.object(server, 'current_user', return_value=None))
        self.patch(mock.patch.object(server.urlreq, 'urlopen',
                                     side_effect=AssertionError('unexpected upstream request')))
        self.patch(mock.patch.object(server, 'open_url',
                                     side_effect=AssertionError('unexpected official request')))

    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def log(self):
        row = server.db_exec('SELECT * FROM parse_logs ORDER BY rowid DESC LIMIT 1', (), 'one')
        return server._parse_log_public(row, True)

    def assert_redacted(self, value):
        text = str(value)
        for token in ('anytocopy', 'private-secret', 'private.example', 'raw-private-body'):
            self.assertNotIn(token, text.lower())

    def test_failed_fallback_preserves_primary_classification_status_and_headers(self):
        cases = [
            (server._ParserServiceError(503, self.auth_message, reason='auth'), 'auth'),
            (server._ParserServiceError(503, '服务暂不可用', reason='not_configured'), 'not_configured'),
            (server._ParserServiceError(503, '服务暂不可用', reason='entitlement'), 'entitlement'),
            (server._ParserServiceError(503, '请求较多', reason='busy', retryable=True), 'busy'),
            (server._ParserServiceError(502, '连接异常', reason='network', retryable=True), 'network'),
            (server.ApiError(404, '作品无法解析，可能已失效、删除或设为私密'), 'unavailable'),
            (server.ApiError(504, '视频解析超时，请稍后重试'), 'timeout'),
            (server.ApiError(503, '任务较多', {'Retry-After': '9'}), 'failed'),
        ]
        for primary, expected_code in cases:
            with self.subTest(code=expected_code, headers=primary.headers):
                with mock.patch.object(server, '_atc_extract', side_effect=primary), \
                        mock.patch.object(server, '_parse_douyin_share_direct', side_effect=server.ApiError(
                            503, '抖音官方内容暂时无法获取，请稍后重试')):
                    with self.assertRaises(server.ApiError) as raised:
                        server._atc_parse_work_url(self.url)
                # 原对象保留其精确 HTTP 状态、重试头和内部 retryable/reason。
                self.assertIs(raised.exception, primary)
                row = self.log()
                self.assertEqual((row['status'], row['code']), ('failed', expected_code))
                events = row['events']
                failure = next(e for e in events if e['code'] == 'fallback_failed')
                self.assertEqual((failure['stage'], failure['reason']), ('official', 'failed'))
                self.assertEqual(events[-1]['code'], expected_code)
                self.assertNotIn('fallback_ok', [e['code'] for e in events])

    def test_unexpected_fallback_failure_is_classified_without_losing_primary_cause(self):
        primary = server._ParserServiceError(503, self.auth_message, reason='auth')
        with mock.patch.object(server, '_atc_extract', side_effect=primary), \
                mock.patch.object(server, '_parse_douyin_share_direct',
                                  side_effect=RuntimeError(self.private_message)):
            with self.assertRaises(server.ApiError) as raised:
                server._atc_parse_work_url(self.url)
        self.assertIs(raised.exception, primary)
        row = self.log()
        self.assertEqual(row['code'], 'auth')
        self.assertEqual(next(e for e in row['events'] if e['code'] == 'fallback_failed')['reason'], 'internal')
        self.assert_redacted(row)

    def test_successful_fallback_still_returns_media_after_primary_failure(self):
        media = {'item_id': self.item_id, 'kind': 'video', 'source': 'douyin_direct',
                 'platform': 'douyin', 'title': '官方作品', 'author': '作者',
                 'stats': {'digg': 0, 'comment': 0, 'share': 0, 'collect': 0},
                 'video': {'url': 'https://v3.douyinvod.com/official.mp4',
                           'media_available': True}}
        with mock.patch.object(server, '_atc_extract', side_effect=server._ParserServiceError(
                503, '请求较多', reason='busy', retryable=True)), \
                mock.patch.object(server, '_parse_douyin_share_direct', return_value=copy.deepcopy(media)):
            result = server._atc_parse_work_url(self.url)
        self.assertEqual(result['video']['url'], media['video']['url'])
        row = self.log()
        self.assertEqual(row['status'], 'success')
        self.assertIn('fallback_ok', [e['code'] for e in row['events']])
        self.assertNotIn('fallback_failed', [e['code'] for e in row['events']])

    def test_official_html_network_failures_record_only_safe_classification_and_status(self):
        cases = [
            (urlerr.HTTPError(self.canonical + '?secret=private-secret', 403,
                             self.private_message, {}, io.BytesIO(b'raw-private-body')), 'network', 403),
            (urlerr.HTTPError(self.canonical, 404, self.private_message,
                             {}, io.BytesIO(b'raw-private-body')), 'unavailable', 404),
            (urlerr.HTTPError(self.canonical, 503, self.private_message,
                             {}, io.BytesIO(b'raw-private-body')), 'network', 503),
            (urlerr.URLError(self.private_message), 'network', None),
            (TimeoutError(self.private_message), 'timeout', None),
            (ValueError(self.private_message), 'response_invalid', None),
            (server.ApiError(503, '没有可用代理'), 'proxy_required', None),
        ]
        for failure, code, status in cases:
            with self.subTest(code=code, status=status):
                with mock.patch.object(server, '_douyin_fetch_html', side_effect=failure):
                    with self.assertRaises(server.ApiError):
                        with server._parse_log_scope('internal', self.canonical):
                            server._parse_douyin_item_direct('video', self.item_id, refresh=True)
                row = self.log()
                event = next(e for e in row['events'] if e['stage'] == 'official' and e['code'] == code)
                self.assertEqual(event.get('http_status'), status)
                self.assert_redacted(row)
                if isinstance(failure, urlerr.HTTPError):
                    self.assertTrue(failure.fp.closed)

    def test_http_auth_rejection_returns_actionable_provider_neutral_message(self):
        cfg = {'base': 'https://parser.example', 'key': 'private-key', 'secret': 'private-secret'}
        for status in (401, 403):
            with self.subTest(status=status):
                failure = urlerr.HTTPError(cfg['base'], status, self.private_message,
                                          {}, io.BytesIO(b'raw-private-body'))
                with mock.patch.object(server.urlreq, 'urlopen', side_effect=failure):
                    with self.assertRaises(server.ApiError) as raised:
                        with server._parse_log_scope('internal', self.url):
                            server._atc_request('POST', '/video/extract', {'workUrl': self.url}, cfg)
                self.assertEqual(raised.exception.message, self.auth_message)
                self.assertEqual((raised.exception.status, raised.exception.reason), (503, 'auth'))
                self.assertTrue(failure.fp.closed)
                row = self.log()
                self.assertEqual(next(e for e in row['events'] if e['code'] == 'network')['http_status'], status)
                self.assert_redacted(row)
                self.assert_redacted(raised.exception.message)

    def test_business_auth_rejection_uses_same_message_and_hides_provider_details(self):
        for payload in ({'code': 401, 'msg': self.private_message},
                        {'code': 403, 'message': self.private_message},
                        {'code': 500, 'msg': 'API key 验证失败 ' + self.private_message}):
            with self.subTest(code=payload['code']):
                failure = server._atc_rejected(payload)
                self.assertEqual((failure.status, failure.reason, failure.message),
                                 (503, 'auth', self.auth_message))
                self.assert_redacted(failure.message)

    def test_anonymous_http_failure_refunds_allowance_and_keeps_auth_diagnostics(self):
        cfg = {'enabled': True, 'key': 'private-key', 'secret': 'private-secret',
               'base': 'https://parser.example'}
        failure = urlerr.HTTPError(cfg['base'], 401, self.private_message,
                                  {}, io.BytesIO(b'raw-private-body'))
        request = make_request('/api/parse', client_ip='testclient')
        client = TestClient(server.app)
        self.addCleanup(client.close)
        with mock.patch.object(server, 'FREE_ANON_DAILY', 3), \
                mock.patch.object(server, '_atc_cfg', return_value=cfg), \
                mock.patch.object(server.urlreq, 'urlopen', side_effect=failure), \
                mock.patch.object(server, '_douyin_resolve_share_url',
                                  return_value=('video', self.item_id, self.canonical)), \
                mock.patch.object(server, '_douyin_fetch_html', return_value=('', [])):
            self.assertEqual(server.quota_status(request), (3, 0, 3))
            response = client.post('/api/parse?lang=zh', json={'text': self.url})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {'error': self.auth_message})
            self.assertEqual(server.quota_status(request), (3, 0, 3))
        reservation = server.db_exec('SELECT * FROM quota_reservations', (), 'one')
        self.assertEqual(reservation['units'], 1)
        self.assertEqual(reservation['committed_units'], 0)
        self.assertNotEqual(reservation['status'], 'pending')
        row = self.log()
        self.assertEqual((row['status'], row['code']), ('failed', 'auth'))
        codes = [e['code'] for e in row['events']]
        self.assertIn('quota_ok', codes)
        self.assertIn('quota_released', codes)
        self.assertIn('fallback_failed', codes)
        self.assertNotIn('quota_settled', codes)
        self.assert_redacted(response.text)
        raw = dict(server.db_exec('SELECT * FROM parse_logs', (), 'one'))
        self.assert_redacted(json.dumps(raw))

    def test_http_retry_after_survives_failed_official_fallback(self):
        client = TestClient(server.app)
        self.addCleanup(client.close)
        with mock.patch.object(server, '_atc_extract', side_effect=server._ParserServiceError(
                503, '视频解析请求较多，请稍后重试', reason='busy', retryable=True)), \
                mock.patch.object(server, '_parse_douyin_share_direct',
                                  side_effect=server.ApiError(503, '官方内容暂不可用')):
            response = client.post('/api/parse?lang=zh', json={'text': self.url})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers.get('Retry-After'), '5')
        self.assertEqual(self.log()['code'], 'busy')


if __name__ == '__main__':
    unittest.main()
