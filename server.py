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

import hashlib
import hmac
import io
import json
import os
import random
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional
from urllib import error as urlerr
from urllib import parse as urlparse
from urllib import request as urlreq

from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, Response, StreamingResponse)
from pydantic import BaseModel

# ---------------------------------------------------------------- 常量与存储

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STORE_FILE = DATA_DIR / "config.json"

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "douyin-admin")
if ADMIN_PASSWORD == "douyin-admin":
    import sys as _sys
    print("⚠️  警告：正在使用默认管理员密码，请设置环境变量 ADMIN_PASSWORD 后再对外部署！",
          file=_sys.stderr)

# 免费使用配额（防薅羊毛）
FREE_ANON_DAILY = int(os.environ.get("FREE_ANON_DAILY", "3"))    # 匿名：每天 3 次
FREE_USER_DAILY = int(os.environ.get("FREE_USER_DAILY", "10"))   # 登录用户：每天 10 次

# ---------------------------------------------------------------- SQLite 数据层

DB_FILE = DATA_DIR / "app.db"
_db_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_daily(
  day INTEGER, subject TEXT, count INTEGER DEFAULT 0,
  PRIMARY KEY(day, subject)
);
CREATE TABLE IF NOT EXISTS request_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, subject TEXT,
  ip TEXT, ua TEXT, link TEXT, ok INTEGER, path TEXT, user_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_reqlog_ts ON request_logs(ts);
CREATE TABLE IF NOT EXISTS page_views(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, ip TEXT, ua TEXT, path TEXT, fp TEXT
);
CREATE INDEX IF NOT EXISTS idx_pv_ts ON page_views(ts);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, pw_salt TEXT, pw_hash TEXT,
  created_at INTEGER, last_login INTEGER, disabled INTEGER DEFAULT 0, reg_ip TEXT
);
CREATE TABLE IF NOT EXISTS api_keys(
  key TEXT PRIMARY KEY, user_id INTEGER, name TEXT, created INTEGER, enabled INTEGER DEFAULT 1,
  balance_cents INTEGER DEFAULT 100, spent_cents INTEGER DEFAULT 0, calls INTEGER DEFAULT 0,
  last_used INTEGER
);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, key TEXT, user_id INTEGER, status TEXT, total INTEGER, done INTEGER DEFAULT 0,
  ok INTEGER DEFAULT 0, cost_cents INTEGER DEFAULT 0, links TEXT, results TEXT,
  created INTEGER, finished INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jobs_key ON jobs(key);
CREATE TABLE IF NOT EXISTS api_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, key TEXT, user_id INTEGER,
  link TEXT, ok INTEGER, cost_cents INTEGER, job_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_apilog_ts ON api_logs(ts);
CREATE TABLE IF NOT EXISTS app_settings(k TEXT PRIMARY KEY, v TEXT);
"""


def _db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def db_exec(sql: str, params=(), fetch: Optional[str] = None):
    with _db_lock:
        conn = _db()
        try:
            cur = conn.execute(sql, params)
            out = (cur.fetchone() if fetch == "one"
                   else cur.fetchall() if fetch == "all"
                   else cur.rowcount if fetch == "rowcount"
                   else cur.lastrowid)
            conn.commit()
            return out
        finally:
            conn.close()


with _db_lock:
    _c = _db()
    _c.execute("PRAGMA journal_mode=WAL")      # 允许并发读，写不阻塞读
    _c.executescript(_SCHEMA)
    _c.execute("CREATE INDEX IF NOT EXISTS idx_reqlog_user ON request_logs(user_id)")
    _c.commit()
    _c.close()


# ---------------------------------------------------------------- 防薅羊毛 / 限频

def _today() -> int:
    return int(time.time() // 86400)


# 只有来自可信反代时才采信 X-Forwarded-For，否则客户端可伪造头绕过所有基于 IP 的风控。
# 设 TRUST_PROXY=1 表示部署在反代后（Nginx/Cloudflare 等），此时才读 XFF。
TRUST_PROXY = os.environ.get("TRUST_PROXY", "").lower() in ("1", "true", "yes")
# 会话 cookie 是否加 Secure（仅走 HTTPS 发送）。生产（反代/HTTPS）应为真；本地 http 调试默认关。
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true", "yes") or TRUST_PROXY


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "?"
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return peer


def _client_fp(request: Request) -> str:
    return (request.headers.get("x-fp") or "")[:64]


def _usage(subject: str, day: int) -> int:
    row = db_exec("SELECT count FROM usage_daily WHERE day=? AND subject=?",
                  (day, subject), "one")
    return row[0] if row else 0


def _quota_subjects(request: Request):
    """返回 (计数主体列表, 限额)。登录用户按 user 计（10/天），匿名按指纹+IP（3/天）。"""
    u = current_user(request)
    if u:
        return [f"user:{u['id']}"], FREE_USER_DAILY
    subs = [f"ip:{_client_ip(request)}"]
    fp = _client_fp(request)
    if fp:
        subs.append(f"fp:{fp}")
    return subs, FREE_ANON_DAILY


def quota_status(request: Request):
    """返回 (limit, used, remaining)。"""
    day = _today()
    subs, limit = _quota_subjects(request)
    used = max((_usage(s, day) for s in subs), default=0)
    return limit, used, max(0, limit - used)


def reserve_quota(request: Request, n: int = 1):
    """预占 n 次配额。返回 (ok, limit, used_after, remaining)。"""
    day = _today()
    subs, limit = _quota_subjects(request)
    used = max((_usage(s, day) for s in subs), default=0)
    if used + n > limit:
        return False, limit, used, max(0, limit - used)
    for s in subs:
        db_exec("INSERT INTO usage_daily(day,subject,count) VALUES(?,?,?) "
                "ON CONFLICT(day,subject) DO UPDATE SET count=count+?",
                (day, s, n, n))
    return True, limit, used + n, limit - (used + n)


def log_request(request: Request, kind: str, link: str, ok: bool):
    try:
        u = current_user(request)
        db_exec("INSERT INTO request_logs(ts,kind,subject,ip,ua,link,ok,path,user_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (int(time.time()), kind,
                 _client_fp(request) or _client_ip(request), _client_ip(request),
                 (request.headers.get("user-agent") or "")[:200],
                 link, 1 if ok else 0, request.url.path, u["id"] if u else None))
    except Exception:
        pass


def log_pageview(request: Request):
    try:
        db_exec("INSERT INTO page_views(ts,ip,ua,path,fp) VALUES(?,?,?,?,?)",
                (int(time.time()), _client_ip(request),
                 (request.headers.get("user-agent") or "")[:200],
                 request.url.path, _client_fp(request)))
    except Exception:
        pass


# ---------------------------------------------------------------- 用户鉴权 / 防机器人

def hash_pw(pw: str, salt: Optional[str] = None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120_000).hex()
    return salt, h


def verify_pw(pw: str, salt: str, h: str) -> bool:
    return hmac.compare_digest(
        hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120_000).hex(), h)


_user_sessions: dict = {}     # token -> (user_id, expiry)
USER_SESSION_TTL = 30 * 86400


def _new_user_session(uid: int) -> str:
    tok = secrets.token_urlsafe(24)
    _user_sessions[tok] = (uid, time.time() + USER_SESSION_TTL)
    return tok


def current_user(request: Request):
    """从 cookie 取当前登录用户（dict）或 None。"""
    tok = request.cookies.get("sess", "")
    ent = _user_sessions.get(tok)
    if not ent or ent[1] < time.time():
        _user_sessions.pop(tok, None)
        return None
    row = db_exec("SELECT * FROM users WHERE id=? AND disabled=0", (ent[0],), "one")
    return dict(row) if row else None


# ---- 滑块验证码（服务端 PNG 缺口 + 行为轨迹 + PoW + 蜜罐 + 一次性签名令牌）----
# 缺口坐标只存在服务端与像素里，绝不出现在返回的标记中——无法靠抓包/解析拿到答案。
_captchas: dict = {}          # cid -> (gap_x, gap_y, issued_at, ip)
CAPTCHA_W, CAPTCHA_H, PIECE = 300, 170, 50
POW_BITS = 14                 # 工作量证明，抬高批量自动化成本
CAPTCHA_SECRET = (os.environ.get("CAPTCHA_SECRET") or "").encode() or secrets.token_bytes(32)
# 多 worker 部署请设 CAPTCHA_SECRET 环境变量，否则各进程密钥不一致导致令牌互不认
_passes: dict = {}            # pass_token -> expiry（一次性）


def _png(width: int, height: int, rows, alpha: bool = False) -> bytes:
    """极简 PNG 编码器（stdlib）。rows 为每行像素字节 bytearray。"""
    import struct
    import zlib
    ct = 6 if alpha else 2                              # RGBA / RGB
    raw = bytearray()
    for r in rows:
        raw.append(0)                                  # filter type 0
        raw += r

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, ct, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def _draw_bg(W: int, H: int):
    import colorsys
    h0 = secrets.randbelow(360) / 360
    c1 = tuple(int(v * 255) for v in colorsys.hls_to_rgb(h0, 0.52, 0.55))
    c2 = tuple(int(v * 255) for v in colorsys.hls_to_rgb((h0 + 0.3) % 1, 0.42, 0.55))
    rows = []
    for y in range(H):
        ty = y / H
        row = bytearray(W * 3)
        for x in range(W):
            t = (x / W + ty) * 0.5
            o = x * 3
            row[o] = int(c1[0] * (1 - t) + c2[0] * t)
            row[o + 1] = int(c1[1] * (1 - t) + c2[1] * t)
            row[o + 2] = int(c1[2] * (1 - t) + c2[2] * t)
        rows.append(row)
    for _ in range(5):                                 # 干扰光斑（只遍历包围盒）
        cx, cy, cr = secrets.randbelow(W), secrets.randbelow(H), 12 + secrets.randbelow(22)
        dark = secrets.randbelow(2)
        for y in range(max(0, cy - cr), min(H, cy + cr)):
            base = rows[y]
            for x in range(max(0, cx - cr), min(W, cx + cr)):
                if (x - cx) ** 2 + (y - cy) ** 2 <= cr * cr:
                    o = x * 3
                    for k in range(3):
                        v = base[o + k]
                        base[o + k] = max(0, min(255, v * 7 // 10 if dark else v * 5 // 4))
    return rows


def _in_piece(dx: int, dy: int, PS: int, rad: int) -> bool:
    if dx < rad and dy < rad:
        return (dx - rad) ** 2 + (dy - rad) ** 2 <= rad * rad
    if dx >= PS - rad and dy < rad:
        return (dx - (PS - rad - 1)) ** 2 + (dy - rad) ** 2 <= rad * rad
    if dx < rad and dy >= PS - rad:
        return (dx - rad) ** 2 + (dy - (PS - rad - 1)) ** 2 <= rad * rad
    if dx >= PS - rad and dy >= PS - rad:
        return (dx - (PS - rad - 1)) ** 2 + (dy - (PS - rad - 1)) ** 2 <= rad * rad
    return True


def make_captcha(request: Request) -> dict:
    W, H, PS = CAPTCHA_W, CAPTCHA_H, PIECE
    cid = secrets.token_urlsafe(12)
    gap_x = 90 + secrets.randbelow(W - PS - 110)       # 答案：仅服务端 + 像素
    gap_y = 20 + secrets.randbelow(H - PS - 34)
    _captchas[cid] = (gap_x, gap_y, time.time(), _client_ip(request))
    now = time.time()
    if len(_captchas) > 3000:
        for k, v in list(_captchas.items()):
            if now - v[2] > 300:
                _captchas.pop(k, None)

    bg = _draw_bg(W, H)
    rad = 11
    piece_rows = []
    for dy in range(PS):
        prow = bytearray(PS * 4)
        for dx in range(PS):
            po = dx * 4
            if _in_piece(dx, dy, PS, rad):
                bo = (gap_x + dx) * 3
                srow = bg[gap_y + dy]
                r, g, b = srow[bo], srow[bo + 1], srow[bo + 2]
                edge = dx < 2 or dy < 2 or dx >= PS - 2 or dy >= PS - 2
                if edge:                               # 亮边，拼图更立体
                    prow[po], prow[po + 1], prow[po + 2] = min(255, r + 90), min(255, g + 90), min(255, b + 90)
                else:
                    prow[po], prow[po + 1], prow[po + 2] = r, g, b
                prow[po + 3] = 255
                srow[bo] = r * 4 // 10                 # 挖空处变暗成缺口
                srow[bo + 1] = g * 4 // 10
                srow[bo + 2] = b * 4 // 10
                if edge:
                    srow[bo] = min(255, srow[bo] + 40)
            else:
                prow[po + 3] = 0
        piece_rows.append(prow)

    import base64
    du = lambda p, m="png": f"data:image/{m};base64," + base64.b64encode(p).decode()
    return {"cid": cid, "bg": du(_png(W, H, bg)), "piece": du(_png(PS, PS, piece_rows, alpha=True)),
            "y": gap_y, "w": W, "h": H, "piece_size": PS, "pow_bits": POW_BITS}


def _pow_ok(cid: str, nonce: str) -> bool:
    if not isinstance(nonce, str) or len(nonce) > 40:
        return False
    digest = hashlib.sha256(f"{cid}:{nonce}".encode()).digest()
    return int.from_bytes(digest, "big").bit_length() <= 256 - POW_BITS   # 前 POW_BITS 位为 0


def verify_captcha(cid: str, x, trajectory, nonce: str, request: Request):
    c = _captchas.pop(cid, None)                       # cid 一次性
    if not c:
        return False, "验证已失效，请重新拖动滑块"
    gap_x, gap_y, t0, ip = c
    if _client_ip(request) != ip:
        return False, "环境变化，请重试"                # 绑定签发时的 IP
    if time.time() - t0 > 180:
        return False, "验证超时，请重试"
    if time.time() - t0 < 0.4:
        return False, "操作过快，请手动拖动"            # 秒过 = 脚本
    try:
        x = float(x)
    except Exception:
        return False, "参数错误"
    if abs(x - gap_x) > 6:
        return False, "拼图未对齐，请重试"
    tr = trajectory or []
    if not isinstance(tr, list) or len(tr) < 6:
        return False, "请手动拖动滑块完成验证"
    try:
        ts = [float(p["t"]) for p in tr]
        xs = [float(p["x"]) for p in tr]
    except Exception:
        return False, "轨迹异常"
    dur = ts[-1] - ts[0]
    if dur < 260 or dur > 30000:
        return False, "拖动速度异常，请重试"
    dxs = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
    if max((abs(d) for d in dxs), default=0) > gap_x * 0.6:
        return False, "疑似脚本，请手动拖动"            # 一步跳到位
    if len(set(round(d, 1) for d in dxs)) < 4:
        return False, "疑似匀速脚本，请手动拖动"        # 速度无变化 = 线性脚本
    if not _pow_ok(cid, nonce):
        return False, "安全校验失败，请刷新重试"
    return True, None


def issue_pass(request: Request) -> str:
    """滑块通过后签发一次性、限时、绑定 IP 的 HMAC 通行令牌。"""
    exp = int(time.time()) + 120
    body = f"{_client_ip(request)}|{exp}|{secrets.token_urlsafe(9)}"
    sig = hmac.new(CAPTCHA_SECRET, body.encode(), hashlib.sha256).hexdigest()[:20]
    tok = f"{body}|{sig}"
    _passes[tok] = exp
    now = int(time.time())
    if len(_passes) > 5000:
        for k, v in list(_passes.items()):
            if v < now:
                _passes.pop(k, None)
    return tok


def consume_pass(tok: str, request: Request) -> bool:
    """注册/登录时校验并作废通行令牌——一次性、防重放、防伪造、绑定 IP。"""
    if not tok or not isinstance(tok, str):
        return False
    exp = _passes.pop(tok, None)                        # 一次性：用过即废
    if exp is None:
        return False
    try:
        t_ip, t_exp, rnd, sig = tok.split("|")
    except Exception:
        return False
    good = hmac.new(CAPTCHA_SECRET, f"{t_ip}|{t_exp}|{rnd}".encode(),
                    hashlib.sha256).hexdigest()[:20]
    return (hmac.compare_digest(sig, good)
            and t_ip == _client_ip(request)
            and int(t_exp) >= int(time.time()))


# ---- 注册/登录按 IP 限频（防爆破）----
_auth_hits: dict = {}          # ip -> [timestamps]
_captcha_hits: dict = {}       # ip -> [timestamps]（验证码签发限频，防 CPU-DoS）
AUTH_MAX_PER_HOUR = 20
CAPTCHA_MAX_PER_MIN = 40


def _rate_ok(store: dict, ip: str, window: float, cap: int) -> bool:
    now = time.time()
    hits = [t for t in store.get(ip, []) if now - t < window]
    hits.append(now)
    store[ip] = hits
    return len(hits) <= cap


def _auth_rate_ok(ip: str) -> bool:
    return _rate_ok(_auth_hits, ip, 3600, AUTH_MAX_PER_HOUR)


def _captcha_rate_ok(ip: str) -> bool:
    return _rate_ok(_captcha_hits, ip, 60, CAPTCHA_MAX_PER_MIN)


def _sweep_memory():
    """周期清理会话/令牌/限频等内存字典，防止无界增长。"""
    now = time.time()
    for tok, ent in list(_user_sessions.items()):
        if ent[1] < now:
            _user_sessions.pop(tok, None)
    for tok, exp in list(_passes.items()):
        if exp < now:
            _passes.pop(tok, None)
    for cid, v in list(_captchas.items()):
        if now - v[2] > 300:
            _captchas.pop(cid, None)
    for store, win in ((_auth_hits, 3600), (_captcha_hits, 60)):
        for ip, hits in list(store.items()):
            fresh = [t for t in hits if now - t < win]
            if fresh:
                store[ip] = fresh
            else:
                store.pop(ip, None)


def _sweeper():
    while True:
        time.sleep(300)
        try:
            _sweep_memory()
        except Exception:
            pass


threading.Thread(target=_sweeper, daemon=True).start()


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


# ---------------------------------------------------------------- 应用设置 + 开放 API 计费

NEW_KEY_BALANCE = int(os.environ.get("NEW_KEY_BALANCE", "100"))   # 新 Key 试用余额（分）


def app_setting(key: str, default: str = "") -> str:
    row = db_exec("SELECT v FROM app_settings WHERE k=?", (key,), "one")
    return row["v"] if row else default


def set_app_setting(key: str, val) -> None:
    db_exec("INSERT INTO app_settings(k,v) VALUES(?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=?", (key, str(val), str(val)))


def api_price_cents() -> int:
    try:
        return max(0, int(app_setting("api_price_cents", "1")))    # 默认 1 分/次
    except Exception:
        return 1


def create_api_key(user_id: Optional[int], name: str) -> dict:
    key = "dy_" + secrets.token_urlsafe(24)
    db_exec("INSERT INTO api_keys(key,user_id,name,created,enabled,balance_cents,spent_cents,calls) "
            "VALUES(?,?,?,?,1,?,0,0)",
            (key, user_id, (name or "未命名")[:60], int(time.time()), NEW_KEY_BALANCE))
    return get_api_key(key)


def get_api_key(key: str) -> Optional[dict]:
    row = db_exec("SELECT * FROM api_keys WHERE key=?", (key,), "one")
    return dict(row) if row else None


def list_api_keys(user_id: Optional[int] = None) -> list:
    if user_id is None:
        rows = db_exec("SELECT * FROM api_keys ORDER BY created DESC", (), "all")
    else:
        rows = db_exec("SELECT * FROM api_keys WHERE user_id=? ORDER BY created DESC",
                       (user_id,), "all")
    return [dict(r) for r in rows]


def revoke_api_key(key: str, user_id: Optional[int] = None) -> bool:
    k = get_api_key(key)
    if not k or (user_id is not None and k["user_id"] != user_id):
        return False
    db_exec("DELETE FROM api_keys WHERE key=?", (key,))
    return True


def recharge_key(key: str, cents: int) -> bool:
    if not get_api_key(key):
        return False
    db_exec("UPDATE api_keys SET balance_cents=balance_cents+? WHERE key=?", (int(cents), key))
    return True


def api_key_check(key: str):
    """校验 key（不扣费）。返回 (rec, error)。"""
    if not key:
        return None, "缺少 API Key（请在 X-API-Key 头或 ?key= 传入）"
    rec = get_api_key(key)
    if not rec or not rec["enabled"]:
        return None, "无效或已禁用的 API Key"
    return rec, None


def try_reserve(key: str, price: int) -> bool:
    """原子扣减余额（仅当余额足够）。返回是否成功——避免并发任务把余额扣成负数。"""
    if price <= 0:
        return True
    n = db_exec("UPDATE api_keys SET balance_cents=balance_cents-? "
                "WHERE key=? AND balance_cents>=?", (price, key, price), "rowcount")
    return bool(n)


def api_settle(key: str, link: str, ok: bool, price: int, job_id: str = ""):
    """结算一次调用：成功则计入 spent/calls，失败则退回已预扣的余额；两种情况都记日志。"""
    uid = (get_api_key(key) or {}).get("user_id")
    if ok:
        if price:
            db_exec("UPDATE api_keys SET spent_cents=spent_cents+?, calls=calls+1, last_used=? "
                    "WHERE key=?", (price, int(time.time()), key))
    else:
        if price:
            db_exec("UPDATE api_keys SET balance_cents=balance_cents+? WHERE key=?", (price, key))
    db_exec("INSERT INTO api_logs(ts,key,user_id,link,ok,cost_cents,job_id) VALUES(?,?,?,?,?,?,?)",
            (int(time.time()), key, uid, link, 1 if ok else 0, price if ok else 0, job_id))


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
    try:
        location = resp.headers.get("Location", "") if hasattr(resp, "headers") else ""
    finally:
        try:
            resp.close()
        except Exception:
            pass
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
        try:
            html = page.read().decode("utf-8", "ignore")
        finally:
            page.close()
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


def _quota_error(limit: int):
    return ApiError(429, f"今日免费次数已用完（每天 {limit} 次）。注册登录后每天可用 "
                         f"{FREE_USER_DAILY} 次，或使用开放 API 按量调用。")


@app.post("/api/parse")
def api_parse(body: ParseBody, request: Request):
    limit, used, remaining = quota_status(request)
    if remaining <= 0:
        raise _quota_error(limit)
    try:
        data = _parse_cached(body.text)
    except ApiError:
        log_request(request, "web", body.text[:100], False)
        raise
    reserve_quota(request, 1)                 # 成功才计一次
    log_request(request, "web", body.text[:100], True)
    return data


# ---------------------------------------------------------------- 开放 API v1（异步任务 + 计费）

def _extract_links(text: str) -> list:
    links = re.findall(r"https://v\.douyin\.com/[\w-]+/?", text or "")
    seen, uniq = set(), []
    for l in links:
        if l not in seen:
            seen.add(l)
            uniq.append(l)
    return uniq


def _run_job(job_id: str, key: str, links: list):
    """后台线程：逐条『先原子预扣、再解析、失败退款』，并发安全，不会把余额扣成负。"""
    results, ok_n, cost = [], 0, 0
    price = api_price_cents()
    for i, l in enumerate(links):
        if not try_reserve(key, price):                    # 原子预扣，余额不足即停
            results.append({"link": l, "ok": False, "error": "余额不足，已停止",
                            "code": "insufficient_balance"})
            db_exec("UPDATE jobs SET done=done+1, results=? WHERE id=?",
                    (json.dumps(results, ensure_ascii=False), job_id))
            continue
        try:
            data = _parse_cached(l)
            results.append({"link": l, "ok": True, "data": data})
            api_settle(key, l, True, price, job_id)
            ok_n += 1
            cost += price
        except ApiError as e:
            results.append({"link": l, "ok": False, "error": e.message})
            api_settle(key, l, False, price, job_id)       # 失败退回预扣
        except Exception as e:
            results.append({"link": l, "ok": False, "error": str(e)})
            api_settle(key, l, False, price, job_id)
        # 结果较大时降低写库频率（每 5 条或最后一条落一次），避免 O(n²) 序列化
        if (i + 1) % 5 == 0 or i + 1 == len(links):
            db_exec("UPDATE jobs SET done=?, ok=?, cost_cents=?, results=? WHERE id=?",
                    (len(results), ok_n, cost, json.dumps(results, ensure_ascii=False), job_id))
    db_exec("UPDATE jobs SET status='done', done=?, ok=?, cost_cents=?, results=?, finished=? WHERE id=?",
            (len(results), ok_n, cost, json.dumps(results, ensure_ascii=False),
             int(time.time()), job_id))


class JobBody(BaseModel):
    links: list = []
    text: str = ""


def _api_key_from(request: Request) -> str:
    return request.headers.get("X-API-Key") or request.query_params.get("key", "")


@app.post("/api/v1/jobs")
def api_v1_create_job(body: JobBody, request: Request):
    """提交批量解析任务（异步）。请求体：{links:[...]} 或 {text:"..."}。返回 job_id。"""
    rec, err = api_key_check(_api_key_from(request))
    if err:
        raise ApiError(401, err)
    links = list(body.links or []) or _extract_links(body.text)
    links = [l for l in links if re.match(r"https://v\.douyin\.com/[\w-]+", str(l))][:100]
    if not links:
        raise ApiError(400, "links 为空或没有合法的 v.douyin.com 链接")
    price = api_price_cents()
    if rec["balance_cents"] < price:
        raise ApiError(402, f"余额不足（当前 {rec['balance_cents']} 分，单价 {price} 分/条），请充值")
    job_id = "job_" + secrets.token_urlsafe(12)
    db_exec("INSERT INTO jobs(id,key,user_id,status,total,done,ok,cost_cents,links,results,created) "
            "VALUES(?,?,?,?,?,0,0,0,?,?,?)",
            (job_id, rec["key"], rec["user_id"], "pending", len(links),
             json.dumps(links, ensure_ascii=False), "[]", int(time.time())))
    threading.Thread(target=_run_job, args=(job_id, rec["key"], links), daemon=True).start()
    return {"code": 0, "message": "accepted", "data": {
        "job_id": job_id, "total": len(links), "status": "pending",
        "price_cents": price, "estimated_cost_cents": price * len(links),
        "query_url": f"/api/v1/jobs/{job_id}"}}


@app.get("/api/v1/jobs/{job_id}")
def api_v1_get_job(job_id: str, request: Request):
    """查询任务结果。需带同一 API Key。"""
    rec, err = api_key_check(_api_key_from(request))
    if err:
        raise ApiError(401, err)
    row = db_exec("SELECT * FROM jobs WHERE id=? AND key=?", (job_id, rec["key"]), "one")
    if not row:
        raise ApiError(404, "任务不存在或无权访问")
    j = dict(row)
    return {"code": 0, "message": "ok", "data": {
        "job_id": j["id"], "status": j["status"], "total": j["total"],
        "done": j["done"], "ok": j["ok"], "cost_cents": j["cost_cents"],
        "created": j["created"], "finished": j["finished"],
        "results": json.loads(j["results"] or "[]")}}


@app.get("/api/v1/balance")
def api_v1_balance(request: Request):
    """查询当前 Key 的余额与用量。"""
    rec, err = api_key_check(_api_key_from(request))
    if err:
        raise ApiError(401, err)
    return {"code": 0, "data": {
        "balance_cents": rec["balance_cents"], "spent_cents": rec["spent_cents"],
        "calls": rec["calls"], "price_cents": api_price_cents()}}


def _fetch_user_info(sec_uid: str) -> dict:
    """经代理拉取作者主页统计（免签名 reflow 接口）：粉丝数、获赞数、作品数等。"""
    url = f"https://www.iesdouyin.com/web/api/v2/user/info/?sec_uid={sec_uid}"
    # 浏览器无法跨域取（抖音接口无 CORS），只能服务器代拉 —— 走代理，不暴露服务器 IP
    resp, _ = open_url(url, headers={"Referer": "https://www.iesdouyin.com/"})
    try:
        ui = (json.loads(resp.read().decode("utf-8", "ignore")) or {}).get("user_info") or {}
    finally:
        try:
            resp.close()
        except Exception:
            pass
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
def api_parse_batch(body: BatchBody, request: Request):
    """批量解析：每条链接算一次配额，超出今日免费额度的部分不解析。"""
    links = re.findall(r"https://v\.douyin\.com/[\w-]+/?", body.text)
    seen, uniq = set(), []
    for l in links:
        if l not in seen:
            seen.add(l)
            uniq.append(l)
    if not uniq:
        raise ApiError(400, "未找到任何 v.douyin.com 分享链接")

    limit, used, remaining = quota_status(request)
    if remaining <= 0:
        raise _quota_error(limit)
    uniq = uniq[:50]
    process, over = uniq[:remaining], uniq[remaining:]   # 超额部分不解析

    out, spent = [], 0
    for l in process:
        try:
            out.append({"ok": True, "link": l, "data": _parse_cached(l)})
            spent += 1
            log_request(request, "web", l, True)
        except ApiError as e:
            out.append({"ok": False, "link": l, "error": e.message})
            log_request(request, "web", l, False)
        except Exception as e:
            out.append({"ok": False, "link": l, "error": str(e)})
    for l in over:
        out.append({"ok": False, "link": l,
                    "error": f"今日免费次数不足未解析（每天 {limit} 次，登录后 {FREE_USER_DAILY} 次）"})
    if spent:
        reserve_quota(request, spent)
    return {"count": len(out), "results": out,
            "quota": {"limit": limit, "remaining": max(0, remaining - spent)}}


class ExportBody(BaseModel):
    items: list          # 前端解析好的结果数组（仅元数据/文案，不含媒体字节）


@app.post("/api/export/xlsx")
def export_xlsx(body: ExportBody):
    """把批量解析结果导出为真正的 Excel(.xlsx)。仅整理元数据，不下载任何视频。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = "抖音批量解析"
    headers = ["序号", "类型", "标题/文案", "作者", "作品ID", "时长(秒)", "分辨率",
               "点赞", "评论", "收藏", "分享", "发布时间", "话题标签", "背景音乐",
               "拍摄位置", "视频/图片地址", "作者主页", "原分享链接"]
    ws.append(headers)
    hf = Font(bold=True, color="FFFFFF")
    hfill = PatternFill("solid", fgColor="E0234E")
    for c in ws[1]:
        c.font, c.fill = hf, hfill
        c.alignment = Alignment(vertical="center")

    for i, d in enumerate(body.items or [], 1):
        d = d or {}
        is_note = d.get("kind") == "note"
        st = d.get("stats") or {}
        v = d.get("video") or {}
        ct = d.get("create_time")
        cts = time.strftime("%Y-%m-%d %H:%M", time.localtime(ct)) if ct else ""
        media = (" | ".join(im.get("url", "") for im in (d.get("images") or []))
                 if is_note else v.get("url", ""))
        res = f"{v.get('width')}×{v.get('height')}" if v.get("width") else ""
        dur = round((d.get("duration_ms") or 0) / 1000, 1) if not is_note else ""
        ws.append([
            i, "图集" if is_note else "视频", d.get("title", ""), d.get("author", ""),
            d.get("item_id", ""), dur, res,
            st.get("digg"), st.get("comment"), st.get("collect"), st.get("share"),
            cts, " ".join("#" + t for t in (d.get("tags") or [])),
            (d.get("music") or {}).get("title") or "", d.get("location") or "",
            media, d.get("author_url", ""), d.get("_link", ""),
        ])

    widths = [5, 6, 40, 14, 20, 8, 11, 8, 8, 8, 8, 17, 24, 24, 20, 46, 40, 30]
    for col, w in enumerate(widths, 1):
        ws.column_dimensions[chr(64 + col) if col <= 26 else "A" + chr(38 + col)].width = w
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = time.strftime("douyin_batch_%Y%m%d_%H%M.xlsx")
    return Response(buf.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": _content_disposition(fname)})


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
    final = getattr(resp, "url", "") or url             # 跟随重定向后复核最终 host，防 SSRF 绕过白名单
    if not _host_allowed(final):
        try:
            resp.close()
        except Exception:
            pass
        raise ApiError(403, "资源重定向到了不允许的域名")
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
                    secure=COOKIE_SECURE, max_age=SESSION_TTL)
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


# ---- 开放 API 密钥管理（管理员）----

@app.get("/api/admin/apikeys")
def admin_list_keys(request: Request):
    _require_admin(request)
    return {"keys": list_api_keys(), "free_ip_daily": FREE_ANON_DAILY,
            "price_cents": api_price_cents()}


class NewKeyBody(BaseModel):
    name: str = ""


@app.post("/api/admin/apikeys")
def admin_create_key(body: NewKeyBody, request: Request):
    _require_admin(request)
    return create_api_key(None, body.name)


@app.delete("/api/admin/apikeys/{key}")
def admin_revoke_key(key: str, request: Request):
    _require_admin(request)
    if not revoke_api_key(key):
        raise ApiError(404, "API Key 不存在")
    return {"ok": True}


class RechargeBody(BaseModel):
    cents: int


@app.post("/api/admin/apikeys/{key}/recharge")
def admin_recharge_key(key: str, body: RechargeBody, request: Request):
    _require_admin(request)
    if not recharge_key(key, body.cents):
        raise ApiError(404, "API Key 不存在")
    return {"ok": True, "key": get_api_key(key)}


class PriceBody(BaseModel):
    price_cents: int


@app.post("/api/admin/api-price")
def admin_set_price(body: PriceBody, request: Request):
    _require_admin(request)
    set_app_setting("api_price_cents", max(0, int(body.price_cents)))
    return {"ok": True, "price_cents": api_price_cents()}


# ---- 数据分析 ----

def _series(sql: str, days: int = 14):
    """返回最近 days 天的 {day: value} 序列（day 为 epoch 天）。sql 需 SELECT day, val。"""
    since = (_today() - days + 1) * 86400
    rows = db_exec(sql, (since,), "all") or []
    m = {r[0]: r[1] for r in rows}
    return [{"day": _today() - i, "v": m.get(_today() - i, 0)} for i in range(days - 1, -1, -1)]


@app.get("/api/admin/analytics")
def admin_analytics(request: Request):
    _require_admin(request)
    today = _today()
    day0 = today * 86400

    def one(sql, params=()):
        r = db_exec(sql, params, "one")
        return (r[0] or 0) if r else 0

    total_users = one("SELECT COUNT(*) FROM users")
    new_users_today = one("SELECT COUNT(*) FROM users WHERE created_at>=?", (day0,))
    pv_today = one("SELECT COUNT(*) FROM page_views WHERE ts>=?", (day0,))
    uv_today = one("SELECT COUNT(DISTINCT ip) FROM page_views WHERE ts>=?", (day0,))
    web_today = one("SELECT COUNT(*) FROM request_logs WHERE ok=1 AND ts>=?", (day0,))
    api_today = one("SELECT COUNT(*) FROM api_logs WHERE ok=1 AND ts>=?", (day0,))
    rev_today = one("SELECT COALESCE(SUM(cost_cents),0) FROM api_logs WHERE ts>=?", (day0,))
    rev_total = one("SELECT COALESCE(SUM(cost_cents),0) FROM api_logs")
    # 回访率：注册后又回来过（last_login 比注册晚 1 天以上）
    returned = one("SELECT COUNT(*) FROM users WHERE last_login-created_at>=86400")
    retention = round(returned / total_users * 100, 1) if total_users else 0.0

    return {
        "cards": {
            "total_users": total_users, "new_users_today": new_users_today,
            "pv_today": pv_today, "uv_today": uv_today,
            "usage_today": web_today + api_today, "api_today": api_today,
            "revenue_today_cents": rev_today, "revenue_total_cents": rev_total,
            "retention_pct": retention,
        },
        "series": {
            "new_users": _series("SELECT created_at/86400, COUNT(*) FROM users WHERE created_at>=? GROUP BY 1"),
            "pv": _series("SELECT ts/86400, COUNT(*) FROM page_views WHERE ts>=? GROUP BY 1"),
            "uv": _series("SELECT ts/86400, COUNT(DISTINCT ip) FROM page_views WHERE ts>=? GROUP BY 1"),
            "parses": _series("SELECT ts/86400, COUNT(*) FROM request_logs WHERE ok=1 AND ts>=? GROUP BY 1"),
            "api_calls": _series("SELECT ts/86400, COUNT(*) FROM api_logs WHERE ok=1 AND ts>=? GROUP BY 1"),
            "revenue": _series("SELECT ts/86400, COALESCE(SUM(cost_cents),0) FROM api_logs WHERE ts>=? GROUP BY 1"),
        },
    }


@app.get("/api/admin/users")
def admin_users(request: Request, limit: int = 100):
    _require_admin(request)
    rows = db_exec(
        "SELECT u.id,u.email,u.created_at,u.last_login,u.disabled,u.reg_ip,"
        "(SELECT COUNT(*) FROM request_logs r WHERE r.user_id=u.id AND r.ok=1) AS parses,"
        "(SELECT COALESCE(SUM(spent_cents),0) FROM api_keys k WHERE k.user_id=u.id) AS spent,"
        "(SELECT COUNT(*) FROM api_keys k WHERE k.user_id=u.id) AS keys "
        "FROM users u ORDER BY u.created_at DESC LIMIT ?", (min(limit, 500),), "all")
    return {"users": [dict(r) for r in rows]}


class UserToggleBody(BaseModel):
    disabled: bool


@app.post("/api/admin/users/{uid}/toggle")
def admin_toggle_user(uid: int, body: UserToggleBody, request: Request):
    _require_admin(request)
    db_exec("UPDATE users SET disabled=? WHERE id=?", (1 if body.disabled else 0, uid))
    return {"ok": True}


@app.get("/api/admin/logs/web")
def admin_web_logs(request: Request, limit: int = 100):
    _require_admin(request)
    rows = db_exec("SELECT ts,ip,ua,link,ok,user_id FROM request_logs ORDER BY id DESC LIMIT ?",
                   (min(limit, 500),), "all")
    return {"logs": [dict(r) for r in rows]}


@app.get("/api/admin/logs/api")
def admin_api_logs(request: Request, limit: int = 100):
    _require_admin(request)
    rows = db_exec("SELECT a.ts,a.key,a.user_id,a.link,a.ok,a.cost_cents,a.job_id,u.email "
                   "FROM api_logs a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT ?",
                   (min(limit, 500),), "all")
    return {"logs": [dict(r) for r in rows]}


# ---- 用户自助 API 密钥（登录后）----

@app.get("/api/keys")
def user_list_keys(request: Request):
    u = current_user(request)
    if not u:
        raise ApiError(401, "请先登录")
    return {"keys": list_api_keys(u["id"]), "price_cents": api_price_cents()}


@app.post("/api/keys")
def user_create_key(body: NewKeyBody, request: Request):
    u = current_user(request)
    if not u:
        raise ApiError(401, "请先登录")
    if len(list_api_keys(u["id"])) >= 10:
        raise ApiError(400, "每个账号最多 10 个密钥")
    return create_api_key(u["id"], body.name)


@app.delete("/api/keys/{key}")
def user_revoke_key(key: str, request: Request):
    u = current_user(request)
    if not u:
        raise ApiError(401, "请先登录")
    if not revoke_api_key(key, u["id"]):
        raise ApiError(404, "密钥不存在或无权删除")
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


# ---------------------------------------------------------------- 页面 + SEO

def _origin(request: Request) -> str:
    """反代下取真实站点 origin，用于 canonical / og:url / sitemap。"""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    return f"{proto}://{host}"


SUPPORTED_LANGS = {"zh": "zh-CN", "en": "en"}


def _pick_lang(request: Request) -> str:
    q = (request.query_params.get("lang") or "").lower()
    if q in SUPPORTED_LANGS:
        return q
    c = (request.cookies.get("lang") or "").lower()
    if c in SUPPORTED_LANGS:
        return c
    al = (request.headers.get("accept-language") or "").lower()
    return "zh" if al.startswith("zh") or not al else ("en" if al[:2] not in ("zh",) else "zh")


def _seo_head(lang: str, origin: str, path: str = "/") -> str:
    """按语言生成整段 SEO 头（title/description/OG/Twitter/hreflang/JSON-LD）。"""
    zh = lang == "zh"
    base = f"{origin}{path}"
    canon = base if zh else f"{base}?lang=en"
    meta = {
        "zh": {
            "title": "抖音无水印下载器 · 粘贴链接即下",
            "desc": "免费的抖音无水印下载工具：粘贴分享链接，即可在线预览并下载抖音视频与图集的无水印原片。开源可信、零隐私采集、无需登录、永不接广告，由你的浏览器直连下载。",
            "kw": "抖音下载,抖音无水印下载,抖音视频下载,抖音去水印,douyin downloader,抖音图集下载,抖音解析,无水印下载器,抖音下载器在线,抖音API",
            "site": "抖音无水印下载器",
            "ogt": "抖音无水印下载器 · 开源可信 · 永不接广告",
            "ogd": "粘贴抖音分享链接，浏览器直连拿走无水印原片。开源、零隐私采集、无需登录、永不接广告，并提供开发者 API。",
            "locale": "zh_CN",
        },
        "en": {
            "title": "Douyin Downloader — No Watermark, Free & Open Source",
            "desc": "Free Douyin (Chinese TikTok) no-watermark downloader. Paste a share link to preview and download original videos & photo galleries — open-source, no login, no ads, browser-direct. Developer API available.",
            "kw": "douyin downloader,douyin video download,no watermark,tiktok downloader,save douyin video,douyin photo download,douyin api,open source downloader",
            "site": "Douyin Downloader",
            "ogt": "Douyin Downloader — No Watermark, Open Source, No Ads",
            "ogd": "Paste a Douyin share link and download the original no-watermark video directly in your browser. Open-source, no login, no ads. Developer API available.",
            "locale": "en_US",
        },
    }[lang]

    ld = {
        "zh": {
            "app_desc": "免登录、免签名的抖音视频与图集无水印下载工具，粘贴分享链接即可在线预览与下载，浏览器直连、开源、零隐私、永不接广告，并提供开发者 API。",
            "features": ["抖音视频无水印下载", "抖音图集下载", "在线预览播放", "批量解析", "浏览器直连不落地", "开发者 API"],
            "faq": [
                ("这个抖音下载器安全吗？会收集我的隐私吗？", "安全。前端代码完全开源、任何人可审查，不嵌入任何危险代码、不采集也不上传你的任何隐私。无需登录、不记录账号，视频由你的浏览器直连获取，我们不存储任何内容。"),
                ("需要登录或安装软件吗？", "不需要。纯网页工具，无需登录抖音账号、无需安装任何 App 或插件，粘贴分享链接即可使用。"),
                ("下载的抖音视频有水印吗？", "没有水印。下载的是无水印原片，也不会加入本站自己的二次水印。"),
                ("支持图集（图片作品）下载吗？", "支持。图集作品会自动识别，可逐张下载原图，也可批量下载。"),
                ("有没有 API 可以批量调用？", "有。登录后可在 API 控制台生成密钥，通过异步接口批量提交链接并查询结果，按次计费。"),
            ],
            "howto": ("如何下载抖音无水印视频", [
                ("复制分享链接", "在抖音 App 里点分享，复制作品链接或整段分享文案。"),
                ("粘贴并解析", "把链接粘贴到本站输入框，点击解析。"),
                ("预览并下载", "在线预览后，一键下载无水印原片到本地。")]),
        },
        "en": {
            "app_desc": "Login-free, signature-free Douyin video & photo-gallery downloader with no watermark. Paste a share link to preview and download in the browser. Open-source, privacy-first, no ads, with a developer API.",
            "features": ["Douyin no-watermark video download", "Photo gallery download", "In-browser preview", "Batch parsing", "Browser-direct (nothing stored)", "Developer API"],
            "faq": [
                ("Is this Douyin downloader safe? Does it collect my data?", "Yes, it's safe. The front-end is fully open-source and auditable; it embeds no malicious code and collects nothing. No login, no account logging — videos are fetched directly by your browser and nothing is stored on our servers."),
                ("Do I need to log in or install anything?", "No. It's a pure web tool — no Douyin login, no app or extension. Just paste a share link."),
                ("Do downloaded videos have a watermark?", "No. You get the original video with no watermark, and we never add our own."),
                ("Can I download photo galleries (image posts)?", "Yes. Image posts are detected automatically; download each original image or batch-download them."),
                ("Is there an API for bulk use?", "Yes. After signing in you can create an API key in the console, submit links in bulk via the async API and poll for results, billed per request."),
            ],
            "howto": ("How to download a Douyin video without watermark", [
                ("Copy the share link", "In the Douyin app tap Share and copy the link or the whole share text."),
                ("Paste and parse", "Paste the link into the input box and click Parse."),
                ("Preview and download", "Preview it, then download the original no-watermark file with one click.")]),
        },
    }[lang]

    graph = [
        {"@type": "WebApplication", "name": meta["site"], "url": f"{origin}/",
         "applicationCategory": "MultimediaApplication", "operatingSystem": "All",
         "offers": {"@type": "Offer", "price": "0", "priceCurrency": "CNY"},
         "description": ld["app_desc"], "featureList": ld["features"]},
        {"@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q,
             "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in ld["faq"]]},
        {"@type": "HowTo", "name": ld["howto"][0], "step": [
            {"@type": "HowToStep", "position": i + 1, "name": n, "text": t}
            for i, (n, t) in enumerate(ld["howto"][1])]},
    ]
    jsonld = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False)

    def esc(s):
        return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")

    return f'''<title>{esc(meta["title"])}</title>
<meta name="description" content="{esc(meta["desc"])}">
<meta name="keywords" content="{esc(meta["kw"])}">
<meta name="robots" content="index,follow,max-image-preview:large">
<meta name="theme-color" content="#0E1013">
<link rel="canonical" href="{canon}">
<link rel="alternate" hreflang="zh-CN" href="{base}">
<link rel="alternate" hreflang="en" href="{base}?lang=en">
<link rel="alternate" hreflang="x-default" href="{base}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="{esc(meta["site"])}">
<meta property="og:title" content="{esc(meta["ogt"])}">
<meta property="og:description" content="{esc(meta["ogd"])}">
<meta property="og:url" content="{canon}">
<meta property="og:image" content="{origin}/og.svg">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:locale" content="{meta["locale"]}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{esc(meta["ogt"])}">
<meta name="twitter:description" content="{esc(meta["ogd"])}">
<meta name="twitter:image" content="{origin}/og.svg">
<script type="application/ld+json">{jsonld}</script>
<script>window.__LANG={lang!r};window.__ORIGIN={origin!r};</script>'''


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    log_pageview(request)
    lang = _pick_lang(request)
    origin = _origin(request)
    html = Path("static/index.html").read_text("utf-8")
    html = (html.replace("{{HTMLLANG}}", SUPPORTED_LANGS[lang])
                .replace("{{SEO_HEAD}}", _seo_head(lang, origin))
                .replace("{{ORIGIN}}", origin))
    resp = HTMLResponse(html)
    resp.set_cookie("lang", lang, max_age=31536000, samesite="lax")
    return resp


@app.get("/api-docs", response_class=HTMLResponse)
def api_docs(request: Request):
    log_pageview(request)
    lang = _pick_lang(request)
    origin = _origin(request)
    html = Path("static/api-docs.html").read_text("utf-8")
    html = (html.replace("{{HTMLLANG}}", SUPPORTED_LANGS[lang])
                .replace("{{SEO_HEAD}}", _seo_head(lang, origin, "/api-docs"))
                .replace("{{ORIGIN}}", origin))
    resp = HTMLResponse(html)
    resp.set_cookie("lang", lang, max_age=31536000, samesite="lax")
    return resp


@app.get("/api/quota")
def api_quota(request: Request):
    """前端查询今日剩余免费次数。"""
    limit, used, remaining = quota_status(request)
    u = current_user(request)
    return {"limit": limit, "used": used, "remaining": remaining,
            "user_daily": FREE_USER_DAILY,
            "user": {"email": u["email"]} if u else None}


# ---------------------------------------------------------------- 用户鉴权 API

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.get("/api/auth/captcha")
def auth_captcha(request: Request):
    if not _captcha_rate_ok(_client_ip(request)):        # 防验证码 CPU-DoS
        raise ApiError(429, "操作过于频繁，请稍后再试")
    return make_captcha(request)


class CaptchaBody(BaseModel):
    cid: str = ""
    x: float = -1
    trajectory: list = []
    nonce: str = ""


@app.post("/api/auth/captcha/verify")
def auth_captcha_verify(body: CaptchaBody, request: Request):
    """滑块校验独立成步。通过后返回一次性通行令牌，注册/登录必须携带它。"""
    ok, err = verify_captcha(body.cid, body.x, body.trajectory, body.nonce, request)
    if not ok:
        raise ApiError(400, err)
    return {"ok": True, "pass_token": issue_pass(request)}


@app.get("/api/auth/me")
def auth_me(request: Request):
    u = current_user(request)
    if not u:
        return {"user": None}
    return {"user": {"email": u["email"], "id": u["id"], "created_at": u["created_at"]}}


class RegisterBody(BaseModel):
    email: str
    password: str
    pass_token: str = ""      # 滑块通过后签发的一次性令牌（缺它必拒）
    hp: str = ""              # 蜜罐字段，正常用户为空


def _do_auth_guard(request: Request, body: RegisterBody):
    if body.hp:                                     # 蜜罐命中 → 机器人
        raise ApiError(400, "验证失败")
    if not _auth_rate_ok(_client_ip(request)):
        raise ApiError(429, "操作过于频繁，请一小时后再试")
    if not consume_pass(body.pass_token, request):  # 必须先过滑块拿到令牌
        raise ApiError(400, "请先完成滑块验证（验证已失效，请重试）")


def _issue_session(uid: int) -> JSONResponse:
    tok = _new_user_session(uid)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("sess", tok, httponly=True, samesite="lax",
                    secure=COOKIE_SECURE, max_age=USER_SESSION_TTL)
    return resp


@app.post("/api/auth/register")
def auth_register(body: RegisterBody, request: Request):
    email = (body.email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise ApiError(400, "邮箱格式不正确")
    if len(body.password or "") < 6:
        raise ApiError(400, "密码至少 6 位")
    _do_auth_guard(request, body)
    if db_exec("SELECT id FROM users WHERE email=?", (email,), "one"):
        raise ApiError(409, "该邮箱已注册，请直接登录")
    salt, h = hash_pw(body.password)
    uid = db_exec("INSERT INTO users(email,pw_salt,pw_hash,created_at,last_login,reg_ip) "
                  "VALUES(?,?,?,?,?,?)",
                  (email, salt, h, int(time.time()), int(time.time()), _client_ip(request)))
    return _issue_session(uid)


@app.post("/api/auth/login")
def auth_login(body: RegisterBody, request: Request):
    email = (body.email or "").strip().lower()
    _do_auth_guard(request, body)
    row = db_exec("SELECT * FROM users WHERE email=?", (email,), "one")
    if not row or not verify_pw(body.password or "", row["pw_salt"], row["pw_hash"]):
        raise ApiError(403, "邮箱或密码错误")
    if row["disabled"]:
        raise ApiError(403, "该账号已被停用")
    db_exec("UPDATE users SET last_login=? WHERE id=?", (int(time.time()), row["id"]))
    return _issue_session(row["id"])


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    tok = request.cookies.get("sess", "")
    _user_sessions.pop(tok, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("sess")
    return resp


@app.get("/api-console")
def api_console():
    return FileResponse("static/api-console.html")


@app.get("/admin_d")
def admin_page():
    return FileResponse("static/admin.html")


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots(request: Request):
    return (f"User-agent: *\nAllow: /\nDisallow: /admin_d\nDisallow: /api/\n\n"
            f"Sitemap: {_origin(request)}/sitemap.xml\n")


@app.get("/sitemap.xml")
def sitemap(request: Request):
    o = _origin(request)

    def entry(path, pri):
        return (f'  <url><loc>{o}{path}</loc>'
                f'<xhtml:link rel="alternate" hreflang="zh-CN" href="{o}{path}"/>'
                f'<xhtml:link rel="alternate" hreflang="en" href="{o}{path}?lang=en"/>'
                f'<changefreq>daily</changefreq><priority>{pri}</priority></url>\n')

    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
           'xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
           + entry("/", "1.0") + entry("/api-docs", "0.7")
           + '</urlset>\n')
    return Response(xml, media_type="application/xml")


@app.get("/og.svg")
def og_image():
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
<defs>
<radialGradient id="g1" cx="18%" cy="20%" r="60%"><stop offset="0" stop-color="#FE2C55" stop-opacity=".55"/><stop offset="1" stop-color="#FE2C55" stop-opacity="0"/></radialGradient>
<radialGradient id="g2" cx="86%" cy="18%" r="55%"><stop offset="0" stop-color="#25F4EE" stop-opacity=".45"/><stop offset="1" stop-color="#25F4EE" stop-opacity="0"/></radialGradient>
<linearGradient id="t" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#FE2C55"/><stop offset="1" stop-color="#25F4EE"/></linearGradient>
</defs>
<rect width="1200" height="630" fill="#0E1013"/>
<rect width="1200" height="630" fill="url(#g1)"/><rect width="1200" height="630" fill="url(#g2)"/>
<g transform="translate(90,150)" fill="none" stroke-width="11" stroke-linecap="round" stroke-linejoin="round">
<path d="M40 0 V70 M8 45 l32 32 32-32" stroke="#25F4EE" transform="translate(-4 0)"/>
<path d="M40 0 V70 M8 45 l32 32 32-32" stroke="#FE2C55" transform="translate(4 0)"/>
<path d="M40 0 V70 M8 45 l32 32 32-32" stroke="#fff"/><path d="M0 92 h80" stroke="#fff"/>
</g>
<text x="90" y="360" font-family="PingFang SC,Noto Sans SC,sans-serif" font-size="96" font-weight="800" fill="#fff">抖音无水印下载器</text>
<text x="94" y="450" font-family="PingFang SC,Noto Sans SC,sans-serif" font-size="42" font-weight="700" fill="url(#t)">开源可信 · 零隐私 · 永不接广告 · 稳定下载</text>
<text x="94" y="524" font-family="PingFang SC,Noto Sans SC,sans-serif" font-size="34" fill="#8A93A0">粘贴分享链接，浏览器直连拿走无水印原片</text>
</svg>'''
    return Response(svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/healthz")
def healthz():
    return {"ok": True, "proxies": len(proxy_mgr.proxies),
            "enabled": sum(p["enabled"] for p in proxy_mgr.proxies)}
