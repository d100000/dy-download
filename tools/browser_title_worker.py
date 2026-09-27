#!/usr/bin/env python3
"""浏览器标题子进程：标准输入/输出仅传递任务和净化后的标题 JSON。

不加载 server，不读取应用密钥、用户 cookie 或持久浏览器配置。
"""
import asyncio
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from urllib.parse import urlsplit, unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_titles import valid_work_url, work_identity
from metadata_fields import extract_metadata, is_truncated_title


_RESOURCE_SUFFIXES = ("douyin.com", "iesdouyin.com", "douyinstatic.com", "douyincdn.com",
                      "byteimg.com", "bytedance.com", "bytedance.net", "bytednsdoc.com",
                      "pstatp.com", "snssdk.com", "ibytedtos.com", "bytecdn.cn", "zjcdn.com")
# 原作品页面实际引用的安全 SDK；只加入精确主机，不开放整个 CDN 后缀。
_RESOURCE_HOSTS = frozenset(("lf-security.bytegoofy.com", "lf-security-backup.bytegoofy.com"))
_BLOCKED_TYPES = frozenset(("media", "image", "font", "websocket", "eventsource"))
_PLACEHOLDER = re.compile(r"^(?:抖音|Douyin|加载中[.。…]*|Loading[.。…]*|无标题|（无标题）)$", re.I)
_SHELL = re.compile(r"^(?:(?:抖音\s*[-_|]?\s*|在抖音)?记录美好生活\d*|"
                    r"请完成安全验证|请完成验证|验证码|安全验证|扫码登录|页面不存在|作品已删除)[!！。.\s]*$")


def clean_title(value):
    if not isinstance(value, str):
        return ""
    title = re.sub(r"\s+", " ", value).strip()
    title = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", title)
    if not title or len(title) > 5000 or _PLACEHOLDER.fullmatch(title):
        return ""
    if _SHELL.fullmatch(title):
        return ""
    return title


def title_result(item_id, kind, title, source, complete=False, truncated=False):
    title = clean_title(title)
    if not title or not re.fullmatch(r"\d{8,30}", item_id):
        return None
    partial = truncated or is_truncated_title(title)
    return {"item_id": item_id, "kind": kind, "title": title, "title_source": source,
            "title_status": "partial" if partial else ("complete" if complete else "available"),
            "_link": "https://www.douyin.com/%s/%s/" % (kind, item_id)}


def _rank(result):
    if not result:
        return (-1, 0)
    return ({"partial": 0, "available": 1, "complete": 2}[result["title_status"]], len(result["title"]))


def pick_result(old, new):
    if not old or not new:
        return new or old
    preferred, other = (new, old) if _rank(new) > _rank(old) else (old, new)
    if (preferred.get("item_id"), preferred.get("kind")) != (other.get("item_id"), other.get("kind")):
        return old
    result = dict(preferred)
    # 标题质量单独排序；同作品的结构化字段只补缺失值，保留合法的 0。
    for key in ("author", "avatar", "author_url", "create_time", "stats", "duration_ms",
                "video", "cover", "content", "content_status", "tags", "snapshot_at"):
        value = other.get(key)
        if result.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
            result[key] = value
        elif isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = dict(result[key])
            for field, field_value in value.items():
                if result[key].get(field) in (None, "") and field_value not in (None, ""):
                    result[key][field] = field_value
    return result


def extract_structured(payloads, item_id, kind):
    """只读取含明确作品 ID 的对象，不接受推荐条目或父节点推断的 ID。"""
    pending, visited, best = list(payloads), 0, None
    while pending and visited < 15000:
        obj = pending.pop()
        visited += 1
        if isinstance(obj, list):
            pending.extend(obj[:500])
        elif isinstance(obj, dict):
            ids = {str(obj[key]) for key in ("aweme_id", "awemeId", "item_id", "itemId")
                   if obj.get(key) is not None}
            if ids == {item_id}:
                metadata = extract_metadata(obj)
                for field in ("full_desc", "fullDesc", "full_title", "fullTitle", "desc", "description", "title"):
                    title = clean_title(obj.get(field))
                    if title:
                        candidate = title_result(item_id, kind, title, "browser_structured",
                                                 complete=field.startswith("full"),
                                                 truncated=is_truncated_title(title, obj))
                        candidate.update(metadata)
                        best = pick_result(best, candidate)
            # 仅识别直接标识作品的 JSON-LD，不把页面地址赋给未知推荐视频。
            if obj.get("@type") in ("VideoObject", "ImageObject"):
                identities = {work_identity(obj.get(key))[1] for key in ("url", "@id") if isinstance(obj.get(key), str)}
                if identities == {item_id}:
                    for field in ("description", "name"):
                        candidate = title_result(item_id, kind, obj.get(field), "browser_structured",
                                                 truncated=is_truncated_title(obj.get(field), obj))
                        if candidate:
                            candidate.update(extract_metadata(obj))
                            best = pick_result(best, candidate)
            pending.extend(value for value in obj.values() if isinstance(value, (dict, list)))
    return best


class NetworkPolicy:
    """官方域名白名单、所有地址必须公网；主导航只接受作品及短链。"""
    def __init__(self, resolver=None):
        self.resolver = resolver or socket.getaddrinfo
        self._hosts = {}
        self.error_code = ""

    def allows(self, url, resource_type, navigation=False, main_frame=True):
        if resource_type in _BLOCKED_TYPES:
            return False
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
                    or host == "live.douyin.com"
                    or not (host in _RESOURCE_HOSTS
                            or any(host == suffix or host.endswith("." + suffix) for suffix in _RESOURCE_SUFFIXES))):
                if navigation and main_frame:
                    self.error_code = "network_policy_blocked"
                return False
            if navigation and (not main_frame or not valid_work_url(url)):
                if main_frame:
                    self.error_code = "network_policy_blocked"
                return False
            if host not in self._hosts:
                answers = self.resolver(host, 443, type=socket.SOCK_STREAM)
                self._hosts[host] = bool(answers) and all(ipaddress.ip_address(answer[4][0]).is_global for answer in answers)
            if not self._hosts[host] and navigation and main_frame:
                self.error_code = "blocked_private_dns"
            return self._hosts[host]
        except (OSError, ValueError, TypeError):
            if navigation and main_frame:
                self.error_code = "network_policy_blocked"
            return False


# DOM 选择器依据 2026-09-27 实页核验：detail-video-info 容器同时声明作品 ID。
# CSS line-clamp 截断不影响 textContent；不读取推荐 H3 或任意全局 h1。
_PAGE_DATA_SCRIPT = r"""() => {
  const payloads = [];
  for (const key of ['_ROUTER_DATA','__ROUTER_DATA','_SSR_DATA','__SSR_DATA']) {
    try { if (window[key]) payloads.push(window[key]); } catch (_) {}
  }
  for (const script of document.querySelectorAll('script#RENDER_DATA,script#__NEXT_DATA__,script[type="application/ld+json"]')) {
    const text = script.textContent || '';
    if (text.length > 2000000) continue;
    try { payloads.push(JSON.parse(script.id === 'RENDER_DATA' ? decodeURIComponent(text) : text)); } catch (_) {}
  }
  const titles = [];
  for (const container of document.querySelectorAll('[data-e2e="detail-video-info"][data-e2e-aweme-id]')) {
    const h1 = container.querySelector('h1');
    if (h1) titles.push({item_id: container.getAttribute('data-e2e-aweme-id'), title: h1.textContent});
  }
  return {payloads, titles};
}"""


def collect_title(page, url, deadline, captured=None, clock=None, stop_requested=None, diagnostics=None):
    """结构化标题可立即返回；DOM 等待稳定；仅明确的暂时 HTTP 错误重试一次。"""
    clock = clock or time.monotonic
    captured = captured if captured is not None else []
    diagnostics = diagnostics if diagnostics is not None else {}
    best, stable_key, stable_since = None, None, None
    started = clock()
    expected = work_identity(url)[1]
    # 给 page.close 与 IPC 留出预算；外层父进程仍有硬截止兜底。
    stop_at = deadline - 0.2
    diagnostics.setdefault("reload_count", 0)
    navigation_failed = False
    try:
        response = page.goto(url, wait_until="commit", timeout=max(1, min(2000, int((stop_at - clock()) * 1000))))
        status = getattr(response, "status", 0)
        # 超时可能仍在正常水合，429 必须尊重限流；两者均不触发重载。
        if (status == 408 or 500 <= status <= 599) and stop_at - clock() >= 1.5:
            if not (stop_requested and stop_requested()):
                diagnostics["reload_count"] = 1
                response = page.reload(wait_until="commit", timeout=max(1, min(1500, int((stop_at - clock()) * 1000))))
                status = getattr(response, "status", 0)
        navigation_failed = bool(status >= 400)
        if status == 429:
            diagnostics["error_code"] = "upstream_rate_limited"
    except Exception:
        # 不输出含 URL/token 的异常；页面可能已 commit，继续在原预算内观察。
        navigation_failed = True
    diagnostics["navigation_s"] = round(max(0.0, clock() - started), 6)

    def observe(candidate):
        if candidate and "first_title_s" not in diagnostics:
            diagnostics["first_title_s"] = round(max(0.0, clock() - started), 6)
        return candidate

    while clock() < stop_at:
        if stop_requested and stop_requested():
            diagnostics.setdefault("error_code", "network_policy_blocked")
            return None
        kind, item_id = work_identity(page.url)
        if item_id and expected and item_id != expected:
            diagnostics["error_code"] = "identity_mismatch"
            return None
        if item_id:
            # 网络响应不依赖 DOM evaluate 成功，避免水合/导航中的 JS 错误拖延已取得的标题。
            structured = observe(extract_structured(list(captured), item_id, kind))
            best = pick_result(best, structured)
            if structured and structured["title_status"] in ("available", "complete"):
                diagnostics["error_code"] = ""
                return best
            try:
                data = page.evaluate(_PAGE_DATA_SCRIPT)
                payloads = data.get("payloads", []) if isinstance(data, dict) else []
                structured = observe(extract_structured(payloads, item_id, kind))
                best = pick_result(best, structured)
                if structured and structured["title_status"] in ("available", "complete"):
                    diagnostics["error_code"] = ""
                    return best
                current_titles = set()
                for entry in data.get("titles", []) if isinstance(data, dict) else []:
                    if isinstance(entry, dict) and str(entry.get("item_id")) == item_id:
                        candidate = observe(title_result(item_id, kind, entry.get("title"), "browser_dom"))
                        best = pick_result(best, candidate)
                        if candidate:
                            current_titles.add(candidate["title"])
                # DOM 仍需连续读到同作品标题；稳定不代表已证实全文。
                if best and best["title_status"] == "available" and best["title"] in current_titles:
                    if best["title"] != stable_key:
                        stable_key, stable_since = best["title"], clock()
                    elif stable_since is not None and clock() - stable_since >= 0.5:
                        diagnostics["error_code"] = ""
                        return best
                else:
                    stable_key, stable_since = None, None
            except Exception:
                # 本轮 DOM 不可读不能视为标题仍然稳定。
                stable_key, stable_since = None, None
        try:
            page.wait_for_timeout(max(1, min(100, int((stop_at - clock()) * 1000))))
        except Exception:
            diagnostics["error_code"] = "page_closed"
            break
    if best:
        diagnostics["error_code"] = "title_partial" if best["title_status"] == "partial" else ""
    else:
        diagnostics.setdefault("error_code", "navigation_failed" if navigation_failed else "title_not_found")
    return best


class AnonymousBrowserSession:
    """每个 worker 专用的匿名会话；有界复用 context，每条任务隔离 page 与策略。"""
    def __init__(self, browser, max_tasks=32, ttl_seconds=300.0, clock=None):
        self.browser = browser
        self.max_tasks = max(1, min(100, int(max_tasks)))
        self.ttl_seconds = max(1.0, min(1800.0, float(ttl_seconds)))
        self.clock = clock or time.monotonic
        self.context = None
        self.page = None
        self.policy = None
        self._created_at = 0.0
        self._tasks = 0
        self._opening_page = False

    def _route(self, route):
        request = route.request
        navigation = request.is_navigation_request()
        if (self.page is not None and self.policy is not None
                and self.policy.allows(request.url, request.resource_type, navigation,
                                       not navigation or request.frame == self.page.main_frame)):
            route.continue_()
        else:
            route.abort()

    def _new_page(self, other):
        # new_page 的事件可能在方法返回前派发；任务打开页面期间不会执行第三方 JS。
        if not self._opening_page and other != self.page:
            other.close()

    def acquire(self, policy):
        if self.page is not None:
            raise RuntimeError("session_busy")
        if self.context is not None and (self._tasks >= self.max_tasks
                or self.clock() - self._created_at >= self.ttl_seconds):
            self.close()
        reused = self.context is not None
        if self.context is None:
            self.context = self.browser.new_context(service_workers="block", accept_downloads=False,
                viewport={"width": 1280, "height": 900}, locale="zh-CN")
            self.context.on("page", self._new_page)
            self.context.route_web_socket("**/*", lambda websocket: websocket.close())
            # 保留安全拦截；不为了 HTTP 缓存移除官方域名与公网 DNS 限制。
            self.context.route("**/*", self._route)
            self._created_at, self._tasks = self.clock(), 0
        self.policy = policy
        self._opening_page = True
        try:
            self.page = self.context.new_page()
        finally:
            self._opening_page = False
        self._tasks += 1
        return self.page, reused

    def release(self, healthy=True):
        page, self.page, self.policy = self.page, None, None
        try:
            if page is not None:
                page.close()
        finally:
            if not healthy:
                self.close()

    def close(self):
        context, self.context = self.context, None
        self.page, self.policy = None, None
        if context is not None:
            context.close()


def fetch_title(browser, url, deadline, diagnostics=None, session=None):
    diagnostics = diagnostics if diagnostics is not None else {}
    started = time.monotonic()
    if not valid_work_url(url):
        diagnostics["error_code"] = "invalid_url"
        diagnostics["elapsed_s"] = 0.0
        return None
    if deadline - started < 0.4:
        diagnostics["error_code"] = "budget_exhausted"
        diagnostics["elapsed_s"] = 0.0
        return None
    # 保持旧调用隔离行为；main 显式传入 worker 专用会话才复用 context。
    owned_session = session is None
    session = session or AnonymousBrowserSession(browser, max_tasks=1)
    policy, captured, result = NetworkPolicy(), [], None
    diagnostics["response_count"] = 0
    try:
        page, reused = session.acquire(policy)
        diagnostics["context_reused"] = reused
        diagnostics["context_s"] = round(time.monotonic() - started, 6)
        if deadline - time.monotonic() < 0.4:
            diagnostics["error_code"] = "budget_exhausted"
            return None
        page.set_default_timeout(max(1, int((deadline - time.monotonic()) * 1000)))

        def capture_response(response):
            try:
                parsed = urlsplit(response.url)
                if (parsed.scheme != "https" or parsed.hostname not in ("www.douyin.com", "douyin.com", "www.iesdouyin.com")
                        or not re.fullmatch(r"/(?:aweme/v1/web/aweme/detail|web/api/v2/aweme/iteminfo)/?", parsed.path)
                        or len(captured) >= 8 or time.monotonic() >= deadline - 0.4):
                    return
                diagnostics["response_count"] += 1
                if response.status != 200 or int(response.headers.get("content-length") or 0) > 2000000:
                    return
                body = response.body()
                if len(body) <= 2000000:
                    payload = json.loads(body)
                    # 有错误状态的 JSON 不得被其中残留的作品字段误判为成功。
                    if isinstance(payload, dict) and payload.get("status_code", 0) in (0, "0", None):
                        captured.append(payload)
            except Exception:
                pass

        page.on("response", capture_response)
        collect_started = time.monotonic()
        result = collect_title(page, url, deadline, captured, stop_requested=lambda: bool(policy.error_code),
                               diagnostics=diagnostics)
        if result and deadline - time.monotonic() > 0.3:
            # 只读取本 worker 匿名页面的 UA，与其游客 Cookie 配套；不输出到 IPC。
            try:
                page.set_default_timeout(max(1, min(200, int((deadline - time.monotonic() - 0.2) * 1000))))
                user_agent = page.evaluate("navigator.userAgent")
                if isinstance(user_agent, str) and 0 < len(user_agent) <= 1024:
                    session.user_agent = user_agent
            except Exception:
                pass
        diagnostics["collect_s"] = round(time.monotonic() - collect_started, 6)
        if "first_title_s" in diagnostics:
            diagnostics["first_title_s"] = round(diagnostics["first_title_s"] + collect_started - started, 6)
        if policy.error_code:
            diagnostics["error_code"] = policy.error_code
        return result
    except Exception:
        diagnostics["error_code"] = "browser_operation_failed"
        return None
    finally:
        cleanup_started = time.monotonic()
        try:
            session.release(healthy=bool(result and not policy.error_code))
            if owned_session:
                session.close()
        except Exception:
            diagnostics["error_code"] = "context_cleanup_failed"
            try:
                session.close()
            except Exception:
                pass
        diagnostics["cleanup_s"] = round(time.monotonic() - cleanup_started, 6)
        diagnostics["elapsed_s"] = round(time.monotonic() - started, 6)


def browser_launch_options(proxy_url=""):
    """只读取服务显式配置的代理；不继承通用 HTTP(S)_PROXY 的隐式语义。"""
    options = {"headless": True, "timeout": 10000,
               "args": ["--disable-background-networking", "--dns-prefetch-disable", "--disable-quic",
                        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]}
    if proxy_url:
        try:
            parsed = urlsplit(proxy_url)
            if (parsed.scheme not in ("http", "https", "socks5") or not parsed.hostname
                    or (parsed.scheme == "socks5" and (parsed.username or parsed.password)) or parsed.path not in ("", "/")
                    or parsed.query or parsed.fragment or parsed.port == 0
                    or any(character.isspace() for character in proxy_url) or len(proxy_url) > 4096):
                raise ValueError("invalid_proxy_config")
            username, password = unquote(parsed.username or ""), unquote(parsed.password or "")
            if any(ord(c) < 32 for c in username + password):
                raise ValueError("invalid_proxy_config")
        except (ValueError, TypeError):
            raise ValueError("invalid_proxy_config") from None
        host = "[" + parsed.hostname + "]" if ":" in parsed.hostname else parsed.hostname
        server = parsed.scheme + "://" + host + (":" + str(parsed.port) if parsed.port else "")
        options["proxy"] = {"server": server}
        if username:
            options["proxy"].update(username=username, password=password)
    return options


class MetadataPipeline:
    """常驻 HTTP 优先；游客初始化/失效时在同一截止时间内由网页补充。"""
    def __init__(self, browser, session, proxy="", http_enabled=True, http_client=None,
                 browser_fetch=None, clock=None):
        from tools.metadata_http import MetadataHTTPClient
        self.browser, self.session = browser, session
        self.clock, self.browser_fetch = clock or time.monotonic, browser_fetch or fetch_title
        self.loop = asyncio.new_event_loop()
        self.http = http_client or MetadataHTTPClient(proxy=proxy)
        # sync_playwright 的 greenlet 已占用当前线程的 asyncio loop。
        # HTTP 连接池和 loop 固定在专用线程，不能在浏览器线程 run_until_complete。
        self.http_thread = threading.Thread(target=self._run_http_loop, daemon=True, name="metadata-http")
        self.http_thread.start()
        self.http_enabled, self.guest_ready = http_enabled, False
        self.guest_at, self.guest_requests = 0.0, 0
        try:
            self.http_budget = min(4.0, max(0.2, float(os.environ.get("HTTP_BUDGET_SECONDS", "1.5"))))
        except ValueError:
            self.http_budget = 1.5

    def _run_http_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _set_guest_session(self, cookies, user_agent):
        async def update():
            self.http.set_guest_session(cookies, user_agent)
        future = asyncio.run_coroutine_threadsafe(update(), self.loop)
        future.result(timeout=0.5)

    def _http_fetch(self, url, deadline, diagnostics):
        diagnostics["http_attempted"] = True
        future = None
        try:
            future = asyncio.run_coroutine_threadsafe(self.http.fetch(url, deadline), self.loop)
            response = future.result(timeout=max(0.001, deadline - self.clock()))
        except FutureTimeoutError:
            if future is not None:
                future.cancel()
            diagnostics["http_error_code"] = "deadline_exceeded"
            return None
        except Exception:
            diagnostics["http_error_code"] = "http_request_failed"
            return None
        for key, value in response.get("timings", {}).items():
            if key in ("resolve_s", "sign_s", "http_s", "decode_s", "client_setup_s"):
                diagnostics[key] = value
        code = response.get("error_code", "")
        # SDK 内部异常从不跨进程传递；具体失败由模块的固定错误码表达。
        diagnostics["http_error_code"] = code
        if code in ("dependency_unavailable", "dependency_version_mismatch"):
            self.http_enabled = False
        result = self._validated(response.get("result"), url)
        if result:
            diagnostics["http_success"] = True
        return result

    @staticmethod
    def _validated(result, url, expected=None):
        if not isinstance(result, dict):
            return None
        identity = (result.get("kind"), result.get("item_id"))
        target = expected or work_identity(url)
        if (not identity[1] or work_identity(result.get("_link")) != identity
                or (target[1] and target != identity)
                or result.get("title_source") not in ("http_structured", "http_ssr", "browser_dom", "browser_structured")
                or result.get("title_status") not in ("available", "partial", "complete")
                or not clean_title(result.get("title"))):
            return None
        return result

    def fetch(self, url, deadline, diagnostics):
        started = self.clock()
        if not valid_work_url(url):
            diagnostics["error_code"] = "invalid_url"
            return None
        if deadline - started < 0.4:
            diagnostics["error_code"] = "budget_exhausted"
            return None
        # HTTP 命中时也检查游客寿命；不能因不再打开网页而无限保留旧会话。
        if self.guest_ready and (started - self.guest_at >= 300 or self.guest_requests >= 64):
            self.session.close()
            self.guest_ready = False
            self._set_guest_session({}, self.http.user_agent)
        best = None
        if self.http_enabled and self.guest_ready:
            self.guest_requests += 1
            # 留至少 2 秒给网页后备；总预算由父进程继续硬约束。
            http_deadline = min(deadline - 2.0, self.clock() + self.http_budget)
            if http_deadline > self.clock() + 0.1:
                best = self._http_fetch(url, http_deadline, diagnostics)
                if best and best.get("title_status") != "partial":
                    diagnostics.update(error_code="", first_title_s=round(self.clock() - started, 6),
                                       elapsed_s=round(self.clock() - started, 6))
                    return best
        browser_started = self.clock()
        browser_diagnostics = {}
        browser_result = self._validated(
            self.browser_fetch(self.browser, url, deadline, browser_diagnostics, session=self.session), url,
            (best["kind"], best["item_id"]) if best else None)
        diagnostics.update(browser_diagnostics)
        diagnostics["browser_s"] = round(self.clock() - browser_started, 6)
        diagnostics["browser_fallback"] = True
        if "first_title_s" in browser_diagnostics:
            diagnostics["first_title_s"] = round(browser_diagnostics["first_title_s"] + browser_started - started, 6)
        best = pick_result(best, browser_result)
        if self.http_enabled and self.session.context is not None and deadline - self.clock() > 0.6:
            try:
                cookies = self.session.context.cookies()
                user_agent = getattr(self.session, "user_agent", "")
                if user_agent:
                    self._set_guest_session(cookies, user_agent)
                    self.guest_ready = bool(self.http.cookies)
                    self.guest_at, self.guest_requests = self.clock(), 0
            except Exception:
                diagnostics["http_error_code"] = "guest_session_unavailable"
        # DOM 先返回时，剩余预算内尝试结构化详情，补作者、日期及统计。
        if (self.http_enabled and self.guest_ready and best
                and (best.get("title_source") == "browser_dom" or best.get("title_status") == "partial")
                and deadline - self.clock() > 0.6):
            extra = self._http_fetch(best["_link"], min(deadline - 0.2, self.clock() + self.http_budget), diagnostics)
            best = pick_result(best, extra)
        if best:
            diagnostics["error_code"] = "title_partial" if best.get("title_status") == "partial" else ""
        diagnostics["elapsed_s"] = round(self.clock() - started, 6)
        return best

    def close(self):
        try:
            future = asyncio.run_coroutine_threadsafe(self.http.close(), self.loop)
            future.result(timeout=1.0)
        except Exception:
            pass
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.http_thread.join(timeout=1.0)
            if not self.http_thread.is_alive():
                self.loop.close()


def main():
    try:
        options = browser_launch_options(os.environ.get("BROWSER_TITLE_PROXY_URL", "").strip())
    except ValueError:
        print(json.dumps({"ready": False, "error_code": "invalid_proxy_config"}), flush=True)
        return
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as driver:
            browser = driver.chromium.launch(**options)
            session = AnonymousBrowserSession(browser)
            pipeline = MetadataPipeline(browser, session, proxy=os.environ.get("BROWSER_TITLE_PROXY_URL", "").strip(),
                                        http_enabled=os.environ.get("METADATA_HTTP_ENABLED", "1").lower() not in ("0", "false", "off"))
            print(json.dumps({"ready": True}), flush=True)
            try:
                for line in sys.stdin:
                    result, diagnostics = None, {}
                    try:
                        task = json.loads(line)
                        deadline = min(float(task["deadline"]), time.monotonic() + 15.0)
                        result = pipeline.fetch(task["url"], deadline, diagnostics)
                    except Exception:
                        diagnostics["error_code"] = "invalid_task"
                    print(json.dumps({"result": result, "error_code": diagnostics.get("error_code", ""),
                                      "diagnostics": diagnostics}, ensure_ascii=False), flush=True)
            finally:
                pipeline.close()
                session.close()
                browser.close()
    except Exception:
        # 原始浏览器异常可能含 URL、token 和环境路径，不跨进程输出。
        print(json.dumps({"ready": False, "error_code": "browser_unavailable"}), flush=True)


if __name__ == "__main__":
    main()
