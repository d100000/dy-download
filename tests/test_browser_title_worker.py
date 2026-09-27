"""浏览器标题池离线回归：作品归属、网络边界、时间预算和子进程回收。"""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from browser_titles import BrowserTitleService, valid_work_url, work_identity
from tools.browser_title_worker import (AnonymousBrowserSession, NetworkPolicy, browser_launch_options,
                                       collect_title, extract_structured, fetch_title, pick_result)


ITEM = "7689724147420630306"
OTHER = "7689724147420630307"
URL = "https://www.douyin.com/video/%s/" % ITEM


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class FakePage:
    def __init__(self, states, url=URL):
        self.url, self.states = url, states
        self.clock = FakeClock()
        self.refreshes = 0
        self.reads = 0

    def goto(self, *args, **kwargs):
        pass

    def evaluate(self, expression):
        self.reads += 1
        return self.states(self.clock.value, self.refreshes)

    def reload(self, **kwargs):
        self.refreshes += 1

    def wait_for_timeout(self, milliseconds):
        self.clock.value += milliseconds / 1000.0


class BrowserTitleExtractionTests(unittest.TestCase):
    def test_accepts_only_official_https_work_or_shortlink(self):
        self.assertEqual(work_identity(URL), ("video", ITEM))
        self.assertTrue(valid_work_url("https://v.douyin.com/abc_d-123/"))
        self.assertEqual(work_identity("https://www.iesdouyin.com/share/slides/%s/" % ITEM), ("note", ITEM))
        for url in ("http://www.douyin.com/video/" + ITEM, "https://www.douyin.com@localhost/video/" + ITEM,
                    "https://evil.douyin.com/video/" + ITEM, "https://www.douyin.com:8443/video/" + ITEM,
                    "https://www.douyin.com/user/" + ITEM, "https://127.0.0.1/video/" + ITEM,
                    "https://v.douyin.com/abc/extra", URL + "\n"):
            self.assertFalse(valid_work_url(url), url)

    def test_structured_result_must_have_matching_identity_and_complete_evidence(self):
        payload = {"items": [{"aweme_id": OTHER, "full_desc": "别人的完整标题"},
                             {"aweme_id": ITEM, "desc": "我的标题..."}]}
        result = extract_structured([payload], ITEM, "video")
        self.assertEqual(result["title"], "我的标题...")
        self.assertEqual(result["title_status"], "partial")
        payload["items"].append({"aweme_id": ITEM, "full_desc": "我的完整标题"})
        result = extract_structured([payload], ITEM, "video")
        self.assertEqual(result["title_status"], "complete")
        payload["items"][-1]["is_truncated"] = True
        self.assertEqual(extract_structured([payload], ITEM, "video")["title_status"], "partial")
        self.assertIsNone(extract_structured([{"aweme_id": OTHER, "title": "错误作品"}], ITEM, "video"))
        self.assertIsNone(extract_structured([{"title": "无归属标题"}], ITEM, "video"))

    def test_available_description_does_not_pretend_complete(self):
        result = extract_structured([{"aweme_id": ITEM, "desc": "正常描述"}], ITEM, "video")
        self.assertEqual(result["title_status"], "available")
        self.assertEqual(extract_structured([{"aweme_id": ITEM, "desc": "验证码的工作原理"}], ITEM, "video")["title"], "验证码的工作原理")
        self.assertIsNone(extract_structured([{"aweme_id": ITEM, "desc": "抖音 - 记录美好生活"}], ITEM, "video"))

    def test_dom_hydration_upgrades_truncated_title_without_selecting_recommendation(self):
        def states(now, refreshed):
            return {"titles": [{"item_id": OTHER, "title": "推荐视频不能串入"},
                               {"item_id": ITEM, "title": "加载后的完整描述" if now >= .5 else "截断..."}]}
        page = FakePage(states)
        result = collect_title(page, URL, 1.2, clock=page.clock)
        self.assertEqual(result["title"], "加载后的完整描述")
        self.assertEqual(result["title_source"], "browser_dom")
        self.assertEqual(result["title_status"], "available")
        self.assertEqual(page.refreshes, 0)

    def test_truncated_title_does_not_trigger_blind_refresh_and_keeps_deadline(self):
        page = FakePage(lambda now, refreshed: {"titles": [{"item_id": ITEM, "title": "仍然截断..."}]})
        result = collect_title(page, URL, 6, clock=page.clock)
        self.assertEqual(result["title_status"], "partial")
        self.assertEqual(page.refreshes, 0)
        self.assertLess(page.clock.value, 6)

    def test_stable_dom_title_returns_early_without_claiming_complete(self):
        page = FakePage(lambda *_: {"titles": [{"item_id": ITEM, "title": "稳定的标题"}]})
        result = collect_title(page, URL, 6, clock=page.clock)
        self.assertEqual(result["title_status"], "available")
        self.assertGreaterEqual(page.clock.value, .5)
        self.assertLess(page.clock.value, 1)
        self.assertEqual(page.refreshes, 0)

    def test_explicit_temporary_http_error_can_refresh_once(self):
        page = FakePage(lambda now, refreshed: {"payloads": [{"aweme_id": ITEM, "full_desc": "完整原文"}]} if refreshed else {})
        page.goto = lambda *args, **kwargs: mock.Mock(status=503)
        diagnostics = {}
        result = collect_title(page, URL, 6, clock=page.clock, diagnostics=diagnostics)
        self.assertEqual(result["title_status"], "complete")
        self.assertEqual(page.refreshes, 1)
        self.assertEqual(diagnostics["reload_count"], 1)
        self.assertLess(page.clock.value, 3)

    def test_normal_slow_hydration_is_not_interrupted_at_two_seconds(self):
        page = FakePage(lambda now, _: {"payloads": [{"aweme_id": ITEM, "desc": "正常加载的文案"}]} if now >= 2.7 else {})
        result = collect_title(page, URL, 6, clock=page.clock)
        self.assertEqual(result["title"], "正常加载的文案")
        self.assertEqual(page.refreshes, 0)
        self.assertLess(page.clock.value, 2.9)

    def test_valid_structured_description_returns_without_dom_stability_delay(self):
        page = FakePage(lambda *_: {"payloads": [{"aweme_id": ITEM, "desc": "结构化描述"}]})
        diagnostics = {}
        result = collect_title(page, URL, 6, clock=page.clock, diagnostics=diagnostics)
        self.assertEqual(result["title_status"], "available")
        self.assertEqual(page.clock.value, 0)
        self.assertEqual(diagnostics["first_title_s"], 0)

    def test_captured_detail_does_not_depend_on_evaluating_dom(self):
        page = FakePage(lambda *_: {})
        page.evaluate = mock.Mock(side_effect=RuntimeError("document changed"))
        result = collect_title(page, URL, 6, captured=[{"aweme_detail": {"aweme_id": ITEM, "desc": "接口文案"}}],
                               clock=page.clock)
        self.assertEqual(result["title"], "接口文案")
        self.assertEqual(page.clock.value, 0)
        page.evaluate.assert_not_called()

    def test_rate_limit_and_navigation_timeout_do_not_trigger_reload(self):
        for response in (mock.Mock(status=429), TimeoutError("still loading")):
            page = FakePage(lambda *_: {})
            page.goto = mock.Mock(side_effect=response if isinstance(response, Exception) else None,
                                  return_value=response)
            diagnostics = {}
            self.assertIsNone(collect_title(page, URL, 6, clock=page.clock, diagnostics=diagnostics))
            self.assertEqual(page.refreshes, 0)
            self.assertEqual(diagnostics["error_code"], "navigation_failed" if isinstance(response, Exception)
                             else "upstream_rate_limited")

    def test_structured_metadata_is_same_work_only_and_preserves_zero(self):
        payload = {"items": [{"aweme_id": OTHER, "desc": "推荐", "author": {"nickname": "错误作者"}},
                             {"aweme_id": ITEM, "desc": "目标版本过低，升级后可展示全部信息",
                              "author": {"nickname": "目标作者"}, "create_time": 1790404437,
                              "statistics": {"digg_count": 0, "comment_count": 12},
                              "video": {"duration": 1000, "width": 1920, "height": 1080}}]}
        result = extract_structured([payload], ITEM, "video")
        self.assertEqual(result["author"], "目标作者")
        self.assertEqual(result["title_status"], "partial")
        self.assertEqual(result["content_status"], "partial")
        self.assertEqual(result["stats"], {"digg": 0, "comment": 12})
        self.assertEqual(result["duration_ms"], 1000)
        self.assertEqual(result["video"], {"width": 1920, "height": 1080})

    def test_merge_same_work_metadata_does_not_overwrite_zero_or_title_quality(self):
        first = {"item_id": ITEM, "kind": "video", "title": "完整标题", "title_status": "complete",
                 "stats": {"digg": 0}}
        second = {"item_id": ITEM, "kind": "video", "title": "标题...", "title_status": "partial",
                  "stats": {"digg": 99, "share": 2}, "author": "作者"}
        result = pick_result(first, second)
        self.assertEqual(result["title"], "完整标题")
        self.assertEqual(result["stats"], {"digg": 0, "share": 2})
        self.assertEqual(result["author"], "作者")
        second["item_id"] = OTHER
        self.assertNotIn("author", pick_result(first, second))

    def test_redirect_to_different_work_rejected(self):
        page = FakePage(lambda *_: {"titles": [{"item_id": OTHER, "title": "错误页面"}]}, URL.replace(ITEM, OTHER))
        self.assertIsNone(collect_title(page, URL, 6, clock=page.clock))

    def test_network_policy_rejection_does_not_wait_or_refresh(self):
        page = FakePage(lambda *_: {})
        self.assertIsNone(collect_title(page, URL, 6, clock=page.clock, stop_requested=lambda: True))
        self.assertEqual(page.reads, 0)
        self.assertEqual(page.refreshes, 0)
        self.assertEqual(page.clock.value, 0)

    def test_network_rejects_private_dns_third_party_and_iframe_navigation(self):
        public = lambda *_args, **_kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))]
        policy = NetworkPolicy(public)
        self.assertTrue(policy.allows(URL, "document", True))
        self.assertTrue(policy.allows("https://lf.douyinstatic.com/a.js", "script"))
        self.assertFalse(policy.allows(URL, "document", True, False))
        self.assertFalse(policy.allows("https://www.douyin.com/login/", "document", True))
        self.assertFalse(policy.allows("https://evil.example/a.js", "script"))
        self.assertFalse(policy.allows("https://www.douyin.com/a.mp4", "media"))
        self.assertFalse(policy.allows("https://douyin.com.evil.example/a", "script"))
        private = lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))]
        private_policy = NetworkPolicy(private)
        self.assertFalse(private_policy.allows(URL, "document", True))
        self.assertEqual(private_policy.error_code, "blocked_private_dns")
        fake_ip = lambda *_args, **_kwargs: [(2, 1, 6, "", ("198.18.0.151", 443))]
        self.assertFalse(NetworkPolicy(fake_ip).allows(URL, "document", True))
        mixed = lambda *_args, **_kwargs: public() + [(2, 1, 6, "", ("169.254.169.254", 443))]
        self.assertFalse(NetworkPolicy(mixed).allows(URL, "document", True))


class BrowserTitleSessionTests(unittest.TestCase):
    def make_browser(self):
        pages, contexts = [], []

        def make_context(**_options):
            context = mock.Mock()
            context.events = {}
            context.on.side_effect = lambda name, handler: context.events.update({name: handler})

            def new_page():
                title = "独立页面文案 %d" % len(pages)
                page = FakePage(lambda *_: {"payloads": [{"aweme_id": ITEM, "desc": title}]})
                page.events = {}
                page.on = lambda name, handler: page.events.update({name: handler})
                page.close = mock.Mock()
                page.set_default_timeout = mock.Mock()
                page.main_frame = object()
                pages.append(page)
                context.events["page"](page)
                return page

            context.new_page.side_effect = new_page
            contexts.append(context)
            return context

        browser = mock.Mock()
        browser.new_context.side_effect = make_context
        return browser, contexts, pages

    def test_context_is_reused_but_each_task_gets_new_page_policy_and_result(self):
        browser, contexts, pages = self.make_browser()
        session = AnonymousBrowserSession(browser)
        self.addCleanup(session.close)
        outcomes, diagnostics = [], []
        for _ in range(2):
            diagnostic = {}
            outcomes.append(fetch_title(browser, URL, time.monotonic() + 6, diagnostic, session=session))
            diagnostics.append(diagnostic)
        self.assertEqual(browser.new_context.call_count, 1)
        self.assertEqual(len(pages), 2)
        self.assertNotEqual(outcomes[0]["title"], outcomes[1]["title"])
        self.assertEqual([d["context_reused"] for d in diagnostics], [False, True])
        self.assertIsNone(session.policy)
        self.assertIsNone(session.page)
        for page in pages:
            page.close.assert_called_once()
        contexts[0].close.assert_not_called()
        contexts[0].route.assert_called_once()
        contexts[0].route_web_socket.assert_called_once()
        self.assertEqual(browser.new_context.call_args.kwargs["service_workers"], "block")
        self.assertFalse(browser.new_context.call_args.kwargs["accept_downloads"])
        self.assertGreaterEqual(diagnostics[1]["elapsed_s"], diagnostics[1]["first_title_s"])
        self.assertEqual(diagnostics[1]["error_code"], "")

    def test_context_rotates_after_task_limit_ttl_or_failure(self):
        browser, contexts, _ = self.make_browser()
        clock = FakeClock()
        session = AnonymousBrowserSession(browser, max_tasks=2, ttl_seconds=10, clock=clock)
        self.addCleanup(session.close)
        for _ in range(3):
            session.acquire(NetworkPolicy())
            session.release()
        self.assertEqual(len(contexts), 2)
        contexts[0].close.assert_called_once()
        clock.value = 11
        session.acquire(NetworkPolicy())
        self.assertEqual(len(contexts), 3)
        session.release(healthy=False)
        self.assertIsNone(session.context)
        contexts[2].close.assert_called_once()

    def test_legacy_fetch_call_still_closes_temporary_context(self):
        browser, contexts, pages = self.make_browser()
        self.assertIsNotNone(fetch_title(browser, URL, time.monotonic() + 6))
        contexts[0].close.assert_called_once()
        pages[0].close.assert_called_once()

    def test_idle_session_aborts_requests_and_closes_popups(self):
        browser, contexts, _ = self.make_browser()
        session = AnonymousBrowserSession(browser)
        self.addCleanup(session.close)
        page, _ = session.acquire(NetworkPolicy(lambda *_a, **_k: [(2, 1, 6, "", ("8.8.8.8", 443))]))
        popup = mock.Mock()
        contexts[0].events["page"](popup)
        popup.close.assert_called_once()
        route = mock.Mock()
        route.request.url, route.request.resource_type = URL, "document"
        route.request.is_navigation_request.return_value = True
        route.request.frame = page.main_frame
        session._route(route)
        route.continue_.assert_called_once()
        session.release()
        session._route(route)
        route.abort.assert_called_once()

    def test_error_responses_cannot_contribute_structured_candidates(self):
        browser, _, pages = self.make_browser()

        def collect(page, _url, _deadline, captured, **_kwargs):
            for status, payload in ((403, {"aweme_id": ITEM, "desc": "无效 HTTP"}),
                                    (200, {"status_code": 1, "aweme_detail": {"aweme_id": ITEM, "desc": "无效业务响应"}})):
                response = mock.Mock(url="https://www.douyin.com/aweme/v1/web/aweme/detail/",
                                     status=status, headers={})
                response.body.return_value = json.dumps(payload).encode()
                page.events["response"](response)
            self.assertEqual(captured, [])
            return None

        with mock.patch("tools.browser_title_worker.collect_title", side_effect=collect):
            self.assertIsNone(fetch_title(browser, URL, time.monotonic() + 6))
        pages[0].close.assert_called_once()

    def test_proxy_requires_explicit_controlled_configuration(self):
        with mock.patch.dict("os.environ", {"HTTPS_PROXY": "http://unused.example:8080"}):
            self.assertNotIn("proxy", browser_launch_options())
        self.assertEqual(browser_launch_options("http://127.0.0.1:7897")["proxy"],
                         {"server": "http://127.0.0.1:7897"})
        self.assertEqual(browser_launch_options("http://user:secret@localhost:80")["proxy"],
                         {"server": "http://localhost:80", "username": "user", "password": "secret"})
        for value in ("file:///tmp/proxy", "socks5://user:secret@localhost:80", "http://localhost:abc",
                      "http://localhost:80/path", "http://localhost:0", "http://localhost:80?token=secret"):
            with self.assertRaisesRegex(ValueError, "^invalid_proxy_config$"):
                browser_launch_options(value)


class BrowserTitlePoolTests(unittest.TestCase):
    def make_service(self, body, **options):
        directory = tempfile.TemporaryDirectory(prefix="title-worker-test-")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "worker.py"
        path.write_text("import json,sys,time,os\n" + body, encoding="utf-8")
        service = BrowserTitleService(enabled=True, workers=1, startup_timeout=.5, **options)
        service._command = [sys.executable, "-u", str(path)]
        patcher = mock.patch("browser_titles._dependency_available", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(service.close)
        return service

    def wait_ready(self, service):
        service.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and service.status()["ready"] != 1:
            time.sleep(.01)
        self.assertEqual(service.status()["ready"], 1)

    def test_dependency_missing_rejects_without_starting_processes(self):
        service = BrowserTitleService(enabled=True)
        with mock.patch("browser_titles._dependency_available", return_value=False), mock.patch("subprocess.Popen") as spawn:
            self.assertFalse(service.submit(URL, lambda _: None))
            service.start()
            self.assertEqual(service.status()["error_code"], "dependency_missing")
            spawn.assert_not_called()

    def test_reuses_same_process_for_multiple_jobs(self):
        body = "print(json.dumps({'ready':True}),flush=True)\nfor line in sys.stdin:\n print(json.dumps({'result':{'pid':os.getpid()}}),flush=True)\n"
        service = self.make_service(body, timeout=.8)
        self.wait_ready(service)
        results = []
        for _ in range(2):
            done = threading.Event()
            self.assertTrue(service.submit(URL, lambda result: (results.append(result), done.set())))
            self.assertTrue(done.wait(1))
        self.assertEqual(results[0]["pid"], results[1]["pid"])

    def test_hung_or_partial_line_worker_is_killed_at_deadline(self):
        body = "print(json.dumps({'ready':True}),flush=True)\nfor line in sys.stdin:\n sys.stdout.write('{');sys.stdout.flush();time.sleep(30)\n"
        service = self.make_service(body, timeout=.2)
        self.wait_ready(service)
        done, results = threading.Event(), []
        start = time.monotonic()
        self.assertTrue(service.submit(URL, lambda result: (results.append(result), done.set())))
        self.assertTrue(done.wait(1))
        self.assertEqual(results, [None])
        self.assertLess(time.monotonic() - start, .8)
        self.assertEqual(service.status()["ready"], 0)
        self.assertFalse(service._processes)

    def test_bounded_queue_expires_while_worker_busy(self):
        body = "print(json.dumps({'ready':True}),flush=True)\nfor line in sys.stdin:\n time.sleep(.35); print(json.dumps({'result':{'done':True}}),flush=True)\n"
        service = self.make_service(body, timeout=.25, max_queue=1)
        self.wait_ready(service)
        results, done = [], threading.Event()
        def callback(result):
            results.append(result)
            if len(results) == 2:
                done.set()
        self.assertTrue(service.submit(URL, callback))
        limit = time.monotonic() + .5
        while service.status()["pending"] and service._queue.qsize() and time.monotonic() < limit:
            time.sleep(.005)
        self.assertTrue(service.submit(URL, callback))
        self.assertFalse(service.submit(URL, callback))
        self.assertTrue(done.wait(1))
        self.assertEqual(results, [None, None])

    def test_startup_failure_finishes_pending_without_permanent_disable(self):
        body = "time.sleep(.05)\nprint(json.dumps({'ready':False}),flush=True)\n"
        service = self.make_service(body, timeout=.2)
        done = threading.Event()
        self.assertTrue(service.submit(URL, lambda result: done.set()))
        self.assertTrue(done.wait(1))
        self.assertTrue(service.submit(URL, lambda _: None))
        self.assertEqual(service.status()["error_code"], "browser_unavailable")

    def test_network_diagnostic_reaches_status_without_exposing_raw_errors(self):
        body = "print(json.dumps({'ready':True}),flush=True)\nfor line in sys.stdin:\n print(json.dumps({'result':None,'error_code':'blocked_private_dns'}),flush=True)\n"
        service = self.make_service(body, timeout=.3)
        self.wait_ready(service)
        done = threading.Event()
        self.assertTrue(service.submit(URL, lambda _: done.set()))
        self.assertTrue(done.wait(1))
        self.assertEqual(service.status()["error_code"], "blocked_private_dns")

    def test_close_completes_queued_and_running_tasks(self):
        body = "print(json.dumps({'ready':True}),flush=True)\nfor line in sys.stdin:\n time.sleep(30)\n"
        service = self.make_service(body, timeout=4, max_queue=2)
        self.wait_ready(service)
        results = []
        self.assertTrue(service.submit(URL, results.append))
        self.assertTrue(service.submit(URL, results.append))
        service.close()
        self.assertEqual(results, [None, None])
        self.assertEqual(service.status()["pending"], 0)
        self.assertFalse(service.submit(URL, results.append))


if __name__ == "__main__":
    unittest.main()
