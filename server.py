#!/usr/bin/env python3
"""抖音无水印下载器 · Web 服务版（含代理池 + 管理后台）

免登录、免签名。后端负责：短链解析 → 分享页元数据提取 → 无水印地址还原，
并以流式代理（支持 Range）转发视频/图片，绕过抖音 CDN 的 UA / 防盗链限制，
让浏览器可以直接在线播放与下载。

反封锁能力：
  · 代理 IP 池（http/https/socks5），所有出站请求轮换走代理
  · 失败自动转移到下一个代理 + 失败计数退避
  · 移动端 UA 池轮换 + Referer 伪装
  · 管理后台（密码鉴权）增删/启停/测试代理、查看出口 IP 与统计

启动:  uvicorn server:app --host 0.0.0.0 --port 8000
环境变量:  ADMIN_PASSWORD  管理后台密码（默认 douyin-admin，生产务必修改）
"""

import io
import json
import os
import random
import re
import secrets
import threading
import time
import zipfile
from pathlib import Path
from typing import Optional
from urllib import error as urlerr
from urllib import parse as urlparse
from urllib import request as urlreq

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

# ---------------------------------------------------------------- 常量与存储

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STORE_FILE = DATA_DIR / "config.json"

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "douyin-admin")

# 移动端 UA 池（轮换降低指纹一致性）
UA_POOL = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; PixeI 7 Build/TQ3A.230805.001) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/118.0.0.0 Mobile Safari/537.36",
]

# 仅允许代理抖音系 CDN，防止服务被当作任意 URL 代理（SSRF）
ALLOWED_HOST_SUFFIXES = (
    "douyinpic.com", "douyinvod.com", "iesdouyin.com", "snssdk.com",
    "douyinstatic.com", "byteimg.com", "ibytedtos.com", "amemv.com",
    "zjcdn.com", "douyincdn.com", "bytecdn.cn", "douyin.com", "pstatp.com",
)

CACHE_TTL = 1800      # 解析结果缓存 30 分钟

# 代理测试目标
TEST_URL_IP = "https://api.ipify.org?format=json"     # 出口 IP
TEST_URL_DOUYIN = "https://www.iesdouyin.com/"        # 抖音可达性

SUPPORTED_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4", "socks4a")

DEFAULT_SETTINGS = {
    "force_proxy": True,          # 无可用代理时拒绝直连（防真实 IP 暴露）
    "default_protocol": "socks5", # 无协议前缀的代理按此协议解析（代理多为 socks5）
    "rotation": "round_robin",    # round_robin | random | least_fail
    "retries": 3,                 # 单个请求最多尝试几个代理后放弃
    "auto_health": True,          # 后台定时健康检查
    "health_interval_min": 10,    # 健康检查间隔（分钟）
    "auto_disable_fail": 5,       # 连续失败达到此数自动禁用（0=不自动禁用）
    "test_reach_douyin": True,    # 测速时附带检测抖音可达
}

_ua_counter = 0


def pick_ua() -> str:
    global _ua_counter
    _ua_counter = (_ua_counter + 1) % len(UA_POOL)
    return UA_POOL[_ua_counter]


# ---------------------------------------------------------------- 代理池管理

class ProxyManager:
    """线程安全的代理池：持久化、轮换、失败计数、统计。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._rr = 0
        self.proxies: list[dict] = []
        self.settings = dict(DEFAULT_SETTINGS)
        self.stats = {"total": 0, "via_proxy": 0, "direct": 0, "retries": 0, "banned": 0}
        self._load()

    # ---- 持久化 ----
    def _load(self):
        if STORE_FILE.exists():
            try:
                d = json.loads(STORE_FILE.read_text("utf-8"))
                self.proxies = d.get("proxies", [])
                self.settings.update(d.get("settings", {}))
            except Exception:
                pass

    def _save(self):
        STORE_FILE.write_text(json.dumps(
            {"proxies": self.proxies, "settings": self.settings},
            ensure_ascii=False, indent=2), "utf-8")

    # ---- 解析：兼容多种代理书写格式 ----
    @staticmethod
    def parse_proxy(raw: str, default_scheme: str = "socks5") -> Optional[str]:
        """把各种常见格式统一成 `scheme://[user:pass@]host:port`。

        支持：
          scheme://user:pass@host:port      scheme://host:port
          user:pass@host:port               host:port
          host:port:user:pass               ip:port（4 段 / 2 段冒号分隔）
          带协议前缀：http/https/socks5/socks5h/socks4/socks4a
        无协议前缀时按 default_scheme（默认 socks5，代理多为 socks5）。
        """
        raw = raw.strip().strip('"\'')
        if not raw:
            return None
        scheme = default_scheme
        m = re.match(r"^(https?|socks5h|socks5|socks4a|socks4)://(.*)$", raw, re.I)
        if m:
            scheme, rest = m.group(1).lower(), m.group(2)
        else:
            rest = raw
        if scheme not in SUPPORTED_SCHEMES:
            return None

        user = pw = None
        if "@" in rest:                                   # user:pass@host:port
            cred, _, hostport = rest.rpartition("@")
            if ":" in cred:
                user, _, pw = cred.partition(":")
            else:
                user = cred
            hp = hostport
        else:
            parts = rest.split(":")
            if len(parts) == 4:                           # host:port:user:pass
                host, port, user, pw = parts
                hp = f"{host}:{port}"
            elif len(parts) == 3:                         # host:port:user
                host, port, user = parts
                hp = f"{host}:{port}"
            else:                                         # host:port
                hp = rest

        hm = re.match(r"^([^:/\s@]+):(\d{1,5})$", hp)
        if not hm or not (0 < int(hm.group(2)) < 65536):
            return None
        host, port = hm.group(1), hm.group(2)
        auth = ""
        if user:
            auth = urlparse.quote(user, safe="")
            if pw:
                auth += ":" + urlparse.quote(pw, safe="")
            auth += "@"
        return f"{scheme}://{auth}{host}:{port}"

    # ---- 增删改 ----
    def add_many(self, raw: str, note: str = "") -> dict:
        default_scheme = self.settings.get("default_protocol", "socks5")
        added, skipped = [], []
        with self._lock:
            existing = {p["url"] for p in self.proxies}
            for line in re.split(r"[\r\n,;]+|\s{2,}", raw.strip()):
                line = line.strip()
                if not line:
                    continue
                url = self.parse_proxy(line, default_scheme)
                if not url:
                    skipped.append(line)
                    continue
                if url in existing:
                    skipped.append(url)
                    continue
                existing.add(url)
                self.proxies.append({
                    "id": secrets.token_hex(4), "url": url, "enabled": True,
                    "auto_off": False, "note": note, "added_at": int(time.time()),
                    "ok": 0, "fail": 0, "last_used": None, "last_ok": None,
                    "latency_ms": None, "exit_ip": None, "douyin_ok": None,
                    "banned": False, "banned_at": None, "banned_reason": None,
                })
                added.append(url)
            self._save()
        return {"added": len(added), "skipped": skipped}

    def remove(self, pid: str) -> bool:
        with self._lock:
            n = len(self.proxies)
            self.proxies = [p for p in self.proxies if p["id"] != pid]
            self._save()
            return len(self.proxies) < n

    def toggle(self, pid: str) -> Optional[bool]:
        with self._lock:
            for p in self.proxies:
                if p["id"] == pid:
                    p["enabled"] = not p["enabled"]
                    p["auto_off"] = False        # 手动操作，取消自动禁用标记
                    if p["enabled"]:
                        p["fail"] = 0
                        p["banned"] = False      # 手动启用即解除封禁标记
                        p["banned_reason"] = None
                    self._save()
                    return p["enabled"]
        return None

    def mark_banned(self, p: dict, reason: str):
        """代理 IP 被抖音封禁：落库标记、自动禁用、计数。"""
        with self._lock:
            p["banned"] = True
            p["banned_at"] = int(time.time())
            p["banned_reason"] = reason
            p["enabled"] = False
            p["auto_off"] = True
            p["fail"] = p.get("fail", 0) + 1
            self.stats["banned"] = self.stats.get("banned", 0) + 1
            self._save()

    def get(self, pid: str) -> Optional[dict]:
        return next((p for p in self.proxies if p["id"] == pid), None)

    def set_setting(self, key: str, val):
        with self._lock:
            self.settings[key] = val
            self._save()

    # ---- 选择与打点 ----
    @property
    def force_proxy(self) -> bool:
        return bool(self.settings.get("force_proxy", True))

    @property
    def retries(self) -> int:
        return max(1, int(self.settings.get("retries", 3)))

    def candidates(self) -> list[dict]:
        """按轮换策略返回本次请求的代理尝试顺序（仅启用中的）。"""
        with self._lock:
            active = [p for p in self.proxies if p["enabled"]]
            if not active:
                return []
            strategy = self.settings.get("rotation", "round_robin")
            if strategy == "random":
                ordered = active[:]
                random.shuffle(ordered)
            elif strategy == "least_fail":
                ordered = sorted(active, key=lambda p: (p["fail"], -p["ok"]))
            else:                                    # round_robin：轮换起点 + 健康优先
                self._rr = (self._rr + 1) % len(active)
                ordered = active[self._rr:] + active[:self._rr]
                ordered.sort(key=lambda p: p["fail"])
            return ordered

    def _auto_disable_if_needed(self, p: dict):
        thr = int(self.settings.get("auto_disable_fail", 5))
        if thr > 0 and p["enabled"] and p["fail"] >= thr:
            p["enabled"] = False
            p["auto_off"] = True

    def mark_ok(self, p: Optional[dict], latency_ms: Optional[int] = None):
        with self._lock:
            self.stats["total"] += 1
            if p is None:
                self.stats["direct"] += 1
                return
            self.stats["via_proxy"] += 1
            p["ok"] += 1
            p["fail"] = 0
            p["last_used"] = p["last_ok"] = int(time.time())
            if latency_ms is not None:
                p["latency_ms"] = latency_ms
            self._save()

    def mark_fail(self, p: dict):
        with self._lock:
            p["fail"] += 1
            p["last_used"] = int(time.time())
            self._auto_disable_if_needed(p)
            self._save()

    def note_retry(self):
        with self._lock:
            self.stats["retries"] += 1

    def record_probe(self, p: dict, ok: bool, latency_ms=None,
                     exit_ip=None, douyin_ok=None):
        """健康检查/手动测试后回写状态，并处理自动禁用 / 自愈。"""
        with self._lock:
            if ok:
                p["fail"] = 0
                p["last_ok"] = int(time.time())
                if latency_ms is not None:
                    p["latency_ms"] = latency_ms
                if exit_ip is not None:
                    p["exit_ip"] = exit_ip
                if douyin_ok is not None:
                    p["douyin_ok"] = douyin_ok
                if p.get("banned"):              # 封禁的代理测通了 → 解封自愈
                    p["banned"] = False
                    p["banned_reason"] = None
                if p.get("auto_off"):            # 自动禁用过的，恢复可用 → 自愈
                    p["enabled"] = True
                    p["auto_off"] = False
            else:
                p["fail"] += 1
                self._auto_disable_if_needed(p)
            self._save()


proxy_mgr = ProxyManager()


# ---------------------------------------------------------------- 开放 API 密钥

API_KEYS_FILE = DATA_DIR / "apikeys.json"


class APIKeyStore:
    """开放 API 的密钥 + 每日配额 + 用量统计（商业化底座）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.keys: list[dict] = []
        self._load()

    def _load(self):
        if API_KEYS_FILE.exists():
            try:
                self.keys = json.loads(API_KEYS_FILE.read_text("utf-8")).get("keys", [])
            except Exception:
                pass

    def _save(self):
        API_KEYS_FILE.write_text(json.dumps({"keys": self.keys}, ensure_ascii=False, indent=2), "utf-8")

    def create(self, name: str, plan: str = "free", quota: int = 100) -> dict:
        rec = {
            "key": "dy_" + secrets.token_urlsafe(24),
            "name": name or "未命名", "plan": plan, "quota": int(quota),
            "used_today": 0, "used_total": 0, "day": int(time.time() // 86400),
            "created": int(time.time()), "last_used": None, "enabled": True,
        }
        with self._lock:
            self.keys.append(rec)
            self._save()
        return rec

    def revoke(self, key: str) -> bool:
        with self._lock:
            n = len(self.keys)
            self.keys = [k for k in self.keys if k["key"] != key]
            self._save()
            return len(self.keys) < n

    def check_and_count(self, key: str):
        """校验 key 并计一次用量。返回 (ok, error_message)。"""
        if not key:
            return False, "缺少 API Key（请在 X-API-Key 头或 ?key= 传入）"
        with self._lock:
            rec = next((k for k in self.keys if k["key"] == key), None)
            if not rec or not rec["enabled"]:
                return False, "无效或已禁用的 API Key"
            day = int(time.time() // 86400)
            if rec.get("day") != day:
                rec["day"], rec["used_today"] = day, 0
            if rec["quota"] > 0 and rec["used_today"] >= rec["quota"]:
                return False, f"今日调用已达上限（{rec['quota']} 次/天），请升级套餐或明日再试"
            rec["used_today"] += 1
            rec["used_total"] += 1
            rec["last_used"] = int(time.time())
            self._save()
            return True, None


apikeys = APIKeyStore()

# 免费网页端按 IP 的每日限额（防滥用，支撑 freemium）
_ip_hits: dict = {}
FREE_IP_DAILY = int(os.environ.get("FREE_IP_DAILY", "300"))


def _ip_rate_ok(ip: str) -> bool:
    if FREE_IP_DAILY <= 0:
        return True
    day = int(time.time() // 86400)
    d, n = _ip_hits.get(ip, (day, 0))
    if d != day:
        d, n = day, 0
    n += 1
    _ip_hits[ip] = (d, n)
    return n <= FREE_IP_DAILY


# ---------------------------------------------------------------- HTTP 出站层

class NoRedirect(urlreq.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _proxy_handler(proxy: dict):
    """根据代理 URL 构造 urllib handler。socks5 走 PySocks。"""
    url = proxy["url"]
    scheme = url.split("://", 1)[0].lower()
    if scheme in ("http", "https"):
        return urlreq.ProxyHandler({"http": url, "https": url})
    if scheme.startswith("socks"):
        import socks
        from sockshandler import SocksiPyHandler
        parts = urlparse.urlsplit(url)
        stype = socks.SOCKS4 if scheme.startswith("socks4") else socks.SOCKS5
        rdns = scheme in ("socks5h", "socks4a")     # 远端解析 DNS，避免 DNS 泄露
        user = urlparse.unquote(parts.username) if parts.username else None
        pw = urlparse.unquote(parts.password) if parts.password else None
        return SocksiPyHandler(stype, parts.hostname, parts.port or 1080,
                               rdns=rdns, username=user, password=pw)
    raise ValueError(f"不支持的代理协议: {scheme}")


def _raw_open(url: str, follow: bool, headers: dict, timeout: int, proxy: Optional[dict]):
    handlers = []
    if proxy:
        handlers.append(_proxy_handler(proxy))
    if not follow:
        handlers.append(NoRedirect())
    opener = urlreq.build_opener(*handlers)
    req = urlreq.Request(url, headers=headers)
    try:
        return opener.open(req, timeout=timeout)
    except urlerr.HTTPError as e:
        if not follow and e.code in (301, 302, 303, 307, 308):
            return e          # 重定向对短链解析而言是"成功"
        raise


def open_url(url: str, follow: bool = True, headers: Optional[dict] = None,
             timeout: int = 30):
    """出站请求核心：一律经代理，失败自动转移。

    所有到抖音的服务器请求都走这里 —— **绝不服务器直连**，避免暴露服务器 IP。
    仅当管理后台关闭「禁止直连」(force_proxy=False) 且无可用代理时，才退回直连。
    返回 (response, proxy_used_or_None)。
    """
    hdrs = {"User-Agent": pick_ua()}
    if headers:
        hdrs.update(headers)

    cands = proxy_mgr.candidates()
    if not cands:                                   # 无可用代理
        if proxy_mgr.force_proxy:
            raise ApiError(503, "没有可用代理，且已开启「禁止服务器直连」——为避免暴露服务器 IP，"
                                "不会直连抖音。请在管理后台添加并启用代理。")
        r = _raw_open(url, follow, hdrs, timeout, None)   # 仅在管理员显式允许时直连
        proxy_mgr.mark_ok(None)
        return r, None

    cands = cands[:proxy_mgr.retries]               # 每请求最多尝试 N 个代理
    errors = []
    for i, p in enumerate(cands):
        if i > 0:
            proxy_mgr.note_retry()                  # 记录一次自动重试（换代理）
        t0 = time.time()
        try:
            r = _raw_open(url, follow, hdrs, timeout, p)
            proxy_mgr.mark_ok(p, int((time.time() - t0) * 1000))
            return r, p
        except urlerr.HTTPError as e:
            if e.code in (403, 401):                # 抖音封禁该代理 IP → 落库+禁用+换代理
                proxy_mgr.mark_banned(p, f"抖音返回 {e.code}，IP 被封禁")
                errors.append(f"{p['url']} → 被封禁(HTTP {e.code})")
                continue
            # 其他 4xx/5xx 是源站问题，不怪代理，直接上抛
            proxy_mgr.mark_ok(p, int((time.time() - t0) * 1000))
            raise
        except Exception as e:                      # 连接/超时 → 代理故障，自动转移
            msg = str(e).lower()
            if "403" in msg or "forbidden" in msg or "tunnel connection failed" in msg:
                proxy_mgr.mark_banned(p, "代理无法连接抖音（403/被封禁）")
                errors.append(f"{p['url']} → 被封禁(403)")
            else:
                proxy_mgr.mark_fail(p)
                errors.append(f"{p['url']} → {type(e).__name__}: {e}")

    # 所有代理都连不通
    if proxy_mgr.force_proxy:
        raise ApiError(502, "全部代理均不可用，且已禁止服务器直连抖音（防止暴露服务器 IP）。"
                            "请在管理后台检查代理。明细：" + " | ".join(errors[:3]))
    r = _raw_open(url, follow, hdrs, timeout, None)   # 仅在管理员显式允许时直连
    proxy_mgr.mark_ok(None)
    return r, None


# ---------------------------------------------------------------- 工具函数

app = FastAPI(title="抖音无水印下载器")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


@app.exception_handler(ApiError)
async def _api_error(_: Request, exc: ApiError):
    return JSONResponse(status_code=exc.status, content={"error": exc.message})


def _host_allowed(url: str) -> bool:
    if not url.startswith(("http://", "https://")):
        return False
    host = urlparse.urlsplit(url).hostname or ""
    return any(host == s or host.endswith("." + s) for s in ALLOWED_HOST_SUFFIXES)


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            yield obj[key]
        for v in obj.values():
            yield from _find_key(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _find_key(v, key)


def _safe_name(desc: str, fallback: str) -> str:
    name = re.sub(r"#\S+", "", desc).strip()
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_")
    return (name or fallback)[:60]


def _proxy_url(upstream: str, name: str = "", download: bool = False) -> str:
    q = {"url": upstream}
    if name:
        q["name"] = name
    if download:
        q["dl"] = "1"
    return "/api/media?" + urlparse.urlencode(q)


def _stream(resp, chunk=256 * 1024):
    try:
        while True:
            block = resp.read(chunk)
            if not block:
                break
            yield block
    finally:
        resp.close()


def _content_disposition(name: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|]+', "_", name)[:80]
    return f"attachment; filename*=UTF-8''{urlparse.quote(safe)}"


# ---------------------------------------------------------------- 核心解析

_cache: dict = {}
_author_cache: dict = {}          # item_id -> (ts, 作者结构化详情)
CDN_HEADERS = {"Referer": "https://www.douyin.com/"}


def _play_api(vid: str) -> str:
    """无水印播放接口地址。交给用户浏览器直接请求：

    浏览器 GET 该地址 → 302 → 跟随到 CDN 直链（按浏览器自身 IP/地区解析）→ 播放。
    这样视频字节不经过本服务器（省带宽、不暴露服务器 IP），且 CDN 直链与浏览器
    同 IP，避免"服务器/代理 IP 解析的直链换个 IP 打不开"的问题。
    实测该接口对桌面 UA / 无 UA 均返回 200，浏览器可直连。
    """
    return f"https://aweme.snssdk.com/aweme/v1/play/?video_id={vid}&ratio=720p&line=0"


def _parse_share(text: str) -> dict:
    m = re.search(r"https://v\.douyin\.com/[\w-]+/?", text)
    if not m:
        raise ApiError(400, "未找到抖音分享链接，请确认文案里包含 v.douyin.com 短链")
    short = m.group(0)

    try:
        resp, _ = open_url(short, follow=False)
    except ApiError:
        raise
    except Exception:
        raise ApiError(502, "短链请求失败，请检查网络/代理后重试")
    location = resp.headers.get("Location", "") if hasattr(resp, "headers") else ""
    km = re.search(r"/share/(video|note|slides)/(\d+)", location)
    if not km:
        if "/share/live" in location:
            raise ApiError(400, "这是直播分享链接，暂不支持下载直播内容")
        raise ApiError(404, "链接已失效或指向不支持的内容类型")
    kind, item_id = km.group(1), km.group(2)
    if kind == "slides":
        kind = "note"

    try:
        page, used_proxy = open_url(f"https://www.iesdouyin.com/share/{kind}/{item_id}/")
        html = page.read().decode("utf-8", "ignore")
    except ApiError:
        raise
    except Exception:
        raise ApiError(502, "分享页请求失败，请稍后重试")

    dm = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", html, re.S)
    if not dm:
        # 分享页正常必有 _ROUTER_DATA；没有 = 被返回验证/风控页。
        # 若是经代理请求，判定该代理 IP 被封禁：落库、禁用、上抛。
        if used_proxy:
            proxy_mgr.mark_banned(used_proxy, "分享页返回验证/无数据，IP 被风控封禁")
            raise ApiError(502, "代理 IP 被抖音风控（返回验证页），已自动封禁并禁用该代理，请重试")
        raise ApiError(502, "分享页无数据，可能被风控或页面结构变更，请稍后重试")
    data = json.loads(dm.group(1))

    items = next((i for i in _find_key(data, "item_list") if i), None)
    if not items:
        raise ApiError(404, "视频不存在、已被删除，或作者设为私密/仅粉丝可见")
    item = items[0]

    desc = item.get("desc", "") or ""
    au = item.get("author") or {}
    author = au.get("nickname") or next(_find_key(au, "nickname"), "") or ""
    avatar_list = next(_find_key(au, "url_list"), None) or []
    avatar = avatar_list[0] if avatar_list else ""
    base = _safe_name(desc, item_id)

    # 作者结构化详情：缓存到服务端，供前端悬停 2s 拉取做浮层
    sec_uid = au.get("sec_uid") or ""
    homepage = f"https://www.douyin.com/user/{sec_uid}" if sec_uid else ""
    author_detail = {
        "nickname": author,
        "avatar": avatar,
        "sec_uid": sec_uid,
        "douyin_id": au.get("unique_id") or au.get("short_id") or "",
        "signature": (au.get("signature") or "").strip(),
        "aweme_count": au.get("aweme_count"),
        "following_count": au.get("following_count"),
        "follower_count": au.get("mplatform_followers_count") or au.get("follower_count"),
        "total_favorited": au.get("total_favorited"),      # 获赞总数（分享页多为空，浮层时富化）
        "homepage": homepage,
        "enriched": False,
    }
    _author_cache[item_id] = (time.time(), author_detail)

    # 作品互动数据（点赞/评论/收藏/分享）—— 分享页直接给，无需额外请求
    st = item.get("statistics") or {}
    stats = {
        "digg": st.get("digg_count"),        # 点赞
        "comment": st.get("comment_count"),  # 评论
        "collect": st.get("collect_count"),  # 收藏
        "share": st.get("share_count"),      # 分享
    }

    # 更多可直接读取的元数据
    tags = [t.get("hashtag_name") for t in (item.get("text_extra") or []) if t.get("hashtag_name")]
    mu = item.get("music") or {}
    music = {"title": mu.get("title"), "author": mu.get("author")} if mu.get("title") else None
    poi = item.get("aweme_poi_info") or {}
    location = poi.get("poi_name") or (item.get("anchor_info") or {}).get("name") or None

    # 缩略图（封面/头像）直接给 CDN 直链，由浏览器直连加载
    result = {
        "kind": kind, "item_id": item_id, "title": desc or "（无标题）",
        "author": author,
        "avatar": avatar,
        "author_url": homepage,
        "create_time": item.get("create_time"),
        "stats": stats,
        "tags": tags,
        "music": music,
        "location": location,
        "base": base,
    }

    if kind == "note":
        images = item.get("images") or []
        if not images:
            raise ApiError(404, "图集中未找到图片")
        urls = [img["url_list"][0] for img in images if img.get("url_list")]
        # 直链交给浏览器直接查看 / 下载
        result["images"] = [{"url": u, "filename": f"{base}_{i:02d}.jpeg"}
                            for i, u in enumerate(urls, 1)]
        return result

    video = item.get("video") or {}
    play = next(_find_key(video.get("play_addr") or {}, "url_list"), None) or []
    if not play:
        raise ApiError(404, "未找到播放地址")
    vm = re.search(r"video_id=([\w-]+)", play[0])
    if not vm:
        raise ApiError(502, "播放地址格式已变更，无法提取 video_id")
    vid = vm.group(1)
    cover_list = next(_find_key(video.get("cover") or {}, "url_list"), None) or []

    result.update({
        "duration_ms": video.get("duration") or 0,
        "cover": cover_list[0] if cover_list else "",
        "video": {
            "url": _play_api(vid),                    # 浏览器直连播放/下载（自行跟随 302）
            "proxy_url": f"/api/video/{vid}",         # 兜底：直连失败时改走服务器代理
            "filename": f"{base}.mp4",
            "width": video.get("width"),
            "height": video.get("height"),
        },
    })
    return result


def _parse_cached(text: str) -> dict:
    key = text.strip()
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    data = _parse_share(text)
    _cache[key] = (now, data)
    _cache[data["item_id"]] = (now, data)
    if len(_cache) > 500:
        for k, (ts, _) in list(_cache.items()):
            if now - ts > CACHE_TTL:
                _cache.pop(k, None)
    return data


# ---------------------------------------------------------------- 公共 API

class ParseBody(BaseModel):
    text: str


@app.post("/api/parse")
def api_parse(body: ParseBody, request: Request):
    ip = (request.client.host if request.client else "") or "?"
    if not _ip_rate_ok(ip):
        raise ApiError(429, f"免费额度已用完（{FREE_IP_DAILY} 次/天/IP）。如需更高额度，请使用开放 API。")
    return _parse_cached(body.text)


@app.post("/api/v1/parse")
def api_v1_parse(body: ParseBody, request: Request):
    """开放 API（需 API Key）。付费商业化入口——按 Key 计量、限额、统计。"""
    key = request.headers.get("X-API-Key") or request.query_params.get("key", "")
    ok, err = apikeys.check_and_count(key)
    if not ok:
        raise ApiError(401, err)
    return _parse_cached(body.text)


def _fetch_user_info(sec_uid: str) -> dict:
    """经代理拉取作者主页统计（免签名 reflow 接口）：粉丝数、获赞数、作品数等。"""
    url = f"https://www.iesdouyin.com/web/api/v2/user/info/?sec_uid={sec_uid}"
    # 浏览器无法跨域取（抖音接口无 CORS），只能服务器代拉 —— 走代理，不暴露服务器 IP
    resp, _ = open_url(url, headers={"Referer": "https://www.iesdouyin.com/"})
    ui = (json.loads(resp.read().decode("utf-8", "ignore")) or {}).get("user_info") or {}
    return {
        "follower_count": ui.get("mplatform_followers_count"),
        "total_favorited": ui.get("total_favorited"),
        "following_count": ui.get("following_count"),
        "aweme_count": ui.get("aweme_count"),
        "douyin_id": ui.get("unique_id") or "",
        "signature": (ui.get("signature") or "").strip(),
    }


@app.get("/api/author")
def api_author(item_id: str):
    """作者结构化详情（供前端悬停浮层）。

    基础字段来自解析时缓存的分享页 author 对象；首次请求时再直连（不走代理）拉一次
    user/info 富化粉丝数/获赞数（分享页不给这两项），结果服务端缓存 10 分钟。
    注：抖音该接口无 CORS/JSONP，浏览器无法跨域直取，故由服务器直连（非代理）代拉。
    """
    hit = _author_cache.get(item_id)
    if not hit:
        raise ApiError(404, "作者信息不存在或已过期，请重新解析该视频")
    detail = hit[1]
    if detail.get("enriched") and time.time() - hit[0] < 600:
        return detail
    sec = detail.get("sec_uid")
    if sec:
        try:
            merged = {**detail, **{k: v for k, v in _fetch_user_info(sec).items() if v is not None},
                      "enriched": True}
            _author_cache[item_id] = (time.time(), merged)
            return merged
        except Exception:
            pass                         # 富化失败就返回基础字段，不影响头像浮层
    return detail


class BatchBody(BaseModel):
    text: str


@app.post("/api/parse/batch")
def api_parse_batch(body: BatchBody):
    """批量解析：文本里每行/每条链接一个，逐条返回结果或错误。"""
    links = re.findall(r"https://v\.douyin\.com/[\w-]+/?", body.text)
    seen, uniq = set(), []
    for l in links:
        if l not in seen:
            seen.add(l)
            uniq.append(l)
    if not uniq:
        raise ApiError(400, "未找到任何 v.douyin.com 分享链接")
    out = []
    for l in uniq[:20]:
        try:
            out.append({"ok": True, "link": l, "data": _parse_cached(l)})
        except ApiError as e:
            out.append({"ok": False, "link": l, "error": e.message})
        except Exception as e:
            out.append({"ok": False, "link": l, "error": str(e)})
    return {"count": len(out), "results": out}


@app.get("/api/video/{vid}")
def api_video(vid: str, request: Request, dl: str = "", name: str = "video.mp4"):
    if not re.fullmatch(r"[\w-]{8,120}", vid):
        raise ApiError(400, "非法的视频 ID")
    upstream = f"https://aweme.snssdk.com/aweme/v1/play/?video_id={vid}&ratio=720p&line=0"
    extra = dict(CDN_HEADERS)
    if request.headers.get("range"):
        extra["Range"] = request.headers["range"]
    try:
        # 播放兜底（浏览器直连失败时才走这里）：经代理，绝不暴露服务器 IP
        resp, _ = open_url(upstream, headers=extra)
    except ApiError:
        raise
    except urlerr.HTTPError as e:
        raise ApiError(502, f"视频源返回 {e.code}，链接可能已过期，请重新解析")
    except Exception:
        raise ApiError(502, "拉取视频失败，请重试")

    status = resp.status if hasattr(resp, "status") else resp.getcode()
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    for h in ("Content-Length", "Content-Range"):
        v = resp.headers.get(h)
        if v:
            headers[h] = v
    if dl:
        headers["Content-Disposition"] = _content_disposition(name or "video.mp4")
    return StreamingResponse(_stream(resp), status_code=status,
                             media_type="video/mp4", headers=headers)


@app.get("/api/media")
def api_media(url: str, name: str = "", dl: str = ""):
    if not _host_allowed(url):
        raise ApiError(403, "该资源域名不在允许范围内")
    try:
        resp, _ = open_url(url, headers=CDN_HEADERS)   # 图片兜底代理：经代理，不暴露服务器 IP
    except ApiError:
        raise
    except Exception:
        raise ApiError(502, "拉取资源失败，请重试")
    ctype = resp.headers.get("Content-Type", "image/jpeg")
    headers = {"Cache-Control": "public, max-age=3600"}
    if resp.headers.get("Content-Length"):
        headers["Content-Length"] = resp.headers["Content-Length"]
    if dl:
        headers["Content-Disposition"] = _content_disposition(name or "image.jpeg")
    return StreamingResponse(_stream(resp), media_type=ctype, headers=headers)


# 注：图集打包 ZIP 需服务器逐张下载再压缩，会走服务器 IP/带宽，
# 与"下载走浏览器直连"的设计冲突，已改为前端逐张浏览器直连下载（downloadAll）。


# ---------------------------------------------------------------- 管理后台

_sessions: dict[str, float] = {}     # token -> 过期时间
SESSION_TTL = 12 * 3600


def _new_session() -> str:
    tok = secrets.token_urlsafe(24)
    _sessions[tok] = time.time() + SESSION_TTL
    return tok


def _require_admin(request: Request):
    tok = request.cookies.get("admin_session", "")
    exp = _sessions.get(tok)
    if not exp or exp < time.time():
        _sessions.pop(tok, None)
        raise ApiError(401, "未登录或会话已过期，请重新登录管理后台")


class LoginBody(BaseModel):
    password: str


@app.post("/api/admin/login")
def admin_login(body: LoginBody):
    if not secrets.compare_digest(body.password, ADMIN_PASSWORD):
        raise ApiError(403, "密码错误")
    tok = _new_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("admin_session", tok, httponly=True, samesite="lax",
                    max_age=SESSION_TTL)
    return resp


@app.post("/api/admin/logout")
def admin_logout(request: Request):
    tok = request.cookies.get("admin_session", "")
    _sessions.pop(tok, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("admin_session")
    return resp


@app.get("/api/admin/state")
def admin_state(request: Request):
    _require_admin(request)
    return {
        "proxies": proxy_mgr.proxies,
        "settings": proxy_mgr.settings,
        "stats": proxy_mgr.stats,
        "ua_pool_size": len(UA_POOL),
    }


class AddProxyBody(BaseModel):
    urls: str
    note: str = ""


@app.post("/api/admin/proxies")
def admin_add_proxy(body: AddProxyBody, request: Request):
    _require_admin(request)
    return proxy_mgr.add_many(body.urls, body.note)


@app.delete("/api/admin/proxies/{pid}")
def admin_del_proxy(pid: str, request: Request):
    _require_admin(request)
    if not proxy_mgr.remove(pid):
        raise ApiError(404, "代理不存在")
    return {"ok": True}


@app.post("/api/admin/proxies/{pid}/toggle")
def admin_toggle_proxy(pid: str, request: Request):
    _require_admin(request)
    state = proxy_mgr.toggle(pid)
    if state is None:
        raise ApiError(404, "代理不存在")
    return {"ok": True, "enabled": state}


PROBE_TIMEOUT = 25    # 住宅代理较慢，给足超时


def _probe_proxy(p: dict, reach_douyin: bool = True) -> dict:
    """测试代理：出口 IP + 延迟，可选附带抖音可达性检测。回写状态并处理自愈/禁用。

    住宅代理每请求轮换 IP 且延迟高，抖音可达性重试 2 次以降低误报。
    """
    t0 = time.time()
    try:
        r = _raw_open(TEST_URL_IP, True, {"User-Agent": pick_ua()}, PROBE_TIMEOUT, p)
        body = r.read().decode("utf-8", "ignore")
        r.close()
        ip = json.loads(body).get("ip", "?")
        latency = int((time.time() - t0) * 1000)
    except Exception as e:
        proxy_mgr.record_probe(p, ok=False)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    douyin_ok = None
    if reach_douyin:
        douyin_ok = False
        for _ in range(2):                       # 轮换住宅代理：重试降低误报
            try:
                r = _raw_open(TEST_URL_DOUYIN, True, {"User-Agent": pick_ua()},
                              PROBE_TIMEOUT, p)
                r.read(128)
                r.close()
                douyin_ok = True
                break
            except Exception:
                continue

    proxy_mgr.record_probe(p, ok=True, latency_ms=latency, exit_ip=ip, douyin_ok=douyin_ok)
    return {"ok": True, "exit_ip": ip, "latency_ms": latency, "douyin_ok": douyin_ok}


def _probe_all(proxies: list[dict], reach_douyin: bool) -> list[dict]:
    """并发测试一批代理。"""
    import concurrent.futures as cf
    out = [None] * len(proxies)
    with cf.ThreadPoolExecutor(max_workers=min(8, max(1, len(proxies)))) as ex:
        futs = {ex.submit(_probe_proxy, p, reach_douyin): i for i, p in enumerate(proxies)}
        for f in cf.as_completed(futs):
            i = futs[f]
            out[i] = {"id": proxies[i]["id"], **f.result()}
    return out


class ValidateBody(BaseModel):
    urls: str


@app.post("/api/admin/proxies/validate")
def admin_validate(body: ValidateBody, request: Request):
    """预览解析结果：把用户粘贴的内容规范化成标准格式，不落库。"""
    _require_admin(request)
    scheme = proxy_mgr.settings.get("default_protocol", "socks5")
    out = []
    for line in re.split(r"[\r\n,;]+|\s{2,}", body.urls.strip()):
        line = line.strip()
        if not line:
            continue
        parsed = ProxyManager.parse_proxy(line, scheme)
        out.append({"raw": line, "parsed": parsed, "ok": bool(parsed)})
    return {"results": out}


@app.post("/api/admin/proxies/{pid}/test")
def admin_test_proxy(pid: str, request: Request):
    _require_admin(request)
    p = proxy_mgr.get(pid)
    if not p:
        raise ApiError(404, "代理不存在")
    return _probe_proxy(p, proxy_mgr.settings.get("test_reach_douyin", True))


@app.post("/api/admin/proxies/test-all")
def admin_test_all(request: Request):
    _require_admin(request)
    reach = proxy_mgr.settings.get("test_reach_douyin", True)
    results = _probe_all(list(proxy_mgr.proxies), reach)
    ok = sum(1 for r in results if r and r.get("ok"))
    return {"results": results, "ok": ok, "total": len(results)}


class SettingBody(BaseModel):
    force_proxy: Optional[bool] = None
    default_protocol: Optional[str] = None
    rotation: Optional[str] = None
    retries: Optional[int] = None
    auto_health: Optional[bool] = None
    health_interval_min: Optional[int] = None
    auto_disable_fail: Optional[int] = None
    test_reach_douyin: Optional[bool] = None


@app.post("/api/admin/settings")
def admin_settings(body: SettingBody, request: Request):
    _require_admin(request)
    vals = body.dict(exclude_none=True)
    if "default_protocol" in vals and vals["default_protocol"] not in SUPPORTED_SCHEMES:
        raise ApiError(400, "不支持的默认协议")
    if "rotation" in vals and vals["rotation"] not in ("round_robin", "random", "least_fail"):
        raise ApiError(400, "不支持的轮换策略")
    if "retries" in vals:
        vals["retries"] = max(1, min(10, int(vals["retries"])))
    if "health_interval_min" in vals:
        vals["health_interval_min"] = max(1, min(1440, int(vals["health_interval_min"])))
    if "auto_disable_fail" in vals:
        vals["auto_disable_fail"] = max(0, min(100, int(vals["auto_disable_fail"])))
    for k, v in vals.items():
        proxy_mgr.set_setting(k, v)
    return {"ok": True, "settings": proxy_mgr.settings}


# ---- 开放 API 密钥管理 ----

@app.get("/api/admin/apikeys")
def admin_list_keys(request: Request):
    _require_admin(request)
    return {"keys": apikeys.keys, "free_ip_daily": FREE_IP_DAILY}


class NewKeyBody(BaseModel):
    name: str = ""
    plan: str = "free"
    quota: int = 100


@app.post("/api/admin/apikeys")
def admin_create_key(body: NewKeyBody, request: Request):
    _require_admin(request)
    return apikeys.create(body.name, body.plan, body.quota)


@app.delete("/api/admin/apikeys/{key}")
def admin_revoke_key(key: str, request: Request):
    _require_admin(request)
    if not apikeys.revoke(key):
        raise ApiError(404, "API Key 不存在")
    return {"ok": True}


# ---------------------------------------------------------------- 后台健康检查

def _health_loop():
    """守护线程：按间隔并发测试启用中（及被自动禁用）的代理，自动禁用/自愈。"""
    while True:
        interval = max(1, int(proxy_mgr.settings.get("health_interval_min", 10)))
        for _ in range(interval * 60):
            time.sleep(1)
        if not proxy_mgr.settings.get("auto_health", True):
            continue
        targets = [p for p in list(proxy_mgr.proxies) if p["enabled"] or p.get("auto_off")]
        if targets:
            _probe_all(targets, proxy_mgr.settings.get("test_reach_douyin", True))


@app.on_event("startup")
def _start_health():
    threading.Thread(target=_health_loop, daemon=True).start()


# ---------------------------------------------------------------- 页面

@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/admin_d")
def admin_page():
    return FileResponse("static/admin.html")


@app.get("/healthz")
def healthz():
    return {"ok": True, "proxies": len(proxy_mgr.proxies),
            "enabled": sum(p["enabled"] for p in proxy_mgr.proxies)}
