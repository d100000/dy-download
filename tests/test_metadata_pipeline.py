"""常驻 HTTP/匿名网页编排的离线行为测试；不安装 SDK、不启动浏览器。"""
import copy
import asyncio
import unittest

from tools.browser_title_worker import MetadataPipeline


ITEM_ID = "7689777123456799991"
OTHER_ID = "7689777123456799992"
URL = "https://www.douyin.com/video/" + ITEM_ID + "/"


def result(source="browser_structured", **changes):
    value = {"item_id": ITEM_ID, "kind": "video", "_link": URL,
             "title": "完整作品文案", "title_status": "available", "title_source": source}
    value.update(changes)
    return value


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Context:
    def __init__(self):
        self.cookie_reads = 0

    def cookies(self):
        self.cookie_reads += 1
        return [{"name": "ttwid", "value": "anonymous-fixture", "domain": ".douyin.com"}]


class Session:
    def __init__(self):
        self.context = None
        self.user_agent = "Fixture Chromium UA"
        self.closed = 0

    def close(self):
        self.closed += 1
        self.context = None


class HTTPClient:
    def __init__(self, clock):
        self.clock = clock
        self.cookies = {}
        self.user_agent = "Fixture Chromium UA"
        self.calls, self.sessions, self.responses = [], [], []
        self.closed = False
        self.cost = .1

    def set_guest_session(self, cookies, user_agent):
        self.sessions.append((copy.deepcopy(cookies), user_agent))
        self.cookies = ({row["name"]: row["value"] for row in cookies}
                        if isinstance(cookies, list) else dict(cookies))
        self.user_agent = user_agent

    async def fetch(self, url, deadline):
        self.calls.append((url, deadline))
        self.clock.advance(min(self.cost, max(0.0, deadline - self.clock())))
        value = self.responses.pop(0) if self.responses else {
            "result": result("http_structured"), "error_code": "", "timings": {"http_s": self.cost}}
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    async def close(self):
        self.closed = True


class MetadataPipelineTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.session = Clock(), Session()
        self.http = HTTPClient(self.clock)
        self.browser_calls, self.browser_results = [], []
        self.browser_cost = .25

        def browser_fetch(browser, url, deadline, diagnostics, session):
            self.browser_calls.append((url, deadline, session))
            if session.context is None:
                session.context = Context()
            self.clock.advance(self.browser_cost)
            diagnostics.update(first_title_s=.1, context_s=.01, error_code="")
            return copy.deepcopy(self.browser_results.pop(0) if self.browser_results else result())

        self.pipeline = MetadataPipeline(object(), self.session, http_client=self.http,
                                         browser_fetch=browser_fetch, clock=self.clock)
        self.addCleanup(self.pipeline.close)

    def fetch(self, budget=6, url=URL):
        diagnostics = {}
        value = self.pipeline.fetch(url, self.clock() + budget, diagnostics)
        return value, diagnostics

    def prime_guest(self):
        value, _ = self.fetch()
        self.assertEqual(value["item_id"], ITEM_ID)
        self.assertEqual(len(self.browser_calls), 1)
        self.assertEqual(self.http.calls, [])

    def test_first_request_collects_guest_session_then_warm_http_avoids_browser(self):
        self.prime_guest()
        self.assertEqual(self.session.context.cookie_reads, 1)
        self.assertEqual(self.http.cookies, {"ttwid": "anonymous-fixture"})
        self.assertEqual(self.http.sessions[-1][1], self.session.user_agent)
        expected = result("http_structured", author="公开作者", create_time=1760000000,
                          stats={"digg": 0, "comment": 4}, duration_ms=16000,
                          video={"width": 1080, "height": 1920}, tags=["电影"])
        self.http.responses.append({"result": expected, "error_code": "", "timings": {"sign_s": .02}})
        value, diagnostics = self.fetch()
        self.assertEqual(value, expected)
        self.assertEqual(len(self.browser_calls), 1)
        self.assertEqual(len(self.http.calls), 1)
        self.assertTrue(diagnostics["http_attempted"])
        self.assertTrue(diagnostics["http_success"])
        self.assertNotIn("browser_fallback", diagnostics)
        self.assertEqual(diagnostics["sign_s"], .02)

    def test_http_works_while_caller_thread_already_has_a_running_asyncio_loop(self):
        # sync_playwright 的 greenlet 正是这种环境，不能嵌套 run_until_complete。
        self.prime_guest()
        async def inside_running_loop():
            value, diagnostics = self.fetch()
            self.assertEqual(value["title_source"], "http_structured")
            self.assertTrue(diagnostics["http_success"])
        asyncio.run(inside_running_loop())
        self.assertEqual(len(self.browser_calls), 1)

    def test_http_failure_leaves_budget_and_accounts_for_browser_first_title(self):
        self.prime_guest()
        self.http.cost = 1.5
        self.http.responses.append({"result": None, "error_code": "deadline_exceeded", "timings": {"http_s": 1.5}})
        started = self.clock()
        value, diagnostics = self.fetch()
        self.assertEqual(value["item_id"], ITEM_ID)
        self.assertEqual(len(self.browser_calls), 2)
        self.assertLessEqual(self.http.calls[-1][1], started + 4)
        self.assertEqual(self.browser_calls[-1][1], started + 6)
        self.assertLess(self.clock(), started + 6)
        self.assertTrue(diagnostics["browser_fallback"])
        self.assertEqual(diagnostics["http_error_code"], "deadline_exceeded")
        self.assertAlmostEqual(diagnostics["first_title_s"], 1.6)

    def test_small_budget_skips_http_and_preserves_browser_fallback(self):
        self.prime_guest()
        value, _ = self.fetch(budget=1.8)
        self.assertEqual(value["item_id"], ITEM_ID)
        self.assertEqual(self.http.calls, [])
        self.assertEqual(len(self.browser_calls), 2)

    def test_unavailable_sdk_is_not_retried_on_every_request(self):
        self.prime_guest()
        self.http.responses.append({"result": None, "error_code": "dependency_version_mismatch"})
        self.assertIsNotNone(self.fetch()[0])
        self.assertIsNotNone(self.fetch()[0])
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(len(self.browser_calls), 3)

    def test_dom_result_gains_same_item_metadata_without_losing_zero_statistics(self):
        self.browser_results.append(result("browser_dom", stats={"digg": 0}))
        self.http.responses.append({"result": result("http_structured", author="公开作者",
            stats={"digg": 25, "comment": 0}, create_time=1760000000,
            duration_ms=16000, video={"width": 1080}, tags=["电影"]), "error_code": ""})
        value, diagnostics = self.fetch()
        self.assertEqual(value["title"], "完整作品文案")
        self.assertEqual(value["author"], "公开作者")
        self.assertEqual(value["stats"], {"digg": 0, "comment": 0})
        self.assertEqual(value["video"], {"width": 1080})
        self.assertEqual(value["create_time"], 1760000000)
        self.assertEqual(value["duration_ms"], 16000)
        self.assertEqual(value["tags"], ["电影"])
        self.assertTrue(diagnostics["http_success"])

    def test_expired_guest_is_replaced_before_next_http_request(self):
        self.prime_guest()
        original_context = self.session.context
        self.clock.advance(301)
        value, _ = self.fetch()
        self.assertEqual(value["item_id"], ITEM_ID)
        self.assertEqual(self.session.closed, 1)
        self.assertIsNot(self.session.context, original_context)
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.http.sessions[-2][0], {})
        self.assertIsNotNone(self.fetch()[0])
        self.assertEqual(len(self.http.calls), 1)

    def test_guest_request_count_is_bounded_even_when_http_always_succeeds(self):
        self.prime_guest()
        for _ in range(64):
            self.assertIsNotNone(self.fetch()[0])
        self.assertEqual(len(self.http.calls), 64)
        self.assertEqual(len(self.browser_calls), 1)
        self.assertIsNotNone(self.fetch()[0])
        self.assertEqual(len(self.http.calls), 64)
        self.assertEqual(len(self.browser_calls), 2)
        self.assertEqual(self.session.closed, 1)

    def test_invalid_url_and_exhausted_budget_do_not_call_any_extractor(self):
        self.assertIsNone(self.fetch(url="https://example.com/video/" + ITEM_ID)[0])
        value, diagnostics = self.fetch(budget=.1)
        self.assertIsNone(value)
        self.assertEqual(diagnostics["error_code"], "budget_exhausted")
        self.assertEqual(self.browser_calls, [])
        self.assertEqual(self.http.calls, [])

    def test_http_exception_falls_back_without_exposing_exception_text(self):
        self.prime_guest()
        self.http.responses.append(RuntimeError("private-cookie=secret"))
        value, diagnostics = self.fetch()
        self.assertEqual(value["item_id"], ITEM_ID)
        self.assertEqual(len(self.browser_calls), 2)
        self.assertEqual(diagnostics["http_error_code"], "http_request_failed")
        self.assertNotIn("secret", str(diagnostics))

    def test_warm_http_wrong_identity_is_rejected_before_returning(self):
        self.prime_guest()
        self.http.responses.append({"result": result("http_structured", item_id=OTHER_ID,
            _link="https://www.douyin.com/video/" + OTHER_ID + "/", author="其他作品作者"), "error_code": ""})
        value, _ = self.fetch()
        self.assertEqual(value["item_id"], ITEM_ID)
        self.assertNotEqual(value.get("author"), "其他作品作者")
        self.assertEqual(len(self.browser_calls), 2)

    def test_dom_enrichment_never_replaces_target_with_other_item(self):
        self.browser_results.append(result("browser_dom"))
        self.http.responses.append({"result": result("http_structured", item_id=OTHER_ID,
            _link="https://www.douyin.com/video/" + OTHER_ID + "/", title_status="complete",
            author="其他作品作者", title="其他作品的更长完整文案"), "error_code": ""})
        value, _ = self.fetch()
        self.assertEqual(value["item_id"], ITEM_ID)
        self.assertEqual(value["title"], "完整作品文案")
        self.assertNotEqual(value.get("author"), "其他作品作者")


if __name__ == "__main__":
    unittest.main()
