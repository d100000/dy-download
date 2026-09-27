"""标题任务去重、身份绑定与只读签名元数据接口；无网络或浏览器依赖。"""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

from tests.test_security_reliability import TestClient, server
from browser_titles import BrowserTitleService


class BrowserTitleLifecycleTests(unittest.TestCase):
    item_id = '7689777123456799991'
    other_id = '7689777123456799992'
    work_url = 'https://www.douyin.com/video/' + item_id + '/'
    short_url = 'https://v.douyin.com/TitleLifecycle/'

    def setUp(self):
        server.db_exec('DELETE FROM parse_snapshots WHERE item_id IN (?,?)',
                       (self.item_id, self.other_id))
        for cache in (server._cache, server._browser_title_jobs, server._browser_title_results):
            patch = mock.patch.dict(cache, {}, clear=True)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.dict(server.proxy_mgr.settings, {'force_proxy': False})
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(server, '_browser_title_service')
        self.service = patch.start()
        self.service.submit.return_value = True
        self.addCleanup(patch.stop)
        self.data = {
            'item_id': self.item_id, 'kind': 'video', 'platform': 'douyin',
            'title': '（无标题）', '_link': self.work_url, 'source': 'parser',
            'video': {'url': 'https://v3.douyinvod.com/private-signed.mp4?secret=test',
                      'direct_url': 'https://v3.douyinvod.com/private-signed.mp4?secret=test',
                      'filename': 'original.mp4'},
            'cover': 'https://p3.douyinpic.com/private-image.jpeg',
            'author': '已获取公开作者', 'stats': {'digg': 0},
        }
        self.result = {
            'item_id': self.item_id, 'kind': 'video', '_link': self.work_url,
            'title': '晚到的完整作品标题', 'title_source': 'browser_structured',
            'title_status': 'complete',
        }
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)

    def signed_url(self, item_id=None, kind='video', token_kind='parse_metadata', exp=None):
        item_id = item_id or self.item_id
        if exp is None:
            exp, sig = server._media_token(token_kind, kind + ':' + item_id, ttl=600)
        else:
            sig = server._media_signature(token_kind, kind + ':' + item_id, exp)
        return '/api/parse-metadata/' + item_id + '?' + urlencode({
            'kind': kind, 'exp': exp, 'sig': sig})

    def save(self, data=None):
        return server._save_parse_snapshot(copy.deepcopy(data or self.data), self.work_url)

    def test_canonical_variations_share_one_inflight_browser_task(self):
        links = [self.work_url, self.work_url.rstrip('/'),
                 self.work_url + '?utm_source=copy#ignored',
                 'https://www.iesdouyin.com/share/video/' + self.item_id + '/']
        for link in links:
            self.assertTrue(server._browser_title_start(link))
        self.service.submit.assert_called_once()
        self.assertEqual(self.service.submit.call_args.args[0], self.work_url)
        self.assertEqual(len(server._browser_title_jobs), 1)

    def test_short_link_pending_task_binds_to_media_identity(self):
        self.assertTrue(server._browser_title_start(self.short_url))
        self.assertEqual(server._browser_title_state(self.data), 'unavailable')
        server._browser_title_merge(self.short_url, self.data)
        self.assertEqual(server._browser_title_state(self.data), 'pending')
        callback = self.service.submit.call_args.args[1]
        callback(copy.deepcopy(self.result))
        self.assertEqual(server._browser_title_state(self.data), 'ready')
        self.assertEqual(server._browser_title_merge('', self.data)['title'], self.result['title'])

    def test_short_link_cannot_rebind_or_accept_another_media_item(self):
        server._browser_title_start(self.short_url)
        server._browser_title_merge(self.short_url, self.data)
        other = dict(self.data, item_id=self.other_id)
        server._browser_title_merge(self.short_url, other)
        wrong = dict(self.result, item_id=self.other_id,
                     _link='https://www.douyin.com/video/' + self.other_id + '/')
        self.service.submit.call_args.args[1](wrong)
        self.assertFalse(server._browser_title_results)
        self.assertEqual(server._browser_title_state(self.data), 'unavailable')
        self.assertEqual(server._browser_title_merge('', self.data), self.data)

    def test_bound_short_link_and_canonical_share_one_pending_task(self):
        self.assertTrue(server._browser_title_start(self.short_url))
        server._browser_title_merge(self.short_url, self.data)
        self.assertTrue(server._browser_title_start(self.work_url))
        self.service.submit.assert_called_once()
        self.assertEqual(self.service.submit.call_args.args[0], self.short_url)

    def test_callback_rejects_mismatched_link_item_kind_and_source(self):
        changes = [
            {'_link': 'https://www.douyin.com/video/' + self.other_id + '/'},
            {'item_id': self.other_id}, {'kind': 'note'},
            {'_link': 'https://example.com/video/' + self.item_id + '/'},
            {'title_source': 'custom'}, {'title_status': 'unverified'},
        ]
        for change in changes:
            with self.subTest(change=change):
                server._browser_title_complete(self.work_url, dict(self.result, **change))
                self.assertFalse(server._browser_title_results)
                self.assertEqual(server._browser_title_merge('', self.data), self.data)

    def test_strict_proxy_mode_does_not_submit_or_create_browser_task(self):
        with mock.patch.dict(server.proxy_mgr.settings, {'force_proxy': True}):
            self.assertFalse(server._browser_title_start(self.work_url))
        self.service.submit.assert_not_called()
        self.assertFalse(server._browser_title_jobs)

    def test_failed_submission_has_a_bounded_retry_cooldown(self):
        self.service.submit.return_value = False
        with mock.patch.object(server.time, 'monotonic', return_value=1000):
            self.assertFalse(server._browser_title_start(self.work_url))
            self.assertFalse(server._browser_title_start(self.work_url))
        self.service.submit.assert_called_once()
        self.service.submit.return_value = True
        with mock.patch.object(server.time, 'monotonic', return_value=1061):
            self.assertTrue(server._browser_title_start(self.work_url))
        self.assertEqual(self.service.submit.call_count, 2)

    def test_registry_prunes_expired_and_bounds_unrelated_inputs(self):
        now = time.monotonic()
        server._browser_title_jobs['expired-job'] = {'expires_at': now - 1}
        server._browser_title_results[('video', 'expired')] = {'expires_at': now - 1}
        for index in range(600):
            server._browser_title_jobs['unrelated-' + str(index)] = {'expires_at': now + 60}
            server._browser_title_results[('video', str(index))] = {'expires_at': now + 60}
        self.assertTrue(server._browser_title_start(self.work_url))
        self.assertNotIn('expired-job', server._browser_title_jobs)
        self.assertNotIn(('video', 'expired'), server._browser_title_results)
        self.assertLessEqual(len(server._browser_title_jobs), 512)
        self.assertLessEqual(len(server._browser_title_results), 512)

    def test_result_is_visible_in_registry_while_database_write_waits(self):
        errors = []
        done = threading.Event()

        def complete():
            try:
                server._browser_title_complete(self.work_url, copy.deepcopy(self.result))
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()

        with server._db_lock:
            thread = threading.Thread(target=complete, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 2)
            deadline = time.monotonic() + 2
            while server._browser_title_state(self.data) != 'ready' and time.monotonic() < deadline:
                done.wait(0.01)
            self.assertEqual(server._browser_title_state(self.data), 'ready')
            self.assertFalse(done.is_set(), '数据库写入应该仍等待锁')
            self.assertEqual(server._browser_title_merge('', self.data)['title'], self.result['title'])
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_signed_metadata_get_is_read_only_and_exposes_public_metadata_without_media_urls(self):
        self.save()
        server._browser_title_complete(self.work_url, self.result)
        with mock.patch.object(server, '_browser_title_start', side_effect=AssertionError('不应启动浏览器')), \
                mock.patch.object(server, 'reserve_quota', side_effect=AssertionError('不应扣费')), \
                mock.patch.object(server, 'settle_quota', side_effect=AssertionError('不应结算')), \
                mock.patch.object(server, 'open_url', side_effect=AssertionError('不应访问网络')):
            response = self.client.get(self.signed_url())
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response.headers['cache-control'])
        data = response.json()['data']
        self.assertEqual(data['title'], self.result['title'])
        self.assertEqual(data['metadata_status'], 'ready')
        self.assertLessEqual(set(data), {'item_id', 'kind', 'title', 'title_source',
                                      'title_status', 'base', 'metadata_status', 'video',
                                      'author', 'avatar', 'author_url', 'cover', 'content',
                                      'content_status', 'create_time', 'duration_ms', 'stats',
                                      'tags', 'snapshot_at', 'metadata_fields', 'metadata_source'})
        self.assertLessEqual(set(data['video']), {'filename', 'width', 'height'})
        self.assertEqual(data['author'], '已获取公开作者')
        self.assertEqual(data['stats'], {'digg': 0})
        self.assertEqual(data['cover'], self.data['cover'])
        for sensitive in ('private-signed', 'secret=', '_link', 'work_url', 'direct_url', 'download_url', 'cookie'):
            self.assertNotIn(sensitive, response.text)
        self.service.submit.assert_not_called()

    def test_image_metadata_exposes_filenames_without_media_or_refresh_urls(self):
        note = dict(self.data, kind='note', images=[{
            'index': 3, 'filename': 'page_03.jpeg',
            'url': 'https://p3.douyinpic.com/private-image.jpeg',
            'download_url': '/private-signed-image?secret=test',
        }])
        note.pop('video')
        self.save(note)
        response = self.client.get(self.signed_url(kind='note'))
        self.assertEqual(response.status_code, 200)
        images = response.json()['data']['images']
        self.assertEqual(len(images), 1)
        self.assertEqual(set(images[0]), {'index', 'filename'})
        self.assertEqual(images[0]['index'], 1)
        self.assertTrue(images[0]['filename'].endswith('_01.jpeg'))
        self.assertNotIn('private-signed', response.text)
        self.assertNotIn('secret=', response.text)

    def test_metadata_token_cannot_be_replayed_for_other_item_kind_or_scope(self):
        self.save()
        valid = self.signed_url()
        paths = [valid.replace(self.item_id, self.other_id, 1),
                 valid.replace('kind=video', 'kind=note'),
                 self.signed_url(token_kind='video'),
                 self.signed_url(exp=int(time.time()) - 10),
                 '/api/parse-metadata/' + self.item_id,
                 valid.replace('sig=', 'sig=bad')]
        for path in paths:
            with self.subTest(path=path.split('?')[0]):
                self.assertEqual(self.client.get(path).status_code, 403)

    def test_metadata_requires_unexpired_same_platform_snapshot(self):
        self.assertEqual(self.client.get(self.signed_url()).status_code, 404)
        self.save()
        server.db_exec('UPDATE parse_snapshots SET expires_at=? WHERE item_id=?',
                       (int(time.time()) - 1, self.item_id))
        self.assertEqual(self.client.get(self.signed_url()).status_code, 404)
        self.save(dict(self.data, platform='tiktok'))
        self.assertEqual(self.client.get(self.signed_url()).status_code, 404)
        self.save()
        self.assertEqual(self.client.get(self.signed_url(kind='note')).status_code, 404)

    def test_parse_response_issues_signed_metadata_poll_only_while_pending(self):
        self.save()
        server._browser_title_start(self.work_url)
        pending = server._browser_title_response(self.data)
        self.assertEqual(pending['metadata_status'], 'pending')
        self.assertEqual(self.client.get(pending['metadata_url']).status_code, 200)
        server._browser_title_complete(self.work_url, self.result)
        ready = server._browser_title_response(pending)
        self.assertEqual(ready['title'], self.result['title'])
        self.assertNotIn('metadata_url', ready)
        self.assertNotIn('metadata_status', ready)


class BrowserTitleServiceLifecycleTests(unittest.TestCase):
    """以本地假 worker 验证进程边界；不启动浏览器、不访问平台。"""

    work_url = 'https://www.douyin.com/video/7689777123456799991/'

    def make_service(self, body, **options):
        directory = tempfile.TemporaryDirectory(prefix='metadata-service-test-')
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'worker.py'
        path.write_text('import json,sys,time,os\n' + body, encoding='utf-8')
        options.setdefault('workers', 1)
        options.setdefault('startup_timeout', .5)
        options.setdefault('python_executable', sys.executable)
        service = BrowserTitleService(enabled=True, **options)
        service._command = [service.python_executable, '-u', str(path)]
        self.addCleanup(service.close)
        return service

    def wait_for(self, predicate, timeout=1.5):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(.005)
        self.assertTrue(predicate())

    def result_for(self, service):
        done, values = threading.Event(), []
        self.assertTrue(service.submit(self.work_url, lambda value: (values.append(value), done.set())))
        self.assertTrue(done.wait(1.5))
        self.assertEqual(len(values), 1)
        return values[0]

    def test_separate_interpreter_uses_handshake_and_child_env_is_explicit(self):
        body = "print(json.dumps({'ready':True}),flush=True)\nfor line in sys.stdin:\n print(json.dumps({'result':{'keys':sorted(os.environ),'proxy':os.environ.get('BROWSER_TITLE_PROXY_URL')}}),flush=True)\n"
        proxy = 'http://fixture:credential@127.0.0.1:12345'
        environment = {'BROWSER_TITLE_PYTHON': sys.executable, 'ADMIN_PASSWORD': 'secret',
                       'HTTP_PROXY': 'http://unselected.example:80', 'PYTHONPATH': '/unselected',
                       'PLAYWRIGHT_BROWSERS_PATH': '/optional/browser', 'METADATA_HTTP_ENABLED': '1'}
        with mock.patch.dict(os.environ, environment), \
                mock.patch('browser_titles._dependency_available', return_value=False) as local_probe:
            service = self.make_service(body, python_executable=None, proxy=proxy)
            self.assertEqual(service.python_executable, sys.executable)
            result = self.result_for(service)
            local_probe.assert_not_called()
        self.assertEqual(result['proxy'], proxy)
        self.assertEqual(service.proxy, proxy)
        self.assertIn('PLAYWRIGHT_BROWSERS_PATH', result['keys'])
        self.assertIn('METADATA_HTTP_ENABLED', result['keys'])
        for key in ('ADMIN_PASSWORD', 'HTTP_PROXY', 'HTTPS_PROXY', 'PYTHONPATH'):
            self.assertNotIn(key, result['keys'])
        serialized = json.dumps(service.status())
        self.assertNotIn('credential', serialized)
        self.assertNotIn('127.0.0.1', serialized)
        self.assertTrue(service.status()['proxy_configured'])

    def test_explicit_empty_proxy_does_not_inherit_parent_proxy(self):
        body = "print(json.dumps({'ready':True}),flush=True)\nfor line in sys.stdin:\n print(json.dumps({'result':{'proxy':os.environ.get('BROWSER_TITLE_PROXY_URL')}}),flush=True)\n"
        with mock.patch.dict(os.environ, {'BROWSER_TITLE_PROXY_URL': 'http://ambient.example:80'}):
            service = self.make_service(body, proxy='')
            self.assertIsNone(self.result_for(service)['proxy'])
        self.assertEqual(service.proxy, '')

    def test_diagnostics_are_sanitized_and_coalesced_protocol_lines_are_preserved(self):
        diagnostics = {'navigation_s': .012, 'response_count': 3, 'context_reused': True,
                       'elapsed_s': float('nan'), 'sign_s': -1, 'detail_s': 'secret',
                       'http_attempted': True, 'http_success': False, 'browser_fallback': True,
                       'client_setup_s': .004, 'decode_s': .002, 'http_error_code': 'missing_detail',
                       'queue_s': 999, 'worker_s': 999,
                       'error_code': {'url': 'secret'}, 'proxy': 'http://secret:pass@example.test',
                       'url': self.work_url, 'cookie': 'private', 'exception': 'private'}
        body = ("print(json.dumps({'ready':True}),flush=True)\nfor line in sys.stdin:\n"
                " sys.stdout.write(json.dumps({'diagnostics':" + repr(diagnostics).replace('nan', "float('nan')") +
                "})+'\\n'+json.dumps({'result':{'title':'完整正文'}})+'\\n');sys.stdout.flush()\n")
        service = self.make_service(body)
        self.assertEqual(self.result_for(service)['title'], '完整正文')
        status = service.status()
        self.assertEqual(status['diagnostics']['navigation_s'], .012)
        self.assertEqual(status['diagnostics']['response_count'], 3)
        self.assertTrue(status['diagnostics']['context_reused'])
        self.assertTrue(status['diagnostics']['http_attempted'])
        self.assertFalse(status['diagnostics']['http_success'])
        self.assertTrue(status['diagnostics']['browser_fallback'])
        self.assertEqual(status['diagnostics']['http_error_code'], 'missing_detail')
        self.assertEqual(status['diagnostics']['client_setup_s'], .004)
        self.assertEqual(status['diagnostics']['decode_s'], .002)
        self.assertIn('queue_s', status['diagnostics'])
        self.assertIn('worker_s', status['diagnostics'])
        self.assertLess(status['diagnostics']['queue_s'], 1)
        self.assertLess(status['diagnostics']['worker_s'], 1)
        for key in ('elapsed_s', 'sign_s', 'detail_s', 'proxy', 'url', 'cookie', 'exception', 'error_code'):
            self.assertNotIn(key, status['diagnostics'])
        self.assertNotIn('secret', json.dumps(status))

    def test_unknown_http_error_string_is_removed(self):
        body = ("print(json.dumps({'ready':True}),flush=True)\nfor line in sys.stdin:\n"
                " print(json.dumps({'result':None,'diagnostics':{'http_error_code':'upstream_cookie=secret'}}),flush=True)\n")
        service = self.make_service(body)
        self.assertIsNone(self.result_for(service))
        self.assertNotIn('http_error_code', service.status()['diagnostics'])
        self.assertEqual(service.status()['error_code'], 'worker_error')

    def test_exit_and_invalid_protocol_have_distinct_failure_codes(self):
        for statement, expected in (("sys.exit(0)", 'worker_exit'),
                                    ("print('not json',flush=True)", 'worker_error')):
            with self.subTest(expected=expected):
                body = "print(json.dumps({'ready':True}),flush=True)\nfor line in sys.stdin:\n " + statement + '\n'
                service = self.make_service(body, timeout=.3)
                self.assertIsNone(self.result_for(service))
                self.assertEqual(service.status()['error_code'], expected)
                self.assertTrue(service.status()['available'])
                service.close()

    def test_expired_queue_and_running_worker_timeout_are_distinct(self):
        body = "print(json.dumps({'ready':True}),flush=True)\nfor line in sys.stdin:\n time.sleep(30)\n"
        service = self.make_service(body, timeout=.2)
        service.start()
        self.wait_for(lambda: service.status()['ready'] == 1)
        observed, done = [], threading.Event()
        def callback(result):
            observed.append((result, service.status()['error_code']))
            if len(observed) == 2:
                done.set()
        self.assertTrue(service.submit(self.work_url, callback))
        self.wait_for(lambda: service._active == 1)
        self.assertTrue(service.submit(self.work_url, callback))
        self.assertTrue(done.wait(1))
        self.assertEqual(observed, [(None, 'worker_timeout'), (None, 'queue_timeout')])
        self.assertFalse(service._processes)
        self.assertEqual(service.status()['completed'], 2)
        self.assertEqual(service.status()['succeeded'], 0)
        self.assertEqual(service.status()['failed'], 2)

    def test_initial_failure_recovers_on_later_spawn(self):
        body = ("from pathlib import Path\nmarker=Path(__file__).with_suffix('.started')\n"
                "if not marker.exists():\n marker.touch();print(json.dumps({'ready':False}),flush=True);sys.exit(0)\n"
                "print(json.dumps({'ready':True}),flush=True)\nfor line in sys.stdin:\n"
                " print(json.dumps({'result':{'title':'恢复'}}),flush=True)\n")
        service = self.make_service(body, timeout=.8)
        self.assertEqual(self.result_for(service)['title'], '恢复')
        self.assertTrue(service.status()['available'])
        self.assertEqual(service.status()['ready'], 1)

    def test_queue_deadline_does_not_wait_for_slow_initial_handshake(self):
        body = "time.sleep(30)\n"
        service = self.make_service(body, timeout=.15, startup_timeout=2)
        started_at = time.monotonic()
        self.assertIsNone(self.result_for(service))
        self.assertLess(time.monotonic() - started_at, .5)
        self.assertEqual(service.status()['error_code'], 'queue_timeout')

    def test_one_failed_slot_keeps_healthy_worker_and_queue_alive(self):
        body = "print(json.dumps({'ready':True}),flush=True)\nfor line in sys.stdin:\n time.sleep(.03);print(json.dumps({'result':{'title':'正常'}}),flush=True)\n"
        service = self.make_service(body, workers=2, max_queue=8, timeout=1)
        spawn = service._spawn
        def per_slot_spawn(deadline=None):
            if threading.current_thread().name.endswith('-0'):
                time.sleep(.05)
                raise RuntimeError('fixture startup failure contains private text')
            return spawn(deadline)
        values = []
        with mock.patch.object(service, '_spawn', side_effect=per_slot_spawn):
            for _ in range(6):
                self.assertTrue(service.submit(self.work_url, values.append))
            self.wait_for(lambda: len(values) == 6)
            self.assertGreaterEqual(sum(value is not None for value in values), 4)
            self.assertTrue(service.status()['available'])
            self.assertEqual(service.status()['ready'], 1)
            self.assertNotIn('private', json.dumps(service.status()))
            service.close()

    def test_large_valid_metadata_message_is_supported_but_ipc_is_bounded(self):
        for length, success in ((90000, True), (300000, False)):
            with self.subTest(length=length):
                body = ("print(json.dumps({'ready':True}),flush=True)\nfor line in sys.stdin:\n"
                        " print(json.dumps({'result':{'content':'x'*" + str(length) + "}}),flush=True)\n")
                service = self.make_service(body, timeout=.8)
                result = self.result_for(service)
                if success:
                    self.assertEqual(len(result['content']), length)
                else:
                    self.assertIsNone(result)
                    self.assertEqual(service.status()['error_code'], 'worker_error')
                service.close()

    def test_recent_diagnostics_are_bounded_and_count_only_final_tasks(self):
        body = ("print(json.dumps({'ready':True,'diagnostics':{'startup_s':999}}),flush=True)\ni=0\n"
                "for line in sys.stdin:\n i+=1\n"
                " print(json.dumps({'diagnostics':{'navigation_s':.01,'cookie':'secret'}}),flush=True)\n"
                " print(json.dumps({'result':{'title':'正文'} if i<35 else None,'error_code':'' if i<35 else 'title_not_found'}),flush=True)\n")
        service = self.make_service(body)
        service.start()
        self.wait_for(lambda: service.status()['ready'] == 1)
        self.assertEqual(service.status()['completed'], 0)
        for _ in range(35):
            self.result_for(service)
        status = service.status()
        self.assertEqual((status['completed'], status['succeeded'], status['failed']), (35, 34, 1))
        self.assertEqual(len(status['recent']), 32)
        self.assertEqual([entry['seq'] for entry in status['recent']], list(range(4, 36)))
        self.assertEqual(status['recent'][-1]['status'], 'failed')
        self.assertEqual(status['recent'][-1]['error_code'], 'title_not_found')
        self.assertNotIn('secret', json.dumps(status))
        self.assertNotIn('正文', json.dumps(status, ensure_ascii=False))
        status['recent'][0]['diagnostics']['navigation_s'] = 999
        self.assertEqual(service.status()['recent'][0]['diagnostics']['navigation_s'], .01)


if __name__ == '__main__':
    unittest.main()
