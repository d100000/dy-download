import asyncio
import importlib.util
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
from urllib import error as urlerr
from urllib import parse as urlparse

from fastapi.testclient import TestClient
from starlette.requests import Request


_TEST_DATA = tempfile.TemporaryDirectory(prefix="douyin-security-tests-")
os.environ["DATA_DIR"] = _TEST_DATA.name
os.environ["APP_SECRET"] = "test-only-app-secret-" + "x" * 48
os.environ["ADMIN_PASSWORD"] = "test-only-admin-password"
os.environ["MIHOMO_OFF"] = "1"

import server  # noqa: E402  (环境变量必须在导入服务前设置)


def make_request(path="/", headers=None, client_ip="203.0.113.10",
                 query_string=""):
    raw_headers = [
        (str(k).lower().encode("latin-1"), str(v).encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string.encode(),
        "headers": raw_headers,
        "client": (client_ip, 12345),
        "server": ("testserver", 80),
    })


def clear_billing():
    with server._db_lock:
        conn = server._db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for table in ("api_logs", "api_ledger", "job_items", "jobs", "api_keys"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute(
                "INSERT INTO app_settings(k,v) VALUES('api_price_cents','1') "
                "ON CONFLICT(k) DO UPDATE SET v='1'")
            conn.commit()
        finally:
            conn.close()


class StaticRegressionTests(unittest.TestCase):
    def test_homepage_has_api_tab_and_no_hardware_fingerprint(self):
        html = Path("static/index.html").read_text("utf-8")
        # 登录后用户菜单里必须有 API 控制台入口（现为用户邮箱下拉菜单内）
        self.assertIn('id="userDrop"', html)
        self.assertIn('href="/api-console"', html)
        self.assertIn("const ANON_ID_KEY = 'dyanon'", html)
        self.assertIn("localStorage.removeItem('dyfp')", html)
        self.assertNotIn("navigator.hardwareConcurrency", html)
        self.assertNotIn("navigator.deviceMemory", html)
        self.assertNotIn("function computeFP", html)
        self.assertNotIn("function saveHistory", html)
        self.assertIn("localStorage.removeItem('dyhistory')", html)
        self.assertIn("sessionStorage.setItem(key", html)

    def test_public_copy_no_longer_makes_zero_collection_claim(self):
        corpus = "\n".join(
            Path(path).read_text("utf-8")
            for path in ("static/index.html", "static/share.html", "README.md")
        ).lower()
        for forbidden in ("零隐私采集", "zero privacy", "collects nothing",
                          "no collection, nothing uploaded"):
            self.assertNotIn(forbidden, corpus)

    def test_oss_video_proxy_has_alternate_route_and_strict_media_type(self):
        source = Path("oss/server.py").read_text("utf-8")
        self.assertIn("aweme.snssdk.com/aweme/v1/play/", source)
        self.assertIn("www.iesdouyin.com/aweme/v1/play/", source)
        self.assertIn("if not content_type or not (", source)
        self.assertIn("ratio=1080p", source)
        self.assertNotIn("ratio=720p", source)
        self.assertIn("class _ResumableVideoStream", source)
        self.assertIn("_video_response_shape(", source)
        self.assertIn("validator=validate", source)
        self.assertIn('"Accept-Encoding": "identity"', source)
        self.assertIn("ratio=1080p", server._play_api("video_id_12345"))
        self.assertIn("ratio=1080p", server._play_api_alt("video_id_12345"))
        self.assertEqual(server.CDN_HEADERS["Accept-Encoding"], "identity")

    def test_api_responses_are_not_cacheable(self):
        with TestClient(server.app) as client:
            response = client.get("/api/keys")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("cache-control"), "private, no-store")
        self.assertEqual(response.headers.get("pragma"), "no-cache")

    def test_inline_script_pages_are_not_cacheable(self):
        home = server.index(make_request("/"))
        self.assertEqual(home.headers.get("cache-control"), "private, no-store")
        self.assertEqual(home.headers.get("pragma"), "no-cache")

        share = server.share_page(
            "missing-share",
            make_request("/s/missing-share", headers={
                "User-Agent": "Mozilla/5.0 MicroMessenger/8.0",
            }))
        self.assertEqual(share.status_code, 404)
        self.assertEqual(share.headers.get("cache-control"), "private, no-store")
        self.assertEqual(share.headers.get("pragma"), "no-cache")
        self.assertEqual(share.headers.get("vary"), "User-Agent")


class AsyncShareTests(unittest.TestCase):
    def setUp(self):
        server._share_hits.clear()
        with server._db_lock:
            conn = server._db()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM shares")
                conn.execute("DELETE FROM share_submissions")
                conn.execute("DELETE FROM blocked_share_items")
                conn.execute("DELETE FROM blocked_share_sources")
                conn.execute("DELETE FROM quota_reservations")
                conn.execute("DELETE FROM usage_daily")
                conn.execute("DELETE FROM app_settings WHERE k='share_primary_domain'")
                conn.commit()
            finally:
                conn.close()
        self.request = make_request(
            "/api/shares", client_ip="203.0.113.91",
            headers={"Idempotency-Key": "share-test-001"})

    def _create(self, text="https://v.douyin.com/Abc_123-/", title=""):
        with mock.patch.object(server, "_wake_share_parse_workers"):
            return server.api_share_create_async(
                server.AsyncShareBody(text=text, title=title), self.request)

    def test_create_returns_before_network_and_is_idempotent(self):
        with mock.patch.object(
                server, "_parse_cached",
                side_effect=AssertionError("request path must not parse")):
            first = self._create("3.87 复制 https://v.douyin.com/Abc_123-/ 打开抖音")
        self.assertEqual(first.status_code, 202)
        payload = json.loads(first.body)["data"]
        self.assertEqual(payload["status"], "pending")
        self.assertFalse(payload["ready"])
        self.assertTrue(payload["share_url"].endswith("/s/" + payload["sid"]))
        self.assertEqual(first.headers["location"], "/s/" + payload["sid"])
        self.assertTrue(payload["manage_token"].startswith("sm1_"))

        row = dict(server.db_exec(
            "SELECT * FROM shares WHERE id=?", (payload["sid"],), "one"))
        self.assertEqual(row["parse_status"], "pending")
        self.assertEqual(row["source_url"], "https://v.douyin.com/Abc_123-/")
        reservation = server.db_exec(
            "SELECT status FROM quota_reservations WHERE id=?",
            (row["quota_reservation_id"],), "one")
        self.assertEqual(reservation["status"], "pending")

        replay = self._create("https://v.douyin.com/Abc_123-/")
        replay_body = json.loads(replay.body)
        self.assertTrue(replay_body["replayed"])
        self.assertEqual(replay_body["data"]["sid"], payload["sid"])
        self.assertEqual(server.db_exec(
            "SELECT COUNT(*) FROM shares", (), "one")[0], 1)
        self.assertEqual(server.db_exec(
            "SELECT COUNT(*) FROM quota_reservations", (), "one")[0], 1)
        self.assertEqual(server.db_exec(
            "SELECT COUNT(*) FROM share_submissions", (), "one")[0], 1)

        with self.assertRaises(server.ApiError) as conflict:
            self._create("https://v.douyin.com/Different9/")
        self.assertEqual(conflict.exception.status, 409)

    def test_concurrent_idempotent_replays_create_one_share_and_reservation(self):
        def submit(_):
            request = make_request(
                "/api/shares", client_ip="203.0.113.92",
                headers={"Idempotency-Key": "same-concurrent-request"})
            return server.api_share_create_async(
                server.AsyncShareBody(
                    text="https://v.douyin.com/Concurrent9/"), request)

        with mock.patch.object(server, "_wake_share_parse_workers"), \
                ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(submit, range(12)))
        sids = {json.loads(response.body)["data"]["sid"] for response in responses}
        self.assertEqual(len(sids), 1)
        self.assertEqual(server.db_exec(
            "SELECT COUNT(*) FROM shares", (), "one")[0], 1)
        self.assertEqual(server.db_exec(
            "SELECT COUNT(*) FROM quota_reservations", (), "one")[0], 1)

    def test_strict_link_validation_is_local_and_rejects_ambiguous_input(self):
        valid = server._normalize_share_short_link(
            "分享文案 https://v.douyin.com/a-B_9/ 复制打开抖音")
        self.assertEqual(valid, "https://v.douyin.com/a-B_9/")
        for value in (
                "http://v.douyin.com/a-B_9/",
                "https://v.douyin.com/a-B_9/?x=1",
                "https://user@v.douyin.com/a-B_9/",
                "https://v.douyin.com/a-B_9/ https://v.douyin.com/Other9/",
                "https://example.com/a-B_9/",
                "x" * 4097):
            with self.assertRaises(server.ApiError, msg=value[:80]):
                server._normalize_share_short_link(value)

    def test_worker_success_atomically_publishes_share_and_settles_quota(self):
        created = json.loads(self._create().body)["data"]
        item = server._claim_share_parse("test-worker:1")
        self.assertEqual(item["id"], created["sid"])
        parsed = {
            "kind": "video", "item_id": "7654321098765432100",
            "title": "异步视频", "author": "测试作者",
            "avatar": "", "cover": "https://p3.douyinpic.com/cover.jpeg",
            "video": {
                "url": "https://www.iesdouyin.com/aweme/v1/play/?video_id=vid_async_1",
                "filename": "async.mp4", "width": 1920, "height": 1080,
            },
        }
        with mock.patch.object(server, "_parse_cached", return_value=parsed), \
                mock.patch.object(server, "_atc_enqueue"):
            server._run_claimed_share_parse(item)

        row = dict(server.db_exec(
            "SELECT * FROM shares WHERE id=?", (created["sid"],), "one"))
        self.assertEqual(row["parse_status"], "ready")
        self.assertEqual(row["item_id"], parsed["item_id"])
        self.assertEqual(row["vid"], "vid_async_1")
        self.assertIsNone(row["source_url"])
        reservation = server.db_exec(
            "SELECT status,committed_units FROM quota_reservations WHERE id=?",
            (row["quota_reservation_id"],), "one")
        self.assertEqual((reservation["status"], reservation["committed_units"]),
                         ("settled", 1))
        status = server.api_share_status(created["sid"], self.request)["data"]
        self.assertEqual(status["status"], "ready")
        self.assertTrue(status["shareable"])
        self.assertNotIn("video", status)

    def test_expired_lease_is_taken_over_and_old_worker_cannot_publish(self):
        created = json.loads(self._create().body)["data"]
        old = server._claim_share_parse("old-worker")
        server.db_exec(
            "UPDATE shares SET lease_until=? WHERE id=?",
            (int(time.time()) - 1, created["sid"]))
        new = server._claim_share_parse("new-worker")
        self.assertEqual(new["id"], created["sid"])
        parsed = {
            "kind": "video", "item_id": "7000000000000000001",
            "title": "lease", "author": "tester", "avatar": "", "cover": "",
            "video": {"url": "https://www.iesdouyin.com/aweme/v1/play/"
                              "?video_id=lease_vid"},
        }
        self.assertFalse(server._finish_share_parse_success(old, parsed))
        self.assertTrue(server._finish_share_parse_success(new, parsed))
        row = server.db_exec(
            "SELECT parse_status,vid FROM shares WHERE id=?",
            (created["sid"],), "one")
        self.assertEqual((row["parse_status"], row["vid"]),
                         ("ready", "lease_vid"))
        reservation = server.db_exec(
            "SELECT status,committed_units FROM quota_reservations", (), "one")
        self.assertEqual((reservation["status"], reservation["committed_units"]),
                         ("settled", 1))

    def test_permanent_failure_is_publicly_redacted_and_refunded(self):
        created = json.loads(self._create().body)["data"]
        item = server._claim_share_parse("test-worker:2")
        with mock.patch.object(
                server, "_parse_cached",
                side_effect=server.ApiError(404, "secret proxy user:pass@host")):
            server._run_claimed_share_parse(item)
        row = dict(server.db_exec(
            "SELECT * FROM shares WHERE id=?", (created["sid"],), "one"))
        self.assertEqual(row["parse_status"], "failed")
        self.assertEqual(row["parse_error_code"], "content_unavailable")
        self.assertIsNone(row["source_url"])
        reservation = server.db_exec(
            "SELECT status FROM quota_reservations WHERE id=?",
            (row["quota_reservation_id"],), "one")
        self.assertEqual(reservation["status"], "refunded")
        public = server.api_share_status(created["sid"], self.request)["data"]
        self.assertEqual(public["status"], "failed")
        self.assertNotIn("secret", json.dumps(public))
        self.assertNotIn("user:pass", json.dumps(public))

    def test_pending_share_page_does_not_synchronously_refresh(self):
        created = json.loads(self._create().body)["data"]
        with mock.patch.object(
                server, "_refresh_share",
                side_effect=AssertionError("pending page must not parse")):
            response = server.share_page(
                created["sid"], make_request(f"/s/{created['sid']}"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("视频正在准备中", response.body.decode())
        self.assertIn("pollShareStatus", response.body.decode())

    def test_anonymous_manage_token_deletes_without_leaking_from_status(self):
        response = self._create()
        created = json.loads(response.body)["data"]
        public = server.api_share_status(created["sid"], self.request)["data"]
        self.assertNotIn("manage_token", public)
        with self.assertRaises(server.ApiError) as denied:
            server.api_share_delete(created["sid"], make_request(
                f"/api/shares/{created['sid']}"))
        self.assertEqual(denied.exception.status, 403)

        delete_request = make_request(
            f"/api/shares/{created['sid']}",
            headers={"X-Share-Manage-Token": created["manage_token"]})
        self.assertTrue(server.api_share_delete(
            created["sid"], delete_request)["ok"])
        self.assertIsNone(server.db_exec(
            "SELECT 1 FROM shares WHERE id=?", (created["sid"],), "one"))
        reservation = server.db_exec(
            "SELECT status FROM quota_reservations", (), "one")
        self.assertEqual(reservation["status"], "refunded")
        # 限频事实不因用户删除而消失。
        self.assertEqual(server.db_exec(
            "SELECT COUNT(*) FROM share_submissions", (), "one")[0], 1)

    def test_deleting_claimed_job_charges_once_and_old_worker_loses_cas(self):
        created = json.loads(self._create().body)["data"]
        item = server._claim_share_parse("delete-race-worker")
        delete_request = make_request(
            f"/api/shares/{created['sid']}",
            headers={"X-Share-Manage-Token": created["manage_token"]})
        server.api_share_delete(created["sid"], delete_request)
        reservation = server.db_exec(
            "SELECT status,committed_units FROM quota_reservations", (), "one")
        self.assertEqual((reservation["status"], reservation["committed_units"]),
                         ("settled", 1))
        parsed = {
            "kind": "video", "item_id": "7000000000000000901",
            "video": {"url": "https://www.iesdouyin.com/aweme/v1/play/"
                              "?video_id=deleted_vid"},
        }
        self.assertFalse(server._finish_share_parse_success(item, parsed))

    def test_submission_limit_survives_delete_and_ignores_spoofed_fp(self):
        old_limit = server.SHARE_MAX_PER_HOUR
        server.SHARE_MAX_PER_HOUR = 2
        try:
            for i in range(2):
                req = make_request(
                    "/api/shares", client_ip="203.0.113.95",
                    headers={"Idempotency-Key": f"rate-{i}", "X-FP": f"fake-{i}"})
                with mock.patch.object(server, "_wake_share_parse_workers"):
                    response = server.api_share_create_async(
                        server.AsyncShareBody(
                            text=f"https://v.douyin.com/Rate{i}Ab/"), req)
                data = json.loads(response.body)["data"]
                server.api_share_delete(data["sid"], make_request(
                    f"/api/shares/{data['sid']}", client_ip="203.0.113.95",
                    headers={"X-Share-Manage-Token": data["manage_token"]}))
            req = make_request(
                "/api/shares", client_ip="203.0.113.95",
                headers={"Idempotency-Key": "rate-2", "X-FP": "rotated-again"})
            with self.assertRaises(server.ApiError) as limited, \
                    mock.patch.object(server, "_wake_share_parse_workers"):
                server.api_share_create_async(
                    server.AsyncShareBody(
                        text="https://v.douyin.com/Rate2Ab/"), req)
            self.assertEqual(limited.exception.status, 429)
        finally:
            server.SHARE_MAX_PER_HOUR = old_limit

    def test_watchdog_expires_processing_job_even_with_live_lease(self):
        created = json.loads(self._create().body)["data"]
        item = server._claim_share_parse(server._share_parse_instance + ":blocked")
        server.db_exec(
            "UPDATE shares SET created=?,lease_until=? WHERE id=?",
            (int(time.time()) - server.SHARE_PARSE_DEADLINE_SECONDS - 5,
             int(time.time()) + 3600, created["sid"]))
        server._share_parse_heartbeat_once()
        row = server.db_exec(
            "SELECT parse_status,source_url FROM shares WHERE id=?",
            (created["sid"],), "one")
        self.assertEqual(row["parse_status"], "failed")
        self.assertIsNone(row["source_url"])
        reservation = server.db_exec(
            "SELECT status FROM quota_reservations", (), "one")
        self.assertEqual(reservation["status"], "refunded")
        parsed = {
            "kind": "video", "item_id": "7000000000000000902",
            "video": {"url": "https://www.iesdouyin.com/aweme/v1/play/"
                              "?video_id=late_vid"},
        }
        self.assertFalse(server._finish_share_parse_success(item, parsed))

    def test_blocked_item_never_publishes_and_source_is_learned(self):
        now = int(time.time())
        server.db_exec(
            "INSERT INTO blocked_share_items(kind,item_id,created) VALUES(?,?,?)",
            ("video", "7000000000000000903", now))
        created = json.loads(self._create().body)["data"]
        item = server._claim_share_parse("blocked-item-worker")
        parsed = {
            "kind": "video", "item_id": "7000000000000000903",
            "video": {"url": "https://www.iesdouyin.com/aweme/v1/play/"
                              "?video_id=blocked_vid"},
        }
        self.assertFalse(server._finish_share_parse_success(item, parsed))
        row = server.db_exec(
            "SELECT status,parse_status,source_hash FROM shares WHERE id=?",
            (created["sid"],), "one")
        self.assertEqual((row["status"], row["parse_status"]),
                         ("takedown", "failed"))
        self.assertTrue(server.db_exec(
            "SELECT 1 FROM blocked_share_sources WHERE source_hash=?",
            (row["source_hash"],), "one"))
        reservation = server.db_exec(
            "SELECT status FROM quota_reservations", (), "one")
        self.assertEqual(reservation["status"], "refunded")

        retry_request = make_request(
            "/api/shares", headers={"Idempotency-Key": "blocked-source-retry"})
        with self.assertRaises(server.ApiError) as blocked:
            server.api_share_create_async(server.AsyncShareBody(
                text="https://v.douyin.com/Abc_123-/"), retry_request)
        self.assertEqual(blocked.exception.status, 451)

    def test_takedown_during_refresh_cannot_revive_and_refunds_settled_quota(self):
        created = json.loads(self._create().body)["data"]
        item = server._claim_share_parse("refresh-race-worker")
        parsed = {
            "kind": "video", "item_id": "7000000000000000904",
            "title": "before takedown", "author": "tester",
            "video": {"url": "https://www.iesdouyin.com/aweme/v1/play/"
                              "?video_id=refresh_vid"},
        }
        self.assertTrue(server._finish_share_parse_success(item, parsed))
        stale = dict(server.db_exec(
            "SELECT * FROM shares WHERE id=?", (created["sid"],), "one"))

        def takedown_then_return(*_args):
            with mock.patch.object(server, "_require_admin"):
                server.admin_takedown(created["sid"], make_request("/api/admin"))
            return {**parsed, "title": "must not revive"}

        with mock.patch.object(server, "_parse_item",
                               side_effect=takedown_then_return):
            latest = server._refresh_share(stale)
        self.assertEqual(latest["status"], "takedown")
        persisted = server.db_exec(
            "SELECT status,title FROM shares WHERE id=?", (created["sid"],), "one")
        self.assertEqual(persisted["status"], "takedown")
        self.assertEqual(persisted["title"], "before takedown")
        reservation = server.db_exec(
            "SELECT status,committed_units FROM quota_reservations", (), "one")
        self.assertEqual((reservation["status"], reservation["committed_units"]),
                         ("refunded", 0))
        self.assertEqual(server.db_exec(
            "SELECT COALESCE(MAX(count),0) FROM usage_daily", (), "one")[0], 0)

    def test_sync_share_atomic_block_check_refunds_racing_reservation(self):
        parsed = {
            "kind": "video", "item_id": "7000000000000000905",
            "title": "racing sync share", "author": "tester",
            "video": {"url": "https://www.iesdouyin.com/aweme/v1/play/"
                              "?video_id=sync_block_vid"},
        }

        def block_after_early_check(_data):
            server.db_exec(
                "INSERT INTO blocked_share_items(kind,item_id,created) VALUES(?,?,?)",
                ("video", parsed["item_id"], int(time.time())))

        with mock.patch.object(server, "_parse_cached", return_value=parsed), \
                mock.patch.object(server, "_require_share_item_allowed",
                                  side_effect=block_after_early_check):
            with self.assertRaises(server.ApiError) as blocked:
                server.api_share_create(
                    server.ShareBody(text="https://v.douyin.com/SyncRace9/"),
                    make_request("/api/share", client_ip="203.0.113.97"))
        self.assertEqual(blocked.exception.status, 451)
        self.assertEqual(server.db_exec(
            "SELECT COUNT(*) FROM shares", (), "one")[0], 0)
        reservation = server.db_exec(
            "SELECT status FROM quota_reservations", (), "one")
        self.assertEqual(reservation["status"], "refunded")

    def test_body_limit_handles_declared_and_chunked_payloads_before_app(self):
        async def exercise(chunks, declared=None, limit=8):
            downstream_called = False
            receive_calls = 0
            sent = []

            async def downstream(_scope, receive, send):
                nonlocal downstream_called
                downstream_called = True
                while True:
                    message = await receive()
                    if not message.get("more_body", False):
                        break
                await send({"type": "http.response.start", "status": 204,
                            "headers": []})
                await send({"type": "http.response.body", "body": b""})

            middleware = server._AsyncShareBodyLimitMiddleware(downstream, limit)
            messages = [
                {"type": "http.request", "body": chunk,
                 "more_body": i < len(chunks) - 1}
                for i, chunk in enumerate(chunks)
            ]

            async def receive():
                nonlocal receive_calls
                receive_calls += 1
                return messages.pop(0)

            async def send(message):
                sent.append(message)

            headers = [] if declared is None else [
                (b"content-length", str(declared).encode())]
            await middleware({"type": "http", "method": "POST",
                              "path": "/api/shares", "headers": headers},
                             receive, send)
            status = next(m["status"] for m in sent
                          if m["type"] == "http.response.start")
            return status, downstream_called, receive_calls

        self.assertEqual(asyncio.run(exercise([b"123456789"], 9)),
                         (413, False, 0))
        status, called, calls = asyncio.run(exercise([b"12345", b"67890"], None))
        self.assertEqual((status, called, calls), (413, False, 2))
        status, called, _ = asyncio.run(exercise([b"1234", b"5678"], 1))
        self.assertEqual((status, called), (204, True))

    def test_origin_ignores_untrusted_host_and_forwarded_headers(self):
        request = make_request(headers={
            "Host": "evil.example", "X-Forwarded-Host": "attacker.example",
            "X-Forwarded-Proto": "javascript:alert(1)//",
        })
        self.assertEqual(server._origin(request), "http://testserver")
        for page in ("http://testserver.evil.example/s/x",
                     "http://testserver@evil.example/s/x"):
            with self.assertRaises(server.ApiError) as rejected:
                server.wx_jssdk(request, url=page)
            self.assertEqual(rejected.exception.status, 403)
        with mock.patch.object(server, "_require_admin"):
            with self.assertRaises(server.ApiError) as invalid:
                server.admin_set_share_config(
                    server.ShareConfigBody(
                        primary_domain="https://user:pass@example.com/path"), request)
        self.assertEqual(invalid.exception.status, 422)


class MediaSecurityTests(unittest.TestCase):
    def setUp(self):
        with server._media_limit_lock:
            server._media_hits.clear()
            server._media_active.clear()

    def test_signed_video_token_rejects_tamper_and_expiry(self):
        vid = "video_id_12345"
        exp, sig = server._media_token("video", vid, 300)
        server._require_media_token("video", vid, exp, sig)
        with self.assertRaises(server.ApiError) as bad_resource:
            server._require_media_token("video", vid + "x", exp, sig)
        self.assertEqual(bad_resource.exception.status, 403)
        with self.assertRaises(server.ApiError):
            server._require_media_token(
                "video", vid, int(time.time()) - 1,
                server._media_signature("video", vid, int(time.time()) - 1))
        with self.assertRaises(server.ApiError) as non_ascii:
            server._require_media_token("video", vid, exp, "é")
        self.assertEqual(non_ascii.exception.status, 403)

        signed = server._video_download_url(vid, "测试.mp4")
        query = urlparse.parse_qs(urlparse.urlsplit(signed).query)
        self.assertIn("sig", query)
        self.assertEqual(query["dl"], ["1"])
        self.assertEqual(query["name"], ["测试.mp4"])

    def test_media_route_requires_capability_and_generic_proxy_is_removed(self):
        req = make_request("/api/video/video_id_12345")
        with self.assertRaises(server.ApiError) as missing:
            server.api_video("video_id_12345", req)
        self.assertEqual(missing.exception.status, 403)
        self.assertNotIn("/api/media", {r.path for r in server.app.routes})

    def test_range_validation_and_idempotent_stream_lease_release(self):
        for valid in ("", "bytes=0-1", "bytes=10-", "bytes=-10"):
            self.assertTrue(server._valid_single_range(valid), valid)
        for invalid in ("bytes=", "items=0-1", "bytes=2-1", "bytes=-0",
                        "bytes=0-1,3-4"):
            self.assertFalse(server._valid_single_range(invalid), invalid)

        old_max = server.MEDIA_MAX_CONCURRENT
        server.MEDIA_MAX_CONCURRENT = 1
        try:
            req = make_request(client_ip="203.0.113.20")
            lease = server._media_lease(req)
            with self.assertRaises(server.ApiError) as limited:
                server._media_lease(req)
            self.assertEqual(limited.exception.status, 429)
            self.assertEqual(limited.exception.headers.get("Retry-After"), "2")
            server._media_release(lease)
            server._media_release(lease)
            self.assertNotIn("203.0.113.20", server._media_active)
        finally:
            server.MEDIA_MAX_CONCURRENT = old_max

    def test_proxy_credentials_are_redacted(self):
        old = server.proxy_mgr.proxies
        server.proxy_mgr.proxies = [{
            "url": "socks5://alice:very-secret@proxy.example:1080",
        }]
        try:
            text = server._redact_proxy_error(
                "connect socks5://alice:very-secret@proxy.example:1080 failed")
            self.assertNotIn("alice", text)
            self.assertNotIn("very-secret", text)
            self.assertIn("proxy.example", text)
        finally:
            server.proxy_mgr.proxies = old

    def test_valid_range_stream_preserves_headers_and_releases_lease(self):
        class FakeResponse:
            status = 206
            headers = {
                "Content-Length": "5",
                "Content-Range": "bytes 0-4/10",
                "Content-Type": "video/mp4",
            }

            def __init__(self):
                self.blocks = [b"hello", b""]
                self.closed = False

            def read(self, _):
                return self.blocks.pop(0)

            def close(self):
                self.closed = True

        fake = FakeResponse()
        original_open = server.open_url
        server.open_url = lambda *args, **kwargs: (fake, None)
        try:
            vid = "video_id_12345"
            exp, sig = server._media_token("video", vid, 300)
            request = make_request(
                "/api/video/" + vid,
                headers={"Range": "bytes=0-4"},
                client_ip="203.0.113.21")
            response = server.api_video(
                vid, request, exp=exp, sig=sig, dl="1", name="测试.mp4")

            async def consume():
                chunks = []
                async for block in response.body_iterator:
                    chunks.append(block)
                return b"".join(chunks)

            self.assertEqual(asyncio.run(consume()), b"hello")
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.headers["content-range"], "bytes 0-4/10")
            self.assertIn("attachment", response.headers["content-disposition"])
            self.assertTrue(fake.closed)
            self.assertNotIn("203.0.113.21", server._media_active)
        finally:
            server.open_url = original_open

    def test_full_video_resumes_from_exact_offset_after_early_eof(self):
        class FakeResponse:
            def __init__(self, status, headers, blocks):
                self.status = status
                self.headers = {
                    "Content-Type": "video/mp4",
                    **headers,
                }
                self.blocks = list(blocks)
                self.closed = False

            def read(self, _size):
                return self.blocks.pop(0) if self.blocks else b""

            def close(self):
                self.closed = True

        initial = FakeResponse(
            200, {"Content-Length": "10"}, [b"abc", b""])
        resumed = FakeResponse(
            206,
            {
                "Content-Length": "7",
                "Content-Range": "bytes 3-9/10",
            },
            [b"defghij"])
        responses = [initial, resumed]
        ranges = []

        def fake_open(_url, **kwargs):
            ranges.append((kwargs.get("headers") or {}).get("Range"))
            return responses.pop(0), None

        vid = "video_id_12345"
        exp, sig = server._media_token("video", vid, 300)
        request = make_request(
            "/api/video/" + vid, client_ip="203.0.113.23")
        with mock.patch.object(server, "open_url", side_effect=fake_open):
            response = server.api_video(vid, request, exp=exp, sig=sig)

            async def consume():
                chunks = []
                async for block in response.body_iterator:
                    chunks.append(block)
                return b"".join(chunks)

            body = asyncio.run(consume())

        self.assertEqual(body, b"abcdefghij")
        self.assertEqual(ranges, [None, "bytes=3-9"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-length"], "10")
        self.assertNotIn("content-range", response.headers)
        self.assertTrue(initial.closed)
        self.assertTrue(resumed.closed)
        self.assertNotIn("203.0.113.23", server._media_active)

    def test_range_video_preserves_partial_exception_bytes_then_resumes(self):
        class PartialReadError(OSError):
            def __init__(self, partial):
                super().__init__("upstream reset")
                self.partial = partial

        class FakeResponse:
            def __init__(self, headers, reads):
                self.status = 206
                self.headers = {
                    "Content-Type": "video/mp4",
                    **headers,
                }
                self.reads = list(reads)
                self.closed = False

            def read(self, _size):
                value = self.reads.pop(0) if self.reads else b""
                if isinstance(value, Exception):
                    raise value
                return value

            def close(self):
                self.closed = True

        initial = FakeResponse(
            {
                "Content-Length": "5",
                "Content-Range": "bytes 5-9/20",
            },
            [PartialReadError(b"67")])
        resumed = FakeResponse(
            {
                "Content-Length": "3",
                "Content-Range": "bytes 7-9/20",
            },
            [b"890"])
        responses = [initial, resumed]
        ranges = []

        def fake_open(_url, **kwargs):
            ranges.append((kwargs.get("headers") or {}).get("Range"))
            return responses.pop(0), None

        vid = "video_id_12345"
        exp, sig = server._media_token("video", vid, 300)
        request = make_request(
            "/api/video/" + vid,
            headers={"Range": "bytes=5-9"},
            client_ip="203.0.113.24")
        with mock.patch.object(server, "open_url", side_effect=fake_open):
            response = server.api_video(vid, request, exp=exp, sig=sig)

            async def consume():
                chunks = []
                async for block in response.body_iterator:
                    chunks.append(block)
                return b"".join(chunks)

            body = asyncio.run(consume())

        self.assertEqual(body, b"67890")
        self.assertEqual(ranges, ["bytes=5-9", "bytes=7-9"])
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-length"], "5")
        self.assertEqual(response.headers["content-range"], "bytes 5-9/20")
        self.assertTrue(initial.closed)
        self.assertTrue(resumed.closed)
        self.assertNotIn("203.0.113.24", server._media_active)

    def test_main_one_byte_progress_cannot_bypass_total_resume_limit(self):
        class FakeResponse:
            def __init__(self, status, headers):
                self.status = status
                self.headers = {
                    "Content-Type": "video/mp4",
                    **headers,
                }
                self.reads = [b"x", b""]
                self.closed = False

            def read(self, _size):
                return self.reads.pop(0) if self.reads else b""

            def close(self):
                self.closed = True

        calls = []
        responses = []

        def fake_open(_url, **kwargs):
            range_header = (kwargs.get("headers") or {}).get("Range")
            calls.append(range_header)
            if range_header:
                start = int(range_header.split("=", 1)[1].split("-", 1)[0])
                response = FakeResponse(
                    206,
                    {
                        "Content-Length": str(100 - start),
                        "Content-Range": f"bytes {start}-99/100",
                    })
            else:
                response = FakeResponse(200, {"Content-Length": "100"})
            responses.append(response)
            return response, None

        vid = "video_id_12345"
        exp, sig = server._media_token("video", vid, 300)
        request = make_request(
            "/api/video/" + vid, client_ip="203.0.113.26")
        with mock.patch.object(server, "open_url", side_effect=fake_open), \
             mock.patch.object(
                 server._ResumableVideoStream,
                 "_MAX_TOTAL_RESUME_ATTEMPTS", 3), \
             mock.patch.object(
                 server._ResumableVideoStream,
                 "_MAX_CONSECUTIVE_RESUME_FAILURES", 12):
            response = server.api_video(vid, request, exp=exp, sig=sig)

            async def consume():
                chunks = []
                async for block in response.body_iterator:
                    chunks.append(block)
                return chunks

            with self.assertRaisesRegex(OSError, "resume budget exhausted"):
                asyncio.run(consume())

        self.assertEqual(
            calls, [None, "bytes=1-99", "bytes=2-99", "bytes=3-99"])
        self.assertEqual(len(responses), 4)
        self.assertTrue(all(response.closed for response in responses))
        self.assertNotIn("203.0.113.26", server._media_active)

    def test_oss_full_video_recovers_from_exception_and_repeated_eof(self):
        spec = importlib.util.spec_from_file_location(
            "oss_server_media_test", Path("oss/server.py"))
        oss_server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(oss_server)

        class PartialReadError(OSError):
            def __init__(self, partial):
                super().__init__("upstream reset")
                self.partial = partial

        class FakeResponse:
            def __init__(self, status, headers, reads):
                self.status = status
                self.headers = {
                    "Content-Type": "video/mp4",
                    **headers,
                }
                self.reads = list(reads)
                self.closed = False

            def read(self, _size):
                value = self.reads.pop(0) if self.reads else b""
                if isinstance(value, Exception):
                    raise value
                return value

            def close(self):
                self.closed = True

        initial = FakeResponse(
            200, {"Content-Length": "10"},
            [PartialReadError(b"ab")])
        first_resume = FakeResponse(
            206,
            {
                "Content-Length": "8",
                "Content-Range": "bytes 2-9/10",
            },
            [b"cde", b""])
        second_resume = FakeResponse(
            206,
            {
                "Content-Length": "5",
                "Content-Range": "bytes 5-9/10",
            },
            [b"fghij"])
        responses = [initial, first_resume, second_resume]
        ranges = []

        def fake_open(_url, follow=True, headers=None):
            del follow
            ranges.append((headers or {}).get("Range"))
            return responses.pop(0)

        vid = "video_id_12345"
        expiry = int(time.time()) + 300
        signature = oss_server._video_signature(vid, expiry)
        request = make_request(
            "/api/video/" + vid, client_ip="203.0.113.25")
        with mock.patch.object(
                oss_server, "_open", side_effect=fake_open):
            response = oss_server.api_video(
                vid, request, exp=str(expiry), sig=signature)

            async def consume():
                chunks = []
                async for block in response.body_iterator:
                    chunks.append(block)
                return b"".join(chunks)

            body = asyncio.run(consume())

        self.assertEqual(body, b"abcdefghij")
        self.assertEqual(ranges, [None, "bytes=2-9", "bytes=5-9"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-length"], "10")
        self.assertNotIn("content-range", response.headers)
        self.assertTrue(initial.closed)
        self.assertTrue(first_resume.closed)
        self.assertTrue(second_resume.closed)
        self.assertNotIn("203.0.113.25", oss_server._media_active)

    def test_oss_one_byte_progress_cannot_bypass_total_resume_limit(self):
        spec = importlib.util.spec_from_file_location(
            "oss_server_resume_budget_test", Path("oss/server.py"))
        oss_server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(oss_server)

        class FakeResponse:
            def __init__(self, status, headers):
                self.status = status
                self.headers = {
                    "Content-Type": "video/mp4",
                    **headers,
                }
                self.reads = [b"x", b""]
                self.closed = False

            def read(self, _size):
                return self.reads.pop(0) if self.reads else b""

            def close(self):
                self.closed = True

        calls = []
        responses = []

        def fake_open(_url, follow=True, headers=None):
            del follow
            range_header = (headers or {}).get("Range")
            calls.append(range_header)
            if range_header:
                start = int(range_header.split("=", 1)[1].split("-", 1)[0])
                response = FakeResponse(
                    206,
                    {
                        "Content-Length": str(100 - start),
                        "Content-Range": f"bytes {start}-99/100",
                    })
            else:
                response = FakeResponse(200, {"Content-Length": "100"})
            responses.append(response)
            return response

        vid = "video_id_12345"
        expiry = int(time.time()) + 300
        signature = oss_server._video_signature(vid, expiry)
        request = make_request(
            "/api/video/" + vid, client_ip="203.0.113.27")
        with mock.patch.object(
                oss_server, "_open", side_effect=fake_open), \
             mock.patch.object(
                 oss_server._ResumableVideoStream,
                 "_MAX_TOTAL_RESUME_ATTEMPTS", 3), \
             mock.patch.object(
                 oss_server._ResumableVideoStream,
                 "_MAX_CONSECUTIVE_RESUME_FAILURES", 12):
            response = oss_server.api_video(
                vid, request, exp=str(expiry), sig=signature)

            async def consume():
                chunks = []
                async for block in response.body_iterator:
                    chunks.append(block)
                return chunks

            with self.assertRaisesRegex(OSError, "resume budget exhausted"):
                asyncio.run(consume())

        self.assertEqual(
            calls, [None, "bytes=1-99", "bytes=2-99", "bytes=3-99"])
        self.assertEqual(len(responses), 4)
        self.assertTrue(all(response.closed for response in responses))
        self.assertNotIn("203.0.113.27", oss_server._media_active)

    def test_video_upstream_rejects_error_page_and_uses_alternate_domain(self):
        class FakeResponse:
            def __init__(self, content_type, final_url):
                self.status = 200
                self.headers = {"Content-Type": content_type}
                self.final_url = final_url
                self.closed = False

            def geturl(self):
                return self.final_url

            def close(self):
                self.closed = True

        bad = FakeResponse(
            "text/html; charset=utf-8",
            "https://aweme.snssdk.com/aweme/v1/play/")
        good = FakeResponse(
            "video/mp4",
            "https://v26.douyinvod.com/video/tos/cn/example")
        calls = []
        original_open = server.open_url

        def fake_open(url, **_kwargs):
            calls.append((url, _kwargs))
            return (bad if len(calls) == 1 else good), None

        server.open_url = fake_open
        try:
            response = server._open_video_upstream(
                "video_id_12345", {"Range": "bytes=0-4"})
            self.assertIs(response, good)
            self.assertTrue(bad.closed)
            self.assertFalse(good.closed)
            self.assertEqual(len(calls), 2)
            self.assertIn("aweme.snssdk.com", calls[0][0])
            self.assertIn("www.iesdouyin.com", calls[1][0])
            self.assertFalse(calls[0][1]["ban_on_auth_error"])
            self.assertIn(502, calls[0][1]["retry_http_statuses"])
        finally:
            good.close()
            server.open_url = original_open

    def test_video_upstream_failures_return_only_generic_error(self):
        original_open = server.open_url
        server.open_url = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("http://user:secret@proxy.example failed"))
        try:
            with self.assertRaises(server.ApiError) as failed:
                server._open_video_upstream("video_id_12345", {})
            self.assertEqual(failed.exception.status, 502)
            self.assertEqual(
                failed.exception.message,
                "视频下载线路暂时不可用，请稍后重试")
            self.assertNotIn("secret", failed.exception.message)
        finally:
            server.open_url = original_open

    def test_video_upstream_rejects_empty_type_and_untrusted_redirect(self):
        class FakeResponse:
            status = 200

            def __init__(self, content_type, final_url):
                self.headers = {"Content-Type": content_type}
                self.final_url = final_url
                self.closed = False

            def geturl(self):
                return self.final_url

            def close(self):
                self.closed = True

        empty_type = FakeResponse(
            "", "https://aweme.snssdk.com/aweme/v1/play/")
        untrusted = FakeResponse(
            "video/mp4", "https://media.attacker.example/video")
        responses = [empty_type, untrusted]
        original_open = server.open_url
        server.open_url = lambda *_args, **_kwargs: (responses.pop(0), None)
        try:
            with self.assertRaises(server.ApiError) as failed:
                server._open_video_upstream("video_id_12345", {})
            self.assertEqual(failed.exception.status, 502)
            self.assertTrue(empty_type.closed)
            self.assertTrue(untrusted.closed)
        finally:
            server.open_url = original_open

    def test_media_5xx_rotates_proxy_without_mutating_health(self):
        first = {
            "url": "http://proxy-one.example:8000",
            "fail": 4,
            "enabled": True,
        }
        second = {
            "url": "http://proxy-two.example:8000",
            "fail": 0,
            "enabled": True,
        }

        class FakeResponse:
            pass

        response = FakeResponse()
        attempts = []

        def fake_raw_open(url, follow, headers, timeout, proxy):
            attempts.append(proxy)
            if proxy is first:
                raise urlerr.HTTPError(url, 502, "gateway error", {}, None)
            return response

        with mock.patch.object(
                server.proxy_mgr, "candidates",
                return_value=[first, second]), \
             mock.patch.object(
                 server.proxy_mgr, "mark_fail") as mark_fail, \
             mock.patch.object(
                 server.proxy_mgr, "mark_ok") as mark_ok, \
             mock.patch.object(server.proxy_mgr, "note_retry"), \
             mock.patch.object(
                 server, "_raw_open", side_effect=fake_raw_open):
            opened, used = server.open_url(
                "https://aweme.snssdk.com/aweme/v1/play/",
                retry_http_statuses=(502,),
                ban_on_auth_error=False)

        self.assertIs(opened, response)
        self.assertIs(used, second)
        self.assertEqual(attempts, [first, second])
        mark_fail.assert_not_called()
        mark_ok.assert_called_once()
        self.assertEqual(first["fail"], 4)
        self.assertTrue(first["enabled"])

    def test_media_resource_403_rotates_without_banning_proxy(self):
        first = {
            "url": "http://proxy-one.example:8000",
            "fail": 4,
            "enabled": True,
        }
        second = {
            "url": "http://proxy-two.example:8000",
            "fail": 0,
            "enabled": True,
        }

        class FakeResponse:
            pass

        response = FakeResponse()

        def fake_raw_open(url, follow, headers, timeout, proxy):
            if proxy is first:
                raise urlerr.HTTPError(url, 403, "resource forbidden", {}, None)
            return response

        with mock.patch.object(
                server.proxy_mgr, "candidates",
                return_value=[first, second]), \
             mock.patch.object(
                 server.proxy_mgr, "mark_fail") as mark_fail, \
             mock.patch.object(
                 server.proxy_mgr, "mark_banned") as mark_banned, \
             mock.patch.object(server.proxy_mgr, "mark_ok"), \
             mock.patch.object(server.proxy_mgr, "note_retry"), \
             mock.patch.object(
                 server, "_raw_open", side_effect=fake_raw_open):
            opened, used = server.open_url(
                "https://aweme.snssdk.com/aweme/v1/play/",
                retry_http_statuses=(502,),
                ban_on_auth_error=False)

        self.assertIs(opened, response)
        self.assertIs(used, second)
        mark_fail.assert_not_called()
        mark_banned.assert_not_called()
        self.assertEqual(first["fail"], 4)
        self.assertTrue(first["enabled"])

    def test_media_tunnel_403_exception_rotates_without_banning_proxy(self):
        first = {
            "url": "http://proxy-one.example:8000",
            "fail": 4,
            "enabled": True,
        }
        second = {
            "url": "http://proxy-two.example:8000",
            "fail": 0,
            "enabled": True,
        }

        class FakeResponse:
            pass

        response = FakeResponse()

        def fake_raw_open(_url, _follow, _headers, _timeout, proxy):
            if proxy is first:
                raise OSError(
                    "Tunnel connection failed: 403 Forbidden")
            return response

        with mock.patch.object(
                server.proxy_mgr, "candidates",
                return_value=[first, second]), \
             mock.patch.object(
                 server.proxy_mgr, "mark_fail") as mark_fail, \
             mock.patch.object(
                 server.proxy_mgr, "mark_banned") as mark_banned, \
             mock.patch.object(server.proxy_mgr, "mark_ok"), \
             mock.patch.object(server.proxy_mgr, "note_retry"), \
             mock.patch.object(
                 server, "_raw_open", side_effect=fake_raw_open):
            opened, used = server.open_url(
                "https://aweme.snssdk.com/aweme/v1/play/",
                retry_http_statuses=(502,),
                ban_on_auth_error=False)

        self.assertIs(opened, response)
        self.assertIs(used, second)
        mark_fail.assert_not_called()
        mark_banned.assert_not_called()
        self.assertEqual(first["fail"], 4)
        self.assertTrue(first["enabled"])

    def test_refresh_share_repairs_legacy_video_id(self):
        sid = "legacy-missing-video-id"
        vid = "recovered_video_id_12345"
        server.db_exec(
            "INSERT OR REPLACE INTO shares"
            "(id,item_id,kind,vid,payload,status,created) VALUES(?,?,?,?,?,?,?)",
            (sid, "item-123", "video", "", "{}", "ok", int(time.time())))
        row = {
            "id": sid,
            "item_id": "item-123",
            "kind": "video",
            "vid": "",
            "status": "ok",
        }
        parsed = {
            "cover": "https://p3.douyinpic.com/example.jpeg",
            "video": {
                "url": server._play_api(vid),
                "filename": "legacy.mp4",
            },
        }
        try:
            with mock.patch.object(server, "_parse_item", return_value=parsed):
                refreshed = server._refresh_share(row)
            self.assertEqual(refreshed["vid"], vid)
            stored = server.db_exec(
                "SELECT vid,status FROM shares WHERE id=?", (sid,), "one")
            self.assertEqual(stored["vid"], vid)
            self.assertEqual(stored["status"], "ok")
        finally:
            server.db_exec("DELETE FROM shares WHERE id=?", (sid,))

    def test_asgi_header_send_failure_still_releases_lease(self):
        request = make_request(client_ip="203.0.113.22")
        lease = server._media_lease(request)
        class FakeUpstream:
            closed = False

            def close(self):
                self.closed = True

        upstream = FakeUpstream()
        finalize = server._media_finalizer(upstream, lease)
        response = server._MediaStreamingResponse(
            iter([b"unused"]), finalize=finalize, media_type="video/mp4")

        async def receive():
            return {"type": "http.disconnect"}

        async def send(_message):
            raise OSError("client disconnected before response start")

        with self.assertRaises(Exception):
            asyncio.run(response(request.scope, receive, send))
        self.assertTrue(upstream.closed)
        self.assertNotIn("203.0.113.22", server._media_active)


class AtomicWebQuotaTests(unittest.TestCase):
    def setUp(self):
        server.db_exec("DELETE FROM quota_reservations")
        server.db_exec("DELETE FROM usage_daily")

    def test_concurrent_requests_cannot_exceed_daily_limit(self):
        old_limit = server.FREE_ANON_DAILY
        server.FREE_ANON_DAILY = 3
        request = make_request(
            headers={"X-FP": "0123456789abcdef0123456789abcdef"},
            client_ip="198.51.100.40")
        try:
            with ThreadPoolExecutor(max_workers=20) as pool:
                reservations = list(pool.map(
                    lambda _: server.reserve_quota(request, 1, endpoint="test"),
                    range(20)))
            accepted = [r for r in reservations if r["ok"]]
            self.assertEqual(len(accepted), 3)
            self.assertEqual(server.quota_status(request), (3, 3, 0))
            for reservation in accepted:
                server.settle_quota(reservation, 1)
            self.assertEqual(server.quota_status(request), (3, 3, 0))
        finally:
            server.FREE_ANON_DAILY = old_limit

    def test_failed_parse_reservation_is_refunded_once(self):
        request = make_request(
            headers={"X-FP": "fedcba9876543210fedcba9876543210"},
            client_ip="198.51.100.41")
        reservation = server.reserve_quota(request, 1, endpoint="test")
        self.assertTrue(reservation["ok"])
        server.release_quota(reservation)
        server.release_quota(reservation)
        self.assertEqual(server.quota_status(request)[1], 0)

    def test_client_cannot_bypass_hmac_with_hash_shaped_identifier(self):
        chosen = "h:" + "a" * 24
        request = make_request(
            headers={"X-FP": chosen}, client_ip="198.51.100.42")
        stored = server._stored_fp(request, "quota", str(server._today()))
        self.assertRegex(stored, r"^h:[0-9a-f]{24}$")
        self.assertNotEqual(stored, chosen)


class DurableBillingTests(unittest.TestCase):
    def setUp(self):
        clear_billing()

    def _create_job(self, links, idem):
        key = server.create_api_key(None, "test-key")["key"]
        request = make_request(headers={
            "X-API-Key": key,
            "Idempotency-Key": idem,
        })
        result = server.api_v1_create_job(
            server.JobBody(links=links), request)
        return key, request, result["data"]["job_id"]

    def test_api_key_in_query_string_is_rejected(self):
        request = make_request(query_string="key=dy_leaks_into_logs")
        self.assertEqual(server._api_key_from(request), "")
        with self.assertRaises(server.ApiError) as missing:
            server.api_v1_balance(request)
        self.assertEqual(missing.exception.status, 401)
        paths = {route.path for route in server.app.routes}
        self.assertIn("/api/admin/apikeys/revoke", paths)
        self.assertIn("/api/admin/apikeys/recharge", paths)
        self.assertIn("/api/keys/revoke", paths)
        self.assertNotIn("/api/admin/apikeys/{key}", paths)
        self.assertNotIn("/api/admin/apikeys/{key}/recharge", paths)
        self.assertNotIn("/api/keys/{key}", paths)

    def test_revoked_user_key_disappears_and_does_not_count_toward_limit(self):
        key = server.create_api_key(999, "rotated-key")["key"]
        self.assertEqual(len(server.list_api_keys(999)), 1)
        self.assertTrue(server.revoke_api_key(key, 999))
        self.assertEqual(server.list_api_keys(999), [])
        self.assertTrue(any(k["key"] == key for k in server.list_api_keys()))

    def test_idempotent_preauthorization_success_charge_and_failure_refund(self):
        links = [
            "https://v.douyin.com/aaaa1111/",
            "https://v.douyin.com/bbbb2222/",
        ]
        key, request, job_id = self._create_job(links, "idem-one")
        replay = server.api_v1_create_job(
            server.JobBody(links=links), request)
        self.assertEqual(replay["data"]["job_id"], job_id)

        account = server.get_api_key(key)
        self.assertEqual(account["balance_cents"], 98)
        self.assertEqual(account["reserved_cents"], 2)
        self.assertEqual(
            server.db_exec("SELECT COUNT(*) n FROM jobs", (), "one")["n"], 1)

        first = server._claim_job_item("worker:first")
        self.assertTrue(server._finish_job_item(first, True, {"title": "ok"}))
        second = server._claim_job_item("worker:second")
        self.assertTrue(server._finish_job_item(
            second, False, error_message="source unavailable"))

        account = server.get_api_key(key)
        self.assertEqual(account["balance_cents"], 99)
        self.assertEqual(account["reserved_cents"], 0)
        self.assertEqual(account["spent_cents"], 1)
        self.assertEqual(account["calls"], 1)
        job = server.db_exec("SELECT * FROM jobs WHERE id=?", (job_id,), "one")
        self.assertEqual((job["status"], job["done"], job["ok"]), ("done", 2, 1))
        self.assertEqual(
            server.db_exec(
                "SELECT COUNT(*) n FROM api_logs WHERE job_id=?",
                (job_id,), "one")["n"], 2)

    def test_expired_lease_can_only_be_settled_by_new_owner(self):
        key, _, job_id = self._create_job(
            ["https://v.douyin.com/cccc3333/"], "idem-lease")
        old = server._claim_job_item("worker:old")
        server.db_exec(
            "UPDATE job_items SET lease_until=0 WHERE job_id=? AND idx=0",
            (job_id,))
        new = server._claim_job_item("worker:new")
        self.assertIsNotNone(new)
        self.assertFalse(server._finish_job_item(old, True, {"wrong": True}))
        self.assertTrue(server._finish_job_item(new, True, {"right": True}))
        self.assertEqual(
            server.db_exec(
                "SELECT COUNT(*) n FROM api_logs WHERE job_id=?",
                (job_id,), "one")["n"], 1)
        account = server.get_api_key(key)
        self.assertEqual((account["balance_cents"], account["reserved_cents"],
                          account["spent_cents"]), (99, 0, 1))

    def test_insufficient_balance_rejects_entire_job(self):
        key = server.create_api_key(None, "small")["key"]
        server.db_exec(
            "UPDATE api_keys SET balance_cents=1 WHERE key=?", (key,))
        request = make_request(headers={"X-API-Key": key})
        with self.assertRaises(server.ApiError) as insufficient:
            server.api_v1_create_job(server.JobBody(links=[
                "https://v.douyin.com/dddd4444/",
                "https://v.douyin.com/eeee5555/",
            ]), request)
        self.assertEqual(insufficient.exception.status, 402)
        self.assertEqual(
            server.db_exec("SELECT COUNT(*) n FROM jobs", (), "one")["n"], 0)
        self.assertEqual(server.get_api_key(key)["balance_cents"], 1)

    def test_non_daemon_worker_consumes_persisted_item_and_stops(self):
        key, _, job_id = self._create_job(
            ["https://v.douyin.com/ffff6666/"], "idem-worker")
        original_parse = server._parse_cached
        server._parse_cached = lambda link: {"item_id": "ok", "source": link}
        try:
            server._start_api_job_workers()
            deadline = time.time() + 3
            while time.time() < deadline:
                job = server.db_exec(
                    "SELECT status FROM jobs WHERE id=?", (job_id,), "one")
                if job["status"] == "done":
                    break
                time.sleep(0.02)
            self.assertEqual(job["status"], "done")
            account = server.get_api_key(key)
            self.assertEqual(
                (account["balance_cents"], account["reserved_cents"],
                 account["spent_cents"], account["calls"]),
                (99, 0, 1, 1))
        finally:
            server._stop_api_job_workers()
            server._parse_cached = original_parse
        self.assertFalse(any(t.is_alive() for t in server._job_threads))


class PrivacyStorageTests(unittest.TestCase):
    def test_legacy_identifiers_are_minimized_and_files_are_private(self):
        server.db_exec(
            "INSERT INTO request_logs(ts,kind,subject,ip,ua,link,ok,path,user_id) "
            "VALUES(1,'web','raw-subject','192.0.2.9','Full UA',"
            "'https://v.douyin.com/private/',1,'/',NULL)")
        server.db_exec(
            "INSERT INTO usage_daily(day,subject,count) VALUES(123,'ip:192.0.2.9',2)")
        with server._db_lock:
            conn = server._db()
            try:
                server._migrate_privacy_data(conn)
                conn.commit()
            finally:
                conn.close()
        row = server.db_exec(
            "SELECT * FROM request_logs WHERE ip<>'' ORDER BY id DESC LIMIT 1",
            (), "one")
        self.assertRegex(row["ip"], r"^h:[0-9a-f]{24}$")
        self.assertEqual(row["ua"], "")
        self.assertRegex(row["link"], r"^h:[0-9a-f]{24}$")
        quota = server.db_exec(
            "SELECT subject FROM usage_daily WHERE day=123", (), "one")
        self.assertRegex(quota["subject"], r"^ip:h:[0-9a-f]{24}$")
        self.assertEqual(
            stat.S_IMODE(server.DB_FILE.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(Path(_TEST_DATA.name).stat().st_mode), 0o700)
        server.db_exec(
            "INSERT INTO reports(ts,sid,reason,contact,ip,handled) "
            "VALUES(1,'sid','reason','private contact','',0)")
        now = int(time.time())
        old = now - 31 * 86400
        server.db_exec(
            "INSERT INTO shares(id,title,expires_at,created) VALUES(?,?,?,?)",
            ("expired-retention-share", "private title", now - 1, now - 7 * 86400))
        server.db_exec(
            "INSERT INTO api_keys(key,name,created,enabled,reserved_cents,deleted_at) "
            "VALUES(?,?,?,0,0,?)",
            ("sk_retention_deleted", "old key", old, old))
        server.db_exec(
            "INSERT INTO api_ledger(ts,key,event,reason) VALUES(?,?,?,?)",
            (old, "sk_retention_deleted", "opening", "old audit detail"))
        server.db_exec(
            "INSERT OR REPLACE INTO usage_daily(day,subject,count) VALUES(?,?,?)",
            (server._today() - server.DATA_RETENTION_DAYS,
             "ip:h:retention-boundary", 1))
        server._cleanup_retained_data(force=True)
        self.assertEqual(
            server.db_exec(
                "SELECT COUNT(*) n FROM reports WHERE contact='private contact'",
                (), "one")["n"], 0)
        self.assertEqual(
            server.db_exec(
                "SELECT COUNT(*) n FROM shares WHERE id='expired-retention-share'",
                (), "one")["n"], 0)
        self.assertEqual(
            server.db_exec(
                "SELECT COUNT(*) n FROM api_ledger "
                "WHERE key='sk_retention_deleted'", (), "one")["n"], 0)
        self.assertEqual(
            server.db_exec(
                "SELECT COUNT(*) n FROM api_keys "
                "WHERE key='sk_retention_deleted'", (), "one")["n"], 0)
        self.assertEqual(
            server.db_exec(
                "SELECT COUNT(*) n FROM usage_daily "
                "WHERE subject='ip:h:retention-boundary'", (), "one")["n"], 0)


class AtcEnhancementTests(unittest.TestCase):
    """AnyToCopy 统一解析：默认媒体模式、主动文案、配额、缓存与线路校验。"""

    def setUp(self):
        server.db_exec("DELETE FROM atc_cache")
        server.db_exec("DELETE FROM atc_jobs")
        server.db_exec("DELETE FROM quota_reservations WHERE endpoint='atc_transcript'")
        server.db_exec("DELETE FROM usage_daily WHERE subject LIKE 'atc:%'")
        for k in ("atc_enabled", "atc_api_key", "atc_api_secret", "atc_base_url",
                  "atc_play_enhance", "atc_transcript_enabled", "atc_transcript_daily",
                  "atc_url_ttl", "share_play_priority", "atc_test_state"):
            server.db_exec("DELETE FROM app_settings WHERE k=?", (k,))
        # 直接造一个登录用户会话（绕过滑块，滑块链路本身由其他用例覆盖）
        now = int(time.time())
        server.db_exec(
            "INSERT OR IGNORE INTO users(id,email,pw_salt,pw_hash,created_at,disabled) "
            "VALUES(424242,'atc@test.dev','s','h',?,0)", (now,))
        self.token = server._new_user_session(424242)
        self.client = TestClient(server.app)
        self.client.cookies.set("sess", self.token)

    def tearDown(self):
        server._user_sessions.pop(self.token, None)
        server.db_exec("DELETE FROM users WHERE id=424242")
        server.db_exec("DELETE FROM usage_daily WHERE subject LIKE 'atc:%'")

    def _enable(self, daily="5"):
        server.set_app_setting("atc_enabled", "1")
        server.set_app_setting("atc_api_key", "ak_test")
        server.set_app_setting("atc_api_secret", "sk_test")
        server.set_app_setting("atc_transcript_enabled", "1")
        server.set_app_setting("atc_transcript_daily", daily)

    def test_primary_extract_omits_text_task_by_default(self):
        self._enable()
        calls = []

        def request(method, path, params, _cfg):
            calls.append((method, path, dict(params)))
            if path == "/video/extract":
                return {"code": 200, "data": "task-basic"}
            return {"code": 200, "data": {
                "status": "WAITING", "title": "普通解析",
                "videoUrl": "https://v3.douyinvod.com/basic.mp4",
                "textContent": "不应该进入解析结果",
                "workType": "video", "duration": 12.5,
            }}

        with mock.patch.object(server, "_atc_request", side_effect=request), \
                mock.patch.object(server.time, "sleep"):
            raw = server._atc_extract(
                "https://v.douyin.com/BasicTask/", include_text=False)
        self.assertEqual(raw["title"], "普通解析")
        self.assertNotIn("taskType", calls[0][2])
        self.assertEqual(calls[0][:2], ("POST", "/video/extract"))

    def test_transcript_extract_explicitly_requests_text(self):
        self._enable()
        calls = []

        def request(method, path, params, _cfg):
            calls.append((method, path, dict(params)))
            if path == "/video/extract":
                return {"code": 200, "data": "task-text"}
            return {"code": 200, "data": {
                "status": "SUCCESS", "videoUrl":
                "https://v3.douyinvod.com/text.mp4", "textContent": "全文"}}

        with mock.patch.object(server, "_atc_request", side_effect=request), \
                mock.patch.object(server.time, "sleep"):
            server._atc_extract(
                "https://v.douyin.com/TextTask/", include_text=True)
        self.assertEqual(calls[0][2]["taskType"], "TEXT")

    def test_atc_result_maps_to_existing_contract_without_transcript(self):
        self._enable()
        work_url = "https://v.douyin.com/MapTask/"
        parsed = server._atc_result_to_parse(work_url, {
            "title": "科幻产品 #科技",
            "content": "科幻产品 #科技",
            "videoUrlList": "https://v3.douyinvod.com/map.mp4",
            "textContent": "默认解析不应下发这个字段",
            "duration": 8.2, "workType": "video",
        })
        self.assertEqual(parsed["source"], "atc")
        self.assertEqual(parsed["video"]["source"], "atc")
        self.assertEqual(parsed["duration_ms"], 8200)
        self.assertIn("/api/atc/video/", parsed["video"]["download_url"])
        self.assertNotIn("textContent", parsed)
        self.assertNotIn("text_content", parsed)
        cached = server._atc_cache_get(parsed["item_id"])
        self.assertEqual(cached["work_url"], work_url)
        self.assertEqual(cached["video_url"],
                         "https://v3.douyinvod.com/map.mp4")
        self.assertFalse(cached["text_content"])

    def test_master_switch_off_is_silent(self):
        # 未配置密钥/未开启 → 404，不暴露功能存在
        r = self.client.post("/api/atc/transcript", json={"item_id": "1"})
        self.assertEqual(r.status_code, 404)
        r = self.client.get("/api/atc/transcript", params={"item_id": "1"})
        self.assertEqual(r.status_code, 404)

    def test_anonymous_gets_401(self):
        self._enable()
        anon = TestClient(server.app)
        r = anon.post("/api/atc/transcript", json={"item_id": "7001"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("登录", r.json()["error"])

    def test_submit_reserves_quota_and_dedups(self):
        self._enable()
        r = self.client.post("/api/atc/transcript", json={"item_id": "7002"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "processing")
        self.assertEqual(r.json()["remaining"], 4)
        # 预占已结算为实际使用
        row = server.db_exec(
            "SELECT count FROM usage_daily WHERE subject='atc:user:424242'", (), "one")
        self.assertEqual(row[0], 1)
        # 幂等入队：同一 item 只有一个在途任务
        self.assertTrue(server._atc_enqueue("7002", purpose="transcript") is False)
        n = server.db_exec(
            "SELECT COUNT(*) FROM atc_jobs WHERE item_id='7002'", (), "one")[0]
        self.assertEqual(n, 1)

    def test_cache_hit_is_free(self):
        self._enable()
        now = int(time.time())
        server.db_exec(
            "INSERT INTO atc_cache(item_id,text_content,video_url,url_fetched_at,"
            "created,updated) VALUES('7003','全文','',0,?,?)", (now, now))
        r = self.client.post("/api/atc/transcript", json={"item_id": "7003"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "ready")
        self.assertEqual(r.json()["text"], "全文")
        # 命中缓存不扣次、不产生任务
        row = server.db_exec(
            "SELECT count FROM usage_daily WHERE subject='atc:user:424242'", (), "one")
        self.assertIsNone(row)
        n = server.db_exec(
            "SELECT COUNT(*) FROM atc_jobs WHERE item_id='7003'", (), "one")[0]
        self.assertEqual(n, 0)

    def test_daily_limit_and_429(self):
        self._enable(daily="2")
        for i in range(2):
            r = self.client.post("/api/atc/transcript",
                                 json={"item_id": f"71{i}"})
            self.assertEqual(r.status_code, 200)
        r = self.client.post("/api/atc/transcript", json={"item_id": "7199"})
        self.assertEqual(r.status_code, 429)
        # 超限不产生新任务
        n = server.db_exec(
            "SELECT COUNT(*) FROM atc_jobs WHERE item_id='7199'", (), "one")[0]
        self.assertEqual(n, 0)

    def test_reservation_refund_on_enqueue_failure(self):
        self._enable()
        with mock.patch.object(server, "_atc_enqueue", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                self.client.post("/api/atc/transcript", json={"item_id": "7200"})
        # 失败已退款
        row = server.db_exec(
            "SELECT count FROM usage_daily WHERE subject='atc:user:424242'", (), "one")
        self.assertTrue(row is None or row[0] == 0)

    def test_play_priority_validation(self):
        admin = TestClient(server.app)
        r = admin.post("/api/admin/login",
                       json={"password": "test-only-admin-password"})
        self.assertEqual(r.status_code, 200)
        r = admin.post("/api/admin/atc", json={"play_priority": ["dy1", "dy2"]})
        self.assertEqual(r.status_code, 400)
        r = admin.post("/api/admin/atc",
                       json={"play_priority": ["proxy", "atc", "dy2", "dy1"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(server._atc_cfg()["play_priority"],
                         ["proxy", "atc", "dy2", "dy1"])
        # 非法 JSON 落库也不炸：自动回退默认顺序
        server.set_app_setting("share_play_priority", "not-json")
        self.assertEqual(server._atc_cfg()["play_priority"],
                         ["atc", "proxy", "dy1", "dy2"])

    def test_admin_cannot_redirect_api_credentials_to_another_host(self):
        admin = TestClient(server.app)
        r = admin.post("/api/admin/login",
                       json={"password": "test-only-admin-password"})
        self.assertEqual(r.status_code, 200)
        r = admin.post("/api/admin/atc", json={
            "base_url": "https://example.com/v1"})
        self.assertEqual(r.status_code, 400)
        r = admin.post("/api/admin/atc", json={
            "base_url": server.ATC_DEFAULT_BASE})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(server._atc_cfg()["base"], server.ATC_DEFAULT_BASE)

    def test_admin_connection_test_uses_basic_mode_and_accepts_immediate_result(self):
        self._enable()
        admin = TestClient(server.app)
        r = admin.post("/api/admin/login",
                       json={"password": "test-only-admin-password"})
        self.assertEqual(r.status_code, 200)
        calls = []

        def request(method, path, params, _cfg):
            calls.append((method, path, dict(params)))
            return {"code": 200, "data": {
                "status": "WAITING", "duration": 9,
                "videoUrlList": ["https://v3.douyinvod.com/test.mp4"]}}

        with mock.patch.object(server, "_atc_request", side_effect=request):
            r = admin.post("/api/admin/atc/test", json={
                "work_url": "https://v.douyin.com/TestBasic/"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "success")
        self.assertTrue(r.json()["has_video"])
        self.assertNotIn("taskType", calls[0][2])

    def test_share_view_injects_atc_only_when_fresh(self):
        import json as _json
        row = {"id": "atcv123", "item_id": "7300", "kind": "video", "vid": "v9",
               "title": "t", "author": "a", "avatar": "", "cover": "",
               "custom_title": "", "payload": _json.dumps({"video": {}}),
               "expires_at": 0, "status": "ok", "views": 0, "plays": 0,
               "downloads": 0, "cta_clicks": 0, "created": int(time.time())}
        # 关闭时：无 atc_url，但有默认优先级
        view = server._share_view(row)
        self.assertNotIn("atc_url", view["data"]["video"])
        self.assertEqual(view["data"]["play_priority"], ["atc", "proxy", "dy1", "dy2"])
        # 开启 + 新鲜缓存：注入
        self._enable()
        now = int(time.time())
        server.db_exec(
            "INSERT INTO atc_cache(item_id,video_url,url_fetched_at,created,updated) "
            "VALUES('7300','https://cdn.example.com/x.mp4',?,?,?)", (now, now, now))
        view = server._share_view(row)
        self.assertEqual(view["data"]["video"].get("atc_url"),
                         "https://cdn.example.com/x.mp4")
        # 过期：不注入且惰性入队
        server.db_exec(
            "UPDATE atc_cache SET url_fetched_at=? WHERE item_id='7300'",
            (now - 100000,))
        view = server._share_view(row)
        self.assertNotIn("atc_url", view["data"]["video"])
        job = server.db_exec(
            "SELECT purpose,status FROM atc_jobs WHERE item_id='7300'", (), "one")
        self.assertEqual(tuple(job), ("play", "pending"))


class PlayLogTests(unittest.TestCase):
    """播放请求日志：分页、筛选、失败后下一条重试线路（next_src）。"""

    def setUp(self):
        server.db_exec("DELETE FROM share_events")
        self.admin = TestClient(server.app)
        r = self.admin.post("/api/admin/login",
                            json={"password": "test-only-admin-password"})
        self.assertEqual(r.status_code, 200)

    def tearDown(self):
        server.db_exec("DELETE FROM share_events")

    def _seed(self, n=25):
        now = int(time.time())
        for i in range(n):
            kind = ("play_try", "play_ok", "play_fail")[i % 3]
            server.db_exec(
                "INSERT INTO share_events(ts,sid,kind,ip,ua,referer,wechat,fp,"
                "source,stage,detail,ms,next_src) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now - i, "sidABC1", kind, "", "wechat/8 ios", "",
                 i % 2, "", "dy1", "error" if kind == "play_fail" else "start",
                 "code=3" if kind == "play_fail" else "", 1200,
                 "dy2" if kind == "play_fail" else ""))

    def test_pagination_and_total(self):
        self._seed(25)
        r = self.admin.get("/api/admin/play-logs?page=1&size=10")
        d = r.json()
        self.assertEqual(d["total"], 25)
        self.assertEqual(d["pages"], 3)
        self.assertEqual(len(d["rows"]), 10)
        r = self.admin.get("/api/admin/play-logs?page=3&size=10")
        self.assertEqual(len(r.json()["rows"]), 5)

    def test_filters(self):
        self._seed(9)   # try/ok/fail 各 3，微信内外各半
        d = self.admin.get("/api/admin/play-logs?result=fail").json()
        self.assertEqual(d["total"], 3)
        self.assertTrue(all(r["kind"] == "play_fail" for r in d["rows"]))
        d = self.admin.get("/api/admin/play-logs?wechat=1").json()
        self.assertTrue(all(r["wechat"] == 1 for r in d["rows"]))
        d = self.admin.get("/api/admin/play-logs?sid=nope").json()
        self.assertEqual(d["total"], 0)

    def test_next_src_recorded_on_fail(self):
        self._seed(3)
        d = self.admin.get("/api/admin/play-logs?result=fail").json()
        self.assertEqual(d["rows"][0]["next_src"], "dy2")
        # 事件接口也接受 next 字段
        c = TestClient(server.app)
        r = c.post("/api/share/sidABC1/event",
                   json={"kind": "play_fail", "source": "atc",
                         "stage": "error", "detail": "code=4", "ms": 800,
                         "next": "proxy"})
        self.assertEqual(r.status_code, 200)
        row = server.db_exec(
            "SELECT next_src FROM share_events WHERE source='atc'", (), "one")
        self.assertEqual(row[0], "proxy")


if __name__ == "__main__":
    unittest.main()
