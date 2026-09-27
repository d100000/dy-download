"""可选的抖音 HTTP 元数据客户端；只在独立 worker 中加载固定版本 SDK。

一个实例在同一 asyncio loop 中串行复用连接和匿名访客身份，不缓存作品。
不读取 Cookie 文件、浏览器配置或环境代理，不登录，不下载媒体。deadline
是 monotonic 的绝对截止时间，覆盖锁等待、DNS、短链、签名和响应；worker
父进程仍须在截止时杀掉卡住的子进程，以覆盖原生库/同步导入无法取消的情况。
"""
import asyncio
import importlib.metadata
import inspect
import ipaddress
import json
import re
import socket
import time
from urllib.parse import parse_qs, urljoin, urlsplit

from browser_titles import valid_work_url, work_identity
from metadata_fields import extract_metadata, is_truncated_title, sanitize_metadata, text_value


SDK_COMMIT = "737bf3dfe9de1dbff57990c0ec4c9e02c75c3d0f"
SDK_VERSION = "5.1.1"
SDK_ARCHIVE_URL = ("https://codeload.github.com/Evil0ctal/Douyin_TikTok_Download_API/tar.gz/"
                   + SDK_COMMIT + "?metadata_runtime=1")
SDK_ARCHIVE_SHA256 = "8da662c5656ae039678b82cbc0688253537760b1f6fccbf037858c2c23f3e18e"
DEFAULT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
_HOSTS = frozenset(("douyin.com", "www.douyin.com", "v.douyin.com",
                    "iesdouyin.com", "www.iesdouyin.com"))
_DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
_REDIRECTS = (301, 302, 303, 307, 308)
_GUEST_COOKIES = frozenset(("ttwid", "msToken", "odin_tt", "tt_webid", "tt_webid_v2",
                           "__ac_nonce", "__ac_signature", "__ac_referer", "s_v_web_id",
                           "uifid", "uifid_temp", "uifidtemp", "UIFID", "UIFID_TEMP", "UIFIDTEMP"))
_INVALID_TITLE = re.compile(r"^(?:抖音|Douyin|加载中[.。…]*|Loading[.。…]*|无标题|（无标题）|"
                            r"请完成安全验证|请完成验证|验证码|安全验证|扫码登录|页面不存在|作品已删除)$", re.I)
ERROR_CODES = frozenset((
    "dependency_unavailable", "dependency_version_mismatch", "unexpected_sdk_endpoint",
    "unexpected_api_redirect", "response_too_large", "invalid_json", "invalid_proxy",
    "invalid_user_agent", "untrusted_url", "dns_failed", "blocked_private_dns",
    "blocked_network_policy", "client_closed", "shortlink_no_redirect",
    "shortlink_invalid_target", "shortlink_redirect_limit", "upstream_status",
    "missing_detail", "identity_mismatch", "kind_mismatch", "missing_title",
    "invalid_work_url", "deadline_exceeded", "http_request_failed", "upstream_failed",
    "upstream_business_error", "upstream_risk_control", "upstream_network_error",
))


class MetadataHTTPError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _official_url(url):
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 32 for c in url):
        return False
    try:
        p = urlsplit(url)
        return bool(p.scheme == "https" and p.hostname in _HOSTS
                    and not p.username and not p.password and p.port in (None, 443))
    except ValueError:
        return False


def _location_identity(url):
    kind, item_id = work_identity(url)
    if item_id:
        return kind, item_id
    if not _official_url(url):
        return "", ""
    # 官方短链有时返回首页 modal_id；只读取明确唯一的作品 ID。
    parsed = urlsplit(url)
    if parsed.path not in ("", "/"):
        return "", ""
    query = parse_qs(parsed.query)
    ids = {value for key in ("modal_id", "aweme_id", "item_id", "vid")
           for value in query.get(key, []) if re.fullmatch(r"\d{8,30}", value)}
    return ("", next(iter(ids))) if len(ids) == 1 else ("", "")


def _guest_cookies(cookies):
    """调用方只能提供专用匿名 context；登录 Cookie 即使误传也不会被发送。"""
    if isinstance(cookies, list):
        cookies = {row.get("name"): row.get("value") for row in cookies
                   if isinstance(row, dict) and str(row.get("domain") or "").lstrip(".") in _HOSTS}
    if not isinstance(cookies, dict):
        return {}
    return {name: value for name, value in cookies.items()
            if name in _GUEST_COOKIES and isinstance(value, str) and 0 < len(value) <= 8192
            and not any(ord(c) < 32 or c == ";" for c in value)}


class _NoRedirectClient:
    """使用 SDK 原有 TLS/代理/连接池工厂，仅关闭未经验证的自动重定向。"""
    def __init__(self, client, redirect_policy):
        self.client, self.redirect_policy = client, redirect_policy

    async def request(self, method, url, **kwargs):
        kwargs["redirect"] = self.redirect_policy
        return await self.client.request(method, url, **kwargs)

    def close(self):
        self.client.close()


def _verify_sdk_distribution(dist):
    """VCS commit 或完整归档 SHA 固定源码；本地归档也必须有同一内容摘要。"""
    try:
        origin = json.loads(dist.read_text("direct_url.json") or "{}")
        commit = origin.get("vcs_info", {}).get("commit_id")
        archive = origin.get("archive_info") or {}
        archive_hash = (archive.get("hashes") or {}).get("sha256")
        source_url = origin.get("url", "")
        source = urlsplit(source_url)
        allowed_source = source_url == SDK_ARCHIVE_URL or (
            source.scheme == "file" and source.hostname in (None, "", "localhost"))
        verified_archive = allowed_source and archive_hash == SDK_ARCHIVE_SHA256
        if dist.version != SDK_VERSION or (commit != SDK_COMMIT and not verified_archive):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise MetadataHTTPError("dependency_version_mismatch") from None


class _SDKBackend:
    """真实 SDK 请求/签名/transport/classifier，未启动其 API、调度器或数据库。"""
    def __init__(self, proxy, user_agent, cookies):
        _verify_sdk_distribution(importlib.metadata.distribution("dtk"))
        from dtk.core.logging import configure
        from dtk.core.types import BrowserFamily, Outcome, Platform
        from dtk.platforms.douyin.adapter import ADAPTER
        from dtk.signing import NativeSigner, RequestSpec as SigningRequest, SigningSession, StaticFingerprint
        from dtk.transport import Fingerprint, RequestSpec, TransportIdentity, WreqTransport
        from dtk.transport.wreq_transport import default_client_factory
        import httpx
        import wreq

        configure("critical")  # 子进程 stdout 留给净化后的 IPC；不记录原始 SDK 错误。
        major = re.search(r"(?:HeadlessChrome|Chrome)/(\d+)", user_agent)
        fingerprint = Fingerprint(
            browser_family=BrowserFamily.CHROME, browser_major=int(major[1]) if major else 130,
            user_agent=user_agent, platform="Linux x86_64" if "Linux" in user_agent else "Win32",
            screen="1920x1080", language="zh-CN", timezone="Asia/Shanghai",
            hardware_concurrency=8, device_memory=8)
        self.cookies = cookies
        self.identity = TransportIdentity(id="metadata-guest", platform=Platform.DOUYIN,
                                         fingerprint=fingerprint, cookies=cookies, proxy_url=proxy or None)
        self.fingerprint = StaticFingerprint.of(fingerprint)
        self.session = SigningSession(cookies=cookies, proxy_url=proxy or None)
        self.signer = NativeSigner(Platform.DOUYIN)
        self.adapter, self.signing_request, self.request_spec = ADAPTER, SigningRequest, RequestSpec
        self.ok_outcome, self.profile = Outcome.OK, ADAPTER.profile_for(fingerprint)

        async def rotated(_identity, values):
            self.cookies.update(_guest_cookies(dict(values)))

        self.transport = WreqTransport(
            max_clients=1, default_timeout=6, connect_timeout=3, cookie_sink=rotated,
            client_factory=lambda options: _NoRedirectClient(default_client_factory(options), wreq.redirect.Policy.none()))
        self.short_client = httpx.AsyncClient(proxy=proxy or None, trust_env=False, timeout=6,
                                            follow_redirects=False, headers={"User-Agent": user_agent})

    async def redirect(self, url, timeout):
        # 只读短链响应头，不加载视频页、重定向正文或媒体文件。
        async with self.short_client.stream("GET", url, timeout=timeout, follow_redirects=False) as response:
            return response.status_code, response.headers.get("location", "")

    async def detail(self, item_id, remaining, timings):
        started = time.monotonic()
        unsigned = self.adapter.build_request("douyin.content_detail", aweme_id=item_id, profile=self.profile)
        if unsigned["url"] != _DETAIL_URL:
            raise MetadataHTTPError("unexpected_sdk_endpoint")
        signed = await self.signer.sign(self.signing_request.get(
            unsigned["url"], unsigned["params"], unsigned["headers"]), self.fingerprint, self.session)
        timings["sign_s"] = round(time.monotonic() - started, 6)
        started = time.monotonic()
        try:
            response = await self.transport.request(self.identity, self.request_spec(
                url=signed.signed_url(unsigned["url"]), method="GET", endpoint="douyin.content_detail",
                headers={**unsigned["headers"], **signed.headers}), timeout=remaining())
        finally:
            timings["http_s"] = round(time.monotonic() - started, 6)
        if response.status in _REDIRECTS:
            raise MetadataHTTPError("unexpected_api_redirect")
        if len(response.body) > 2_000_000:
            raise MetadataHTTPError("response_too_large")
        classification = self.transport.classify(response)
        if classification.outcome is not self.ok_outcome:
            code = str(classification.outcome.value).lower()
            error = "upstream_" + code
            raise MetadataHTTPError(error if error in ERROR_CODES else "upstream_failed")
        started = time.monotonic()
        try:
            return response.json()
        except (ValueError, UnicodeError):
            raise MetadataHTTPError("invalid_json") from None
        finally:
            timings["decode_s"] = round(time.monotonic() - started, 6)

    async def close(self):
        await self.transport.close()
        await self.short_client.aclose()


class MetadataHTTPClient:
    """fetch(url, deadline) 返回 {result, error_code, timings}；失败可交浏览器。

    policy 可为同步/异步 allows(url) 对象或可调用对象，负责每次出站的公网 DNS
    检查；即使注入 policy，模块仍固定限制官方 HTTPS 主机。未注入时逐请求解析
    并拒绝任何非公网地址。显式 proxy='' 表示直连，绝不继承环境代理。
    """
    def __init__(self, proxy="", user_agent=DEFAULT_UA, guest_cookies=None, policy=None, *, _backend_factory=None):
        self.proxy = str(proxy or "")
        if self.proxy:
            parsed = urlsplit(self.proxy)
            if parsed.scheme not in ("http", "https", "socks5", "socks5h") or not parsed.hostname:
                raise MetadataHTTPError("invalid_proxy")
        self.user_agent = self._valid_ua(user_agent)
        self.cookies = _guest_cookies(guest_cookies)
        self.policy, self._backend_factory = policy, _backend_factory or _SDKBackend
        self._backend, self._generation, self._backend_generation = None, 0, -1
        self._lock = asyncio.Lock()
        self._closed = False

    @staticmethod
    def _valid_ua(value):
        if not isinstance(value, str) or not 1 <= len(value) <= 1024 or any(ord(c) < 32 for c in value):
            raise MetadataHTTPError("invalid_user_agent")
        return value

    def set_guest_session(self, cookies, user_agent):
        """只由 worker 的新建匿名 context 调用，不接受文件路径或个人配置。"""
        user_agent = self._valid_ua(user_agent)
        cookies = _guest_cookies(cookies)
        if cookies != self.cookies or user_agent != self.user_agent:
            self.cookies, self.user_agent = cookies, user_agent
            self._generation += 1

    async def _allowed(self, url):
        if not _official_url(url):
            raise MetadataHTTPError("untrusted_url")
        if self.policy is not None:
            allows = getattr(self.policy, "allows", self.policy)
            allowed = allows(url)
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if not allowed:
                code = getattr(self.policy, "error_code", "")
                raise MetadataHTTPError(code if code in ERROR_CODES else "blocked_network_policy")
            return
        try:
            addresses = await asyncio.get_running_loop().getaddrinfo(
                urlsplit(url).hostname, 443, type=socket.SOCK_STREAM)
        except OSError:
            raise MetadataHTTPError("dns_failed") from None
        if not addresses or not all(ipaddress.ip_address(answer[4][0]).is_global for answer in addresses):
            raise MetadataHTTPError("blocked_private_dns")

    async def _get_backend(self):
        if self._backend is not None and self._backend_generation != self._generation:
            await self._backend.close()
            self._backend = None
        if self._backend is None:
            self._backend = self._backend_factory(self.proxy, self.user_agent, self.cookies)
            self._backend_generation = self._generation
        return self._backend

    async def _fetch(self, url, deadline, timings):
        remaining = lambda: max(0.001, deadline - time.monotonic())
        async with self._lock:
            if self._closed:
                raise MetadataHTTPError("client_closed")
            started = time.monotonic()
            backend = await self._get_backend()
            timings["client_setup_s"] = round(time.monotonic() - started, 6)
            kind, item_id = work_identity(url)
            started = time.monotonic()
            try:
                if not item_id:
                    current = url
                    for _ in range(3):
                        await self._allowed(current)
                        status, location = await backend.redirect(current, remaining())
                        if status not in _REDIRECTS or not location:
                            raise MetadataHTTPError("shortlink_no_redirect")
                        target = urljoin(current, location)
                        await self._allowed(target)
                        kind, item_id = _location_identity(target)
                        if item_id:
                            break
                        if not valid_work_url(target):
                            raise MetadataHTTPError("shortlink_invalid_target")
                        current = target
                    if not item_id:
                        raise MetadataHTTPError("shortlink_redirect_limit")
            finally:
                timings["resolve_s"] = round(time.monotonic() - started, 6)
            await self._allowed(_DETAIL_URL)
            payload = await backend.detail(item_id, remaining, timings)
            if not isinstance(payload, dict) or payload.get("status_code") not in (None, 0):
                raise MetadataHTTPError("upstream_status")
            obj = payload.get("aweme_detail")
            if not isinstance(obj, dict):
                raise MetadataHTTPError("missing_detail")
            if str(obj.get("aweme_id") or "") != item_id:
                raise MetadataHTTPError("identity_mismatch")
            actual_kind = "note" if isinstance(obj.get("images"), list) and obj["images"] else "video"
            if kind and kind != actual_kind:
                raise MetadataHTTPError("kind_mismatch")
            title = next((text_value(obj.get(key), 10001) for key in
                          ("full_desc", "fullDesc", "desc", "description", "title")
                          if isinstance(obj.get(key), str) and obj[key].strip()), "")
            if not title or _INVALID_TITLE.fullmatch(title):
                raise MetadataHTTPError("missing_title")
            partial = is_truncated_title(title, obj) or len(title) > 5000
            result = sanitize_metadata(extract_metadata(obj))
            result.update(item_id=item_id, kind=actual_kind, platform="douyin", title=title[:5000],
                          title_source="http_structured", title_status="partial" if partial else "available",
                          _link="https://www.douyin.com/%s/%s/" % (actual_kind, item_id))
            return result

    async def fetch(self, url, deadline):
        started, timings = time.monotonic(), {}
        result, error = None, ""
        try:
            if not valid_work_url(url):
                raise MetadataHTTPError("invalid_work_url")
            if deadline <= started:
                raise MetadataHTTPError("deadline_exceeded")
            result = await asyncio.wait_for(self._fetch(url, deadline, timings), timeout=deadline - started)
            if time.monotonic() > deadline:
                raise MetadataHTTPError("deadline_exceeded")
        except asyncio.TimeoutError:
            result, error = None, "deadline_exceeded"
        except MetadataHTTPError as exc:
            result, error = None, exc.code if exc.code in ERROR_CODES else "http_request_failed"
        except (ImportError, importlib.metadata.PackageNotFoundError, SyntaxError):
            error = "dependency_unavailable"
        except Exception:
            # 永不把 Cookie、签名 URL、代理凭据或第三方异常字符串传给 IPC。
            error = "http_request_failed"
        timings["total_s"] = round(time.monotonic() - started, 6)
        return {"result": result, "error_code": error, "timings": timings}

    async def close(self):
        self._closed = True
        async with self._lock:
            if self._backend is not None:
                await self._backend.close()
                self._backend = None
