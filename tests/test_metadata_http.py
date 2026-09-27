"""HTTP 元数据协议和安全边界的离线测试；不导入 server，不连接抖音。"""
import asyncio
import copy
import json
import socket
import time
import unittest
from unittest import mock

from tools.metadata_http import (DEFAULT_UA, MetadataHTTPClient, MetadataHTTPError,
                                 SDK_ARCHIVE_SHA256, SDK_ARCHIVE_URL, SDK_COMMIT,
                                 _GUEST_COOKIES, _NoRedirectClient, _verify_sdk_distribution)


ITEM = "7689777123456789012"
URL = "https://www.douyin.com/video/%s/" % ITEM
SHORT = "https://v.douyin.com/GuestOnly/"


def detail():
    return {"status_code": 0, "aweme_detail": {
        "aweme_id": ITEM, "desc": "完整的公开作品文案", "create_time": 1700000000,
        "author": {"nickname": "作者", "sec_uid": "guest_public_author"},
        "statistics": {"digg_count": 0, "comment_count": 3, "share_count": 4, "collect_count": 5},
        "video": {"duration": 12345, "width": 1080, "height": 1920,
                  "play_addr": {"url_list": ["https://v3.douyinvod.com/media?secret=test"]}},
        "cookie": "never-publish", "sessionid": "never-publish"}}


class FakeBackend:
    def __init__(self, proxy, user_agent, cookies):
        self.proxy, self.user_agent, self.cookies = proxy, user_agent, cookies
        self.payload, self.redirects, self.requests = detail(), [(302, URL)], []
        self.wait = 0
        self.closed = False
        self.error = None

    async def redirect(self, url, timeout):
        self.requests.append(("redirect", url))
        return self.redirects.pop(0)

    async def detail(self, item_id, remaining, timings):
        self.requests.append(("detail", item_id))
        await asyncio.sleep(self.wait)
        if self.error:
            raise self.error
        return copy.deepcopy(self.payload)

    async def close(self):
        self.closed = True


class MetadataHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backends = []
        def factory(*args):
            backend = FakeBackend(*args)
            self.backends.append(backend)
            return backend
        self.client = MetadataHTTPClient(policy=lambda url: True, _backend_factory=factory)
        self.backend = await self.client._get_backend()
        self.addAsyncCleanup(self.client.close)

    async def fetch(self, url=URL):
        return await self.client.fetch(url, time.monotonic() + 1)

    async def test_exact_identity_public_fields_and_zero_stats_without_media_or_credentials(self):
        row = await self.fetch()
        result = row["result"]
        self.assertEqual(row["error_code"], "")
        self.assertEqual(result["item_id"], ITEM)
        self.assertEqual(result["_link"], URL)
        self.assertEqual(result["title_status"], "available")
        self.assertEqual(result["title_source"], "http_structured")
        self.assertEqual(result["author"], "作者")
        self.assertEqual(result["stats"]["digg"], 0)
        self.assertEqual(result["duration_ms"], 12345)
        self.assertEqual(result["video"], {"width": 1080, "height": 1920})
        self.assertNotIn("secret", json.dumps(row))
        self.assertNotIn("sessionid", json.dumps(row))
        self.assertIn("total_s", row["timings"])

    async def test_connection_backend_reused_without_reusing_content(self):
        first = await self.fetch()
        self.backend.payload["aweme_detail"]["desc"] = "另一次响应"
        second = await self.fetch()
        self.assertNotEqual(first["result"]["title"], second["result"]["title"])
        self.assertEqual(len(self.backends), 1)
        self.assertEqual(len(self.backend.requests), 2)

    async def test_shortlink_each_hop_validated_and_resolved_id_used(self):
        checked = []
        self.client.policy = lambda url: checked.append(url) or True
        row = await self.fetch(SHORT)
        self.assertEqual(row["result"]["item_id"], ITEM)
        self.assertEqual(self.backend.requests[0], ("redirect", SHORT))
        self.assertIn(URL, checked)
        self.assertTrue(any("/aweme/detail/" in value for value in checked))

    async def test_untrusted_redirect_never_requested_even_with_permissive_policy(self):
        for target in ("https://attacker.example/video/" + ITEM, "http://www.douyin.com/video/" + ITEM,
                       "https://user:secret@www.douyin.com/video/" + ITEM,
                       "https://www.douyin.com:8443/video/" + ITEM):
            self.backend.redirects = [(302, target)]
            row = await self.fetch(SHORT)
            self.assertEqual(row["error_code"], "untrusted_url")
            self.assertIsNone(row["result"])
        self.assertTrue(all(call[0] == "redirect" for call in self.backend.requests))

    async def test_missing_or_changed_identity_and_kind_rejected(self):
        self.backend.payload["aweme_detail"]["aweme_id"] = ITEM[:-1] + "9"
        self.assertEqual((await self.fetch())["error_code"], "identity_mismatch")
        self.backend.payload = detail()
        self.backend.payload["aweme_detail"]["images"] = [{"url_list": ["image"]}]
        self.assertEqual((await self.fetch())["error_code"], "kind_mismatch")
        row = await self.fetch(URL.replace("/video/", "/note/"))
        self.assertEqual(row["result"]["kind"], "note")

    async def test_anonymous_cookie_allowlist_and_ua_changed_together(self):
        self.client.set_guest_session([
            {"name": "UIFID_TEMP", "value": "anonymous-token", "domain": ".douyin.com"},
            {"name": "sessionid", "value": "personal-token", "domain": ".douyin.com"},
            {"name": "ttwid", "value": "wrong-domain-token", "domain": ".example.com"}], DEFAULT_UA)
        await self.fetch()
        current = self.backends[-1]
        self.assertTrue(self.backend.closed)
        self.assertEqual(current.cookies, {"UIFID_TEMP": "anonymous-token"})
        self.assertEqual(current.user_agent, DEFAULT_UA)
        self.assertNotIn("sessionid", _GUEST_COOKIES)

    async def test_deadline_covers_request_and_queue_wait(self):
        self.backend.wait = 1
        started = time.monotonic()
        row = await self.client.fetch(URL, started + 0.02)
        self.assertEqual(row["error_code"], "deadline_exceeded")
        self.assertLess(time.monotonic() - started, 0.3)
        await self.client._lock.acquire()
        try:
            row = await self.client.fetch(URL, time.monotonic() + 0.02)
            self.assertEqual(row["error_code"], "deadline_exceeded")
        finally:
            self.client._lock.release()

    async def test_private_dns_blocks_before_request(self):
        self.client.policy = None
        with mock.patch.object(asyncio.get_running_loop(), "getaddrinfo", new=mock.AsyncMock(
                return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.151", 443))])):
            row = await self.fetch()
        self.assertEqual(row["error_code"], "blocked_private_dns")
        self.assertEqual(self.backend.requests, [])

    async def test_timeout_dns_is_covered_by_deadline(self):
        self.client.policy = None
        async def slow_dns(*args, **kwargs):
            await asyncio.sleep(10)
        with mock.patch.object(asyncio.get_running_loop(), "getaddrinfo", new=slow_dns):
            row = await self.client.fetch(URL, time.monotonic() + 0.02)
        self.assertEqual(row["error_code"], "deadline_exceeded")
        self.assertEqual(self.backend.requests, [])

    async def test_truncation_notice_and_local_length_limit_never_mark_complete(self):
        for title in ("作品部分文案…", "部分文案 版本过低，升级后可展示全部信息", "长" * 5001):
            self.backend.payload["aweme_detail"]["desc"] = title
            row = await self.fetch()
            self.assertEqual(row["result"]["title_status"], "partial")
        self.assertEqual(len(row["result"]["title"]), 5000)
        self.assertEqual(len(row["result"]["content"]), 5001)
        self.backend.payload["aweme_detail"]["desc"] = "长" * 2000
        self.assertEqual((await self.fetch())["result"]["title_status"], "available")

    async def test_empty_and_nonzero_status_rejected_and_errors_redacted(self):
        self.backend.payload = {"status_code": 8}
        self.assertEqual((await self.fetch())["error_code"], "upstream_status")
        self.backend.payload = detail()
        self.backend.payload["aweme_detail"]["desc"] = ""
        self.assertEqual((await self.fetch())["error_code"], "missing_title")
        self.backend.error = RuntimeError("secret=private-token https://user:pass@proxy/")
        row = await self.fetch()
        self.assertEqual(row["error_code"], "http_request_failed")
        self.assertNotIn("private-token", json.dumps(row))

    async def test_invalid_input_or_expired_budget_does_not_request(self):
        row = await self.fetch("http://127.0.0.1/internal")
        self.assertEqual(row["error_code"], "invalid_work_url")
        row = await self.client.fetch(URL, time.monotonic() - 1)
        self.assertEqual(row["error_code"], "deadline_exceeded")
        self.assertEqual(self.backend.requests, [])

    async def test_missing_sdk_is_optional(self):
        def unavailable(*args):
            raise ImportError("not installed")
        client = MetadataHTTPClient(_backend_factory=unavailable)
        self.addAsyncCleanup(client.close)
        row = await client.fetch(URL, time.monotonic() + 1)
        self.assertEqual(row["error_code"], "dependency_unavailable")

    async def test_transport_wrapper_disables_automatic_redirects(self):
        raw = mock.Mock()
        raw.request = mock.AsyncMock(return_value="response")
        policy = object()
        wrapped = _NoRedirectClient(raw, policy)
        await wrapped.request("GET", URL, headers={"test": "value"})
        self.assertIs(raw.request.call_args.kwargs["redirect"], policy)
        wrapped.close()
        raw.close.assert_called_once()

    async def test_distribution_requires_fixed_git_commit_or_archive_hash(self):
        for url in (SDK_ARCHIVE_URL, "file:///tmp/sdk.tar.gz"):
            dist = mock.Mock(version="5.1.1")
            dist.read_text.return_value = json.dumps({"url": url, "archive_info": {
                "hashes": {"sha256": SDK_ARCHIVE_SHA256}}})
            _verify_sdk_distribution(dist)
            dist.read_text.return_value = json.dumps({"url": url, "archive_info": {
                "hashes": {"sha256": "0" * 64}}})
            with self.assertRaisesRegex(MetadataHTTPError, "dependency_version_mismatch"):
                _verify_sdk_distribution(dist)
        dist.read_text.return_value = json.dumps({"url": "https://attacker.example/sdk.tar.gz",
                "archive_info": {"hashes": {"sha256": SDK_ARCHIVE_SHA256}}})
        with self.assertRaises(MetadataHTTPError):
            _verify_sdk_distribution(dist)
        dist.read_text.return_value = json.dumps({"vcs_info": {"commit_id": SDK_COMMIT}})
        _verify_sdk_distribution(dist)
        dist.version = "5.1.2"
        with self.assertRaises(MetadataHTTPError):
            _verify_sdk_distribution(dist)


if __name__ == "__main__":
    unittest.main()
