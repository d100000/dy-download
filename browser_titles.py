"""有界、可选的浏览器标题补全池；不导入 server，也不等待媒体解析。

每个管理线程只拥有一个长期子进程。任务截止时间覆盖排队、导航与刷新；
Playwright 卡住时由父进程终止整个进程组，不能拖住 FastAPI 的工作线程。
"""
import importlib.util
from collections import deque
import json
import math
import os
from pathlib import Path
import queue
import re
import select
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit


_WORK_HOSTS = frozenset(("douyin.com", "www.douyin.com", "iesdouyin.com", "www.iesdouyin.com"))
_ERROR_CODES = frozenset((
    "disabled", "dependency_missing", "browser_unavailable", "queue_timeout",
    "worker_exit", "worker_timeout", "worker_error", "invalid_proxy_config",
    "invalid_url", "budget_exhausted", "network_policy_blocked", "blocked_private_dns",
    "identity_mismatch", "upstream_rate_limited", "navigation_failed", "title_not_found",
    "page_closed", "title_partial", "browser_operation_failed", "context_cleanup_failed",
    "invalid_task",
))
_DIAGNOSTIC_SECONDS = frozenset((
    "context_s", "navigation_s", "first_title_s", "collect_s", "cleanup_s", "elapsed_s",
    "queue_s", "startup_s", "worker_s", "resolve_s", "sign_s", "detail_s",
    "session_s", "http_s", "ssr_s", "browser_s", "client_setup_s", "decode_s",
))
_HTTP_ERROR_CODES = frozenset((
    "dependency_unavailable", "dependency_version_mismatch", "unexpected_sdk_endpoint",
    "unexpected_api_redirect", "response_too_large", "upstream_failed", "invalid_json",
    "invalid_proxy", "invalid_user_agent", "untrusted_url", "dns_failed", "blocked_private_dns",
    "blocked_network_policy", "client_closed", "shortlink_no_redirect", "shortlink_invalid_target",
    "shortlink_redirect_limit", "upstream_status", "missing_detail", "identity_mismatch",
    "kind_mismatch", "missing_title", "invalid_work_url", "deadline_exceeded",
    "http_request_failed", "guest_session_unavailable",
    "upstream_business_error", "upstream_network_error", "upstream_risk_control",
))
_CHILD_ENV_KEYS = frozenset((
    "PATH", "HOME", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TZ",
    "TMPDIR", "TMP", "TEMP", "DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "XDG_CACHE_HOME",
    "PLAYWRIGHT_BROWSERS_PATH", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
    "DYLD_FALLBACK_LIBRARY_PATH", "METADATA_HTTP_ENABLED", "HTTP_BUDGET_SECONDS",
    "BROWSER_TITLE_CONTEXT_TTL", "METADATA_CONTEXT_TTL_SECONDS",
))
_MAX_MESSAGE_BYTES = 256 * 1024


def _safe_error_code(value, default=""):
    return value if isinstance(value, str) and value in _ERROR_CODES else default


def _safe_diagnostics(value):
    """管理状态只保留已知计时/计数字段，不接受页面、URL、异常或凭据。"""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in _DIAGNOSTIC_SECONDS:
        number = value.get(key)
        if (isinstance(number, (float, int)) and not isinstance(number, bool)
                and 0 <= number <= 3600 and math.isfinite(number)):
            result[key] = round(float(number), 6)
    for key in ("response_count", "reload_count"):
        number = value.get(key)
        if isinstance(number, int) and not isinstance(number, bool) and 0 <= number <= 100000:
            result[key] = number
    for key in ("context_reused", "http_attempted", "http_success", "browser_fallback"):
        if isinstance(value.get(key), bool):
            result[key] = value[key]
    if _safe_error_code(value.get("error_code")):
        result["error_code"] = value["error_code"]
    if (isinstance(value.get("http_error_code"), str)
            and value["http_error_code"] in _HTTP_ERROR_CODES):
        result["http_error_code"] = value["http_error_code"]
    return result


class _WorkerFailure(RuntimeError):
    def __init__(self, code):
        self.code = _safe_error_code(code, "worker_error")
        super().__init__(self.code)


def work_identity(url):
    """只认可官方 HTTPS 作品路径，返回 (kind, item_id)。"""
    try:
        parsed = urlsplit(str(url or ""))
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.hostname not in _WORK_HOSTS):
            return "", ""
        match = re.fullmatch(r"/(?:share/)?(video|note|slides)/(\d{8,30})/?", parsed.path)
        if match:
            return ("video" if match[1] == "video" else "note"), match[2]
    except (TypeError, ValueError):
        pass
    return "", ""


def valid_work_url(url):
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 32 for c in url):
        return False
    if work_identity(url)[1]:
        return True
    try:
        parsed = urlsplit(url)
        return bool(parsed.scheme == "https" and parsed.hostname == "v.douyin.com"
                    and not parsed.username and not parsed.password
                    and parsed.port in (None, 443)
                    and re.fullmatch(r"/[A-Za-z0-9_-]{1,128}/?", parsed.path))
    except (TypeError, ValueError):
        return False


def _dependency_available():
    return importlib.util.find_spec("playwright") is not None


class BrowserTitleService:
    def __init__(self, enabled=False, timeout=6.0, workers=2, max_queue=8,
                 startup_timeout=12.0, python_executable=None, proxy=None):
        self.enabled = bool(enabled)
        self.timeout = min(15.0, max(0.1, float(timeout)))
        self.workers = min(2, max(1, int(workers)))
        self.startup_timeout = min(30.0, max(0.1, float(startup_timeout)))
        configured_python = (python_executable if python_executable is not None
                             else os.environ.get("BROWSER_TITLE_PYTHON", ""))
        self.python_executable = str(configured_python or sys.executable)
        self._independent_python = bool(configured_python)
        self._proxy = str(proxy if proxy is not None else os.environ.get("BROWSER_TITLE_PROXY_URL", ""))
        self._queue = queue.Queue(maxsize=min(64, max(1, int(max_queue))))
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._threads = []
        self._processes = set()
        self._ready = 0
        self._active = 0
        self._started = False
        self._unavailable = not self.enabled
        self._error_code = "disabled" if not self.enabled else ""
        self._diagnostics = {}
        self._completed = 0
        self._succeeded = 0
        self._recent = deque(maxlen=32)
        self._command = [self.python_executable, "-u",
                         str(Path(__file__).resolve().parent / "tools" / "browser_title_worker.py")]

    @property
    def proxy(self):
        return self._proxy

    def _child_environment(self):
        # 子进程无需应用密钥、数据库配置、个人 Cookie 或隐式系统代理。
        environment = {key: value for key, value in os.environ.items() if key in _CHILD_ENV_KEYS}
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        if self._proxy:
            environment["BROWSER_TITLE_PROXY_URL"] = self._proxy
        return environment

    def start(self):
        """异步预热；独立 Python 的依赖由子进程握手确认。"""
        with self._lock:
            if self._started or self._stopped.is_set() or self._unavailable:
                return
            self._started = True
            if not self._independent_python and not _dependency_available():
                self._unavailable, self._error_code = True, "dependency_missing"
                return
            self._threads = [threading.Thread(target=self._run, daemon=True,
                             name="browser-title-%d" % i) for i in range(self.workers)]
            self._threads.append(threading.Thread(target=self._watch_queue, daemon=True,
                                                  name="browser-title-deadlines"))
            for thread in self._threads:
                thread.start()

    def submit(self, url, callback):
        """立即入队；已接收任务最终 callback(dict|None)，拒绝任务无回调。"""
        if not valid_work_url(url) or not callable(callback):
            return False
        self.start()
        with self._lock:
            if self._unavailable or self._stopped.is_set():
                return False
            accepted_at = time.monotonic()
            try:
                self._queue.put_nowait((url, callback, accepted_at, accepted_at + self.timeout))
            except queue.Full:
                return False
        return True

    def status(self):
        with self._lock:
            return {"enabled": self.enabled, "available": not self._unavailable and not self._stopped.is_set(),
                    "ready": self._ready, "workers": self.workers,
                    "pending": self._queue.qsize() + self._active,
                    "error_code": self._error_code, "diagnostics": dict(self._diagnostics),
                    "completed": self._completed, "succeeded": self._succeeded,
                    "failed": self._completed - self._succeeded,
                    "recent": [dict(entry, diagnostics=dict(entry["diagnostics"])) for entry in self._recent],
                    "python_configured": self._independent_python,
                    "proxy_configured": bool(self._proxy)}

    def _finish(self, callback, result, error_code="", diagnostics=None):
        # 只在任务终态计数；预热和中间诊断不会创建历史项。
        self._record(error_code, diagnostics, task_succeeded=isinstance(result, dict))
        try:
            callback(result)
        except Exception:
            # 标题是可选补全；业务回调失败不可杀掉整个 worker。
            pass

    def _record(self, error_code="", diagnostics=None, task_succeeded=None):
        safe = _safe_diagnostics(diagnostics)
        code = _safe_error_code(error_code)
        if code:
            safe["error_code"] = code
        else:
            safe.pop("error_code", None)
        with self._lock:
            self._error_code, self._diagnostics = code, safe
            if task_succeeded is not None:
                self._completed += 1
                self._succeeded += int(task_succeeded)
                self._recent.append({"seq": self._completed,
                    "status": "succeeded" if task_succeeded else "failed",
                    "error_code": code, "diagnostics": dict(safe)})

    @staticmethod
    def _failure_code(error):
        if isinstance(error, _WorkerFailure):
            return error.code
        if isinstance(error, TimeoutError):
            return "worker_timeout"
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return "worker_exit"
        return "worker_error"

    def _read(self, process, deadline):
        # os.read 有硬截止；跨调用保留尾部，避免一次写出的 diagnostics/result 丢包。
        buffer = process._title_read_buffer
        while not self._stopped.is_set() and time.monotonic() < deadline:
            if b"\n" in buffer:
                line, _, rest = buffer.partition(b"\n")
                buffer[:] = rest
                if len(line) > _MAX_MESSAGE_BYTES:
                    raise _WorkerFailure("worker_error")
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    raise _WorkerFailure("worker_error")
                if not isinstance(message, dict):
                    raise _WorkerFailure("worker_error")
                return message
            if len(buffer) > _MAX_MESSAGE_BYTES:
                raise _WorkerFailure("worker_error")
            remaining = max(0.0, deadline - time.monotonic())
            if not select.select([process.stdout], [], [], min(remaining, 0.1))[0]:
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise _WorkerFailure("worker_exit")
            buffer.extend(chunk)
        raise TimeoutError("worker_timeout")

    def _message(self, process, deadline, expected, diagnostics):
        while True:
            message = self._read(process, deadline)
            safe = _safe_diagnostics(message.get("diagnostics"))
            for key in ("queue_s", "startup_s", "worker_s"):
                safe.pop(key, None)  # 父进程阶段耗时只由父进程计时。
            diagnostics.update(safe)
            if expected in message:
                return message
            if set(message) != {"diagnostics"}:
                raise _WorkerFailure("worker_error")

    def _kill(self, process):
        if process is None:
            return
        with self._lock:
            if process not in self._processes:
                return
            self._processes.discard(process)
        try:
            # Chromium 子进程也必须一起回收；worker 独立 session，不会杀到应用。
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        for stream in (process.stdin, process.stdout):
            if stream:
                stream.close()

    def _spawn(self, deadline=None):
        started_at = time.monotonic()
        process = subprocess.Popen(self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0,
                                   env=self._child_environment())
        process._title_read_buffer = bytearray()
        process._title_startup_diagnostics = {}
        with self._lock:
            self._processes.add(process)
        try:
            startup_deadline = started_at + self.startup_timeout
            message = self._message(process, min(startup_deadline, deadline) if deadline is not None
                                    else startup_deadline, "ready", process._title_startup_diagnostics)
            process._title_startup_diagnostics["startup_s"] = time.monotonic() - started_at
            if message.get("ready") is not True:
                code = message.get("error_code")
                raise _WorkerFailure(_safe_error_code(code, "browser_unavailable"))
            return process
        except Exception:
            self._kill(process)
            raise

    def _expire_pending(self):
        # 所有槽位退避时仍及时结束过期任务。队列 FIFO 且同预算，截止时间有序。
        # 使用 Queue 自身 mutex 原子移除队头，避免取出再放回改变顺序/突破容量。
        expired = []
        with self._queue.mutex:
            while self._queue.queue and self._queue.queue[0][3] <= time.monotonic():
                expired.append(self._queue.queue.popleft())
                self._queue.not_full.notify()
        for _, callback, accepted_at, _ in expired:
            self._finish(callback, None, "queue_timeout", {"queue_s": time.monotonic() - accepted_at})
            self._queue.task_done()

    def _watch_queue(self):
        # 预热握手可长于任务预算；独立检查避免所有槽位启动中时队列越过截止。
        while not self._stopped.wait(0.02):
            self._expire_pending()

    def _run(self):
        process = None
        failures = 0
        retry_at = 0.0
        try:
            initial_started_at = time.monotonic()
            try:
                process = self._spawn()
                with self._lock:
                    self._ready += 1
                self._record(diagnostics=process._title_startup_diagnostics)
            except Exception as error:
                failures = 1
                self._record(self._failure_code(error), {"startup_s": time.monotonic() - initial_started_at})
                # 仅此槽位失败；其他 worker 的进程和队列不受影响。
            while not self._stopped.is_set():
                if time.monotonic() < retry_at:
                    self._stopped.wait(min(0.05, retry_at - time.monotonic()))
                    continue
                try:
                    url, callback, accepted_at, deadline = self._queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                with self._lock:
                    self._active += 1
                result = None
                code = "worker_exit"
                diagnostics = {"queue_s": time.monotonic() - accepted_at, "startup_s": 0.0}
                worker_started_at = None
                spawn_started_at = None
                try:
                    if self._stopped.is_set():
                        continue
                    if time.monotonic() >= deadline:
                        code = "queue_timeout"
                        continue
                    if process is None:
                        # 没有任务时不重启；启动与处理共享本次任务的剩余预算。
                        spawn_started_at = time.monotonic()
                        process = self._spawn(deadline)
                        diagnostics.update(process._title_startup_diagnostics)
                        with self._lock:
                            self._ready += 1
                    if time.monotonic() >= deadline:
                        raise TimeoutError("worker_timeout")
                    worker_started_at = time.monotonic()
                    process.stdin.write((json.dumps({"url": url, "deadline": deadline}) + "\n").encode())
                    process.stdin.flush()
                    message = self._message(process, deadline, "result", diagnostics)
                    if time.monotonic() > deadline:
                        raise TimeoutError("worker_timeout")
                    candidate = message["result"]
                    if candidate is not None and not isinstance(candidate, dict):
                        raise _WorkerFailure("worker_error")
                    result = candidate
                    code = message.get("error_code") or diagnostics.get("error_code", "")
                    code = _safe_error_code(code, "worker_error" if result is None else "")
                    diagnostics["worker_s"] = time.monotonic() - worker_started_at
                    failures = 0
                except Exception as error:
                    result = None
                    was_ready = process is not None
                    self._kill(process)
                    process = None
                    if worker_started_at is not None:
                        diagnostics["worker_s"] = time.monotonic() - worker_started_at
                    elif spawn_started_at is not None:
                        diagnostics["startup_s"] = time.monotonic() - spawn_started_at
                    with self._lock:
                        if was_ready:
                            self._ready = max(0, self._ready - 1)
                    code = "worker_exit" if self._stopped.is_set() else self._failure_code(error)
                    failures += 1
                    retry_at = time.monotonic() + min(3.0, 0.1 * (2 ** min(failures - 1, 5)))
                finally:
                    self._finish(callback, result, code, diagnostics)
                    self._queue.task_done()
                    with self._lock:
                        self._active -= 1
        finally:
            if process is not None:
                self._kill(process)
                with self._lock:
                    self._ready = max(0, self._ready - 1)

    def close(self):
        self._stopped.set()
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            self._kill(process)
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1.5)
        # 队列归服务所有；一个失败槽位不得清空健康槽位的待办。
        while True:
            try:
                _, callback, _, _ = self._queue.get_nowait()
            except queue.Empty:
                break
            self._finish(callback, None, "worker_exit")
            self._queue.task_done()
