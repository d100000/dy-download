#!/usr/bin/env python3
"""多平台无水印下载器 · Web 服务版（抖音官方解析 + 兼容平台解析）

抖音作品链接通过官方网页/公开接口获取元数据和短时签名媒体地址；其他平台继续
使用配置的兼容解析服务。服务端只保留必要的短时缓存与分享快照，不落地媒体文件。
普通解析默认不请求语音文案。视频可浏览器直连，也可通过受签名保护且支持 Range
的同源流播放/下载。

反封锁能力：
  · 代理 IP 池（http/https/socks5），统一解析链路按策略轮换代理
  · 失败自动转移到下一个代理 + 失败计数退避
  · 移动端 UA 池轮换 + Referer 伪装
  · 管理后台（密码鉴权）增删/启停/测试代理、查看出口 IP 与统计

启动:  uvicorn server:app --host 0.0.0.0 --port 8000 --no-access-log
环境变量:  ADMIN_PASSWORD  管理后台密码（默认 douyin-admin，生产务必修改）
"""

import base64
from contextlib import contextmanager
import gzip
import hashlib
import hmac
import ipaddress
import io
import json
import math
import os
import platform
import queue
import random
import re
import secrets
import signal
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
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
from pydantic import BaseModel, Field, StrictInt

# ---------------------------------------------------------------- 常量与存储

# 版本号（语义化：修 bug +patch，新功能 +minor，不兼容改动 +major）。
# 每次改动必须同步更新 README.md 顶部版本号与「更新日志」，规则见 CLAUDE.md。
APP_VERSION = "1.29.2"
UI_MESSAGES = json.loads(Path("static/ui-locales.json").read_text("utf-8"))


def _ui_text(message: str, lang: str, fallback: Optional[str] = None) -> str:
    return UI_MESSAGES.get(lang, {}).get(message, fallback or message) if lang != "zh" else message

_BUILD_DATE = time.strftime("%Y-%m-%d", time.gmtime())  # 进程启动日期，供 sitemap lastmod

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
try:
    DATA_DIR.chmod(0o700)
except OSError:
    pass
STORE_FILE = DATA_DIR / "config.json"


def _load_app_secret() -> bytes:
    """读取稳定密钥；未配置时用完整临时文件 + hard-link 原子持久化。"""
    configured = (os.environ.get("APP_SECRET")
                  or os.environ.get("CAPTCHA_SECRET") or "").strip()
    if configured:
        if len(configured.encode()) < 32:
            raise RuntimeError("APP_SECRET/CAPTCHA_SECRET 至少需要 32 字节")
        return configured.encode()
    path = DATA_DIR / ".app-secret"
    try:
        saved = path.read_text("utf-8").strip()
        if saved:
            path.chmod(0o600)
            return saved.encode()
    except FileNotFoundError:
        pass

    candidate = secrets.token_urlsafe(48)
    tmp = DATA_DIR / f".app-secret.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(candidate)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    saved = path.read_text("utf-8").strip()
    if not saved:
        raise RuntimeError(f"应用密钥文件为空，请删除后重启或设置 APP_SECRET：{path}")
    path.chmod(0o600)
    return saved.encode()


APP_SECRET = _load_app_secret()

_admin_password_env = os.environ.get("ADMIN_PASSWORD")
_require_admin_password = os.environ.get(
    "REQUIRE_ADMIN_PASSWORD", "").lower() in ("1", "true", "yes", "on")
if _admin_password_env is None:
    if _require_admin_password:
        raise RuntimeError("ADMIN_PASSWORD 未设置；当前部署禁止使用默认管理密码")
    ADMIN_PASSWORD = "douyin-admin"
else:
    # 显式传入空值时必须拒绝启动；否则 compare_digest('', '') 会让后台
    # 允许空密码登录。默认值仅用于本地开发，生产环境仍应显式覆盖。
    if not _admin_password_env.strip():
        raise RuntimeError("ADMIN_PASSWORD 不能为空；请设置强密码后再启动")
    ADMIN_PASSWORD = _admin_password_env
if (_require_admin_password
        and (ADMIN_PASSWORD == "douyin-admin" or len(ADMIN_PASSWORD) < 12)):
    raise RuntimeError("ADMIN_PASSWORD 至少需要 12 位，且不能使用默认密码")
if ADMIN_PASSWORD == "douyin-admin":
    import sys as _sys
    print("⚠️  警告：正在使用默认管理员密码，请设置环境变量 ADMIN_PASSWORD 后再对外部署！",
          file=_sys.stderr)

# 免费使用配额（防薅羊毛）
FREE_ANON_DAILY = int(os.environ.get("FREE_ANON_DAILY", "3"))    # 匿名：每天 3 次
FREE_USER_DAILY = int(os.environ.get("FREE_USER_DAILY", "10"))   # 后台未设置时的默认值


def _clamped_env_int(name: str, default: int,
                     minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


MEDIA_TOKEN_TTL = max(300, min(86400, int(os.environ.get("MEDIA_TOKEN_TTL", "43200"))))
MEDIA_REQUESTS_PER_MIN = max(10, int(os.environ.get("MEDIA_REQUESTS_PER_MIN", "120")))
MEDIA_MAX_CONCURRENT = max(1, int(os.environ.get("MEDIA_MAX_CONCURRENT", "6")))
IMAGE_REQUESTS_PER_MIN = _clamped_env_int(
    "IMAGE_REQUESTS_PER_MIN", 240, MEDIA_REQUESTS_PER_MIN, 1000)
IMAGE_MAX_BYTES = _clamped_env_int(
    "IMAGE_MAX_BYTES", 50 * 1024 * 1024, 1024 * 1024, 100 * 1024 * 1024)
MEDIA_RESUME_MAX_ATTEMPTS = _clamped_env_int(
    "MEDIA_RESUME_MAX_ATTEMPTS", 64, 1, 256)
MEDIA_RESUME_MAX_SECONDS = _clamped_env_int(
    "MEDIA_RESUME_MAX_SECONDS", 3600, 30, 7200)
MEDIA_RESUME_MAX_FAILURES = _clamped_env_int(
    "MEDIA_RESUME_MAX_FAILURES", 8, 2, 16)
DATA_RETENTION_DAYS = max(
    1, min(30, int(os.environ.get("DATA_RETENTION_DAYS", "30")))
)
API_JOB_WORKERS = max(1, min(8, int(os.environ.get("API_JOB_WORKERS", "2"))))
QUOTA_RESERVATION_TTL = max(300, int(os.environ.get("QUOTA_RESERVATION_TTL", "3600")))
ASYNC_SHARE_BODY_MAX = _clamped_env_int(
    "ASYNC_SHARE_BODY_MAX", 8192, 1024, 65536)
PARSE_TEXT_MAX = _clamped_env_int("PARSE_TEXT_MAX", 8192, 1024, 65536)
BATCH_TEXT_MAX = _clamped_env_int("BATCH_TEXT_MAX", 65536, 4096, 262144)
EXPORT_ITEMS_MAX = _clamped_env_int("EXPORT_ITEMS_MAX", 100, 1, 500)
API_JOB_BODY_MAX = _clamped_env_int("API_JOB_BODY_MAX", 131072, 8192, 1048576)
EXPORT_BODY_MAX = _clamped_env_int("EXPORT_BODY_MAX", 2097152, 65536, 8388608)

# ---------------------------------------------------------------- SQLite 数据层

DB_FILE = DATA_DIR / "app.db"
_db_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_daily(
  day INTEGER, subject TEXT, count INTEGER DEFAULT 0,
  PRIMARY KEY(day, subject)
);
CREATE TABLE IF NOT EXISTS quota_reservations(
  id TEXT PRIMARY KEY, day INTEGER, subjects TEXT, units INTEGER,
  committed_units INTEGER DEFAULT 0, status TEXT,
  endpoint TEXT, created INTEGER, settled INTEGER, lease_until INTEGER
);
CREATE INDEX IF NOT EXISTS idx_quota_reservation_lease
  ON quota_reservations(status, lease_until);
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
  created_at INTEGER, last_login INTEGER, disabled INTEGER DEFAULT 0, reg_ip TEXT,
  api_trial_granted_cents INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS user_sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS wallet_ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, ts INTEGER NOT NULL,
  event TEXT NOT NULL, event_key TEXT NOT NULL, request_hash TEXT,
  balance_delta INTEGER DEFAULT 0, reserved_delta INTEGER DEFAULT 0,
  spent_delta INTEGER DEFAULT 0, note TEXT DEFAULT '',
  UNIQUE(user_id,event_key)
);
CREATE INDEX IF NOT EXISTS idx_user_sessions_expiry ON user_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id);
CREATE TABLE IF NOT EXISTS api_keys(
  key TEXT PRIMARY KEY, user_id INTEGER, name TEXT, created INTEGER, enabled INTEGER DEFAULT 1,
  balance_cents INTEGER DEFAULT 100, spent_cents INTEGER DEFAULT 0, calls INTEGER DEFAULT 0,
  last_used INTEGER, reserved_cents INTEGER DEFAULT 0, deleted_at INTEGER
);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, key TEXT, user_id INTEGER, status TEXT, total INTEGER, done INTEGER DEFAULT 0,
  ok INTEGER DEFAULT 0, cost_cents INTEGER DEFAULT 0, links TEXT, results TEXT,
  created INTEGER, finished INTEGER, price_cents INTEGER DEFAULT 0,
  updated INTEGER, request_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_key ON jobs(key);
CREATE TABLE IF NOT EXISTS job_items(
  job_id TEXT, idx INTEGER, link TEXT, status TEXT DEFAULT 'pending',
  price_cents INTEGER DEFAULT 0, reserved INTEGER DEFAULT 0,
  result TEXT, error TEXT, attempts INTEGER DEFAULT 0,
  lease_owner TEXT, lease_until INTEGER,
  started INTEGER, finished INTEGER,
  PRIMARY KEY(job_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_job_items_status ON job_items(status, job_id);
CREATE INDEX IF NOT EXISTS idx_job_items_claim
  ON job_items(status, lease_until, job_id, idx);
CREATE TABLE IF NOT EXISTS api_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, key TEXT, user_id INTEGER,
  link TEXT, ok INTEGER, cost_cents INTEGER, job_id TEXT, item_idx INTEGER
);
CREATE INDEX IF NOT EXISTS idx_apilog_ts ON api_logs(ts);
CREATE TABLE IF NOT EXISTS api_ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, key TEXT,
  job_id TEXT, item_idx INTEGER, event TEXT,
  balance_delta INTEGER DEFAULT 0, reserved_delta INTEGER DEFAULT 0,
  spent_delta INTEGER DEFAULT 0, calls_delta INTEGER DEFAULT 0, reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_job ON api_ledger(job_id, item_idx);
CREATE TABLE IF NOT EXISTS app_settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS shares(
  id TEXT PRIMARY KEY, item_id TEXT, kind TEXT, vid TEXT,
  owner_user_id INTEGER, owner_fp TEXT, owner_ip TEXT,
  title TEXT, author TEXT, avatar TEXT, cover TEXT,
  payload TEXT, custom_title TEXT,
  visibility TEXT DEFAULT 'link', pw_salt TEXT, pw_hash TEXT,
  expires_at INTEGER DEFAULT 0, refreshed_at INTEGER,
  status TEXT DEFAULT 'ok',
  views INTEGER DEFAULT 0, plays INTEGER DEFAULT 0,
  downloads INTEGER DEFAULT 0, cta_clicks INTEGER DEFAULT 0,
  created INTEGER,
  parse_status TEXT DEFAULT 'ready', source_url TEXT,
  parse_error_code TEXT, attempts INTEGER DEFAULT 0,
  next_attempt_at INTEGER DEFAULT 0, lease_owner TEXT, lease_until INTEGER,
  quota_reservation_id TEXT, owner_scope TEXT, idem_key_hash TEXT,
  request_hash TEXT, source_hash TEXT, assigned_origin TEXT,
  updated INTEGER, ready_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_shares_owner ON shares(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_shares_item ON shares(item_id);
-- 成功解析的临时快照；来源独立保存，不向分享访客公开。
CREATE TABLE IF NOT EXISTS parse_snapshots(
  item_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
  created INTEGER NOT NULL, expires_at INTEGER NOT NULL,
  source_url TEXT, canonical_url TEXT
);
CREATE INDEX IF NOT EXISTS idx_parse_snapshots_expiry ON parse_snapshots(expires_at);
CREATE TABLE IF NOT EXISTS share_submissions(
  sid TEXT PRIMARY KEY, ts INTEGER, owner_scope TEXT, ip_scope TEXT,
  user_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_share_submit_owner
  ON share_submissions(owner_scope, ts);
CREATE INDEX IF NOT EXISTS idx_share_submit_ip
  ON share_submissions(ip_scope, ts);
CREATE INDEX IF NOT EXISTS idx_share_submit_ts ON share_submissions(ts);
CREATE TABLE IF NOT EXISTS blocked_share_items(
  kind TEXT, item_id TEXT, created INTEGER,
  PRIMARY KEY(kind, item_id)
);
CREATE TABLE IF NOT EXISTS blocked_share_sources(
  source_hash TEXT PRIMARY KEY, created INTEGER
);
CREATE TABLE IF NOT EXISTS share_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, sid TEXT, kind TEXT,
  ip TEXT, ua TEXT, referer TEXT, wechat INTEGER, fp TEXT,
  source TEXT, stage TEXT, detail TEXT, ms INTEGER, event_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_share_ev ON share_events(ts, sid);
CREATE INDEX IF NOT EXISTS idx_share_ev_kind ON share_events(kind, ts);
CREATE TABLE IF NOT EXISTS reports(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, sid TEXT,
  reason TEXT, contact TEXT, ip TEXT, handled INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS media_traffic(
  day INTEGER, scope TEXT,
  requests INTEGER DEFAULT 0, bytes INTEGER DEFAULT 0,
  PRIMARY KEY(day, scope)
);
-- 兼容平台解析：结果缓存（按 item_id 全站共享，热门视频只调一次 API）
CREATE TABLE IF NOT EXISTS atc_cache(
  item_id TEXT PRIMARY KEY,
  work_url TEXT,
  video_url TEXT, url_fetched_at INTEGER,
  content TEXT, text_content TEXT,
  audio_url TEXT, duration REAL,
  created INTEGER, updated INTEGER
);
-- ATC 内部任务队列（对方并发上限 5，串行化提交；重启后按 task_id 续查）
CREATE TABLE IF NOT EXISTS atc_jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT, work_url TEXT,
  purpose TEXT,
  task_id TEXT,
  status TEXT DEFAULT 'pending',
  error TEXT, created INTEGER, updated INTEGER,
  lease_owner TEXT, lease_until INTEGER
);
CREATE INDEX IF NOT EXISTS idx_atc_jobs_status ON atc_jobs(status, id);
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


def _privacy_hash(kind: str, value: str, scope: str = "") -> str:
    """把网络标识转为本站不可逆 HMAC，避免在数据库中保存原始 IP/指纹。"""
    value = str(value or "").strip()
    if not value:
        return ""
    digest = hmac.new(APP_SECRET, f"{kind}:{scope}:{value}".encode(),
                      hashlib.sha256).hexdigest()[:24]
    return f"h:{digest}"


def _safe_referer(value: str) -> str:
    """埋点只保留来源的 scheme/host/path，丢弃可能含个人信息的 query/fragment。"""
    try:
        p = urlparse.urlsplit(value or "")
        return urlparse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))[:200]
    except Exception:
        return ""


def _migrate_privacy_data(conn) -> None:
    """一次性把老库中的原始网络标识就地改成带 h: 前缀的不可逆 HMAC。"""
    columns = (("request_logs", "ip", "request-ip"),
               ("page_views", "ip", "analytics-visitor"))
    for table, col, kind in columns:
        rows = conn.execute(
            f"SELECT rowid,{col} FROM {table} "
            f"WHERE COALESCE({col},'')<>'' AND {col} NOT LIKE 'h:%'"
        ).fetchall()
        for rowid, value in rows:
            conn.execute(f"UPDATE {table} SET {col}=? WHERE rowid=?",
                         (_privacy_hash(kind, value), rowid))

    rows = conn.execute(
        "SELECT rowid,subject FROM request_logs "
        "WHERE COALESCE(subject,'')<>'' AND subject NOT LIKE 'h:%' "
        "AND subject NOT LIKE 'user:%'"
    ).fetchall()
    for rowid, value in rows:
        conn.execute("UPDATE request_logs SET subject=? WHERE rowid=?",
                     (_privacy_hash("request-subject", value), rowid))
    rows = conn.execute(
        "SELECT rowid,link FROM request_logs WHERE COALESCE(link,'')<>'' "
        "AND link NOT LIKE 'h:%'"
    ).fetchall()
    for rowid, value in rows:
        conn.execute("UPDATE request_logs SET link=? WHERE rowid=?",
                     (_privacy_hash("submitted-link", value)[:26], rowid))

    # 这些旧字段没有业务读取用途，直接清空比继续保留可关联摘要更符合最小化原则。
    conn.execute("UPDATE users SET reg_ip='' WHERE COALESCE(reg_ip,'')<>''")
    conn.execute(
        "UPDATE shares SET owner_ip='',owner_fp='' "
        "WHERE COALESCE(owner_ip,'')<>'' OR COALESCE(owner_fp,'')<>''")
    conn.execute(
        "UPDATE share_events SET ip='',fp='',referer='',ua='' "
        "WHERE COALESCE(ip,'')<>'' OR COALESCE(fp,'')<>'' "
        "OR COALESCE(referer,'')<>'' OR COALESCE(ua,'')<>''")
    conn.execute(
        "UPDATE page_views SET ua='',fp='' "
        "WHERE COALESCE(ua,'')<>'' OR COALESCE(fp,'')<>''")
    conn.execute("UPDATE request_logs SET ua='' WHERE COALESCE(ua,'')<>''")
    conn.execute("UPDATE reports SET ip='' WHERE COALESCE(ip,'')<>''")

    # 免费额度主体也不能含原始 IP/浏览器指纹；迁移时取 MAX 防止计数被意外叠加。
    rows = conn.execute(
        "SELECT day,subject,count FROM usage_daily "
        "WHERE (subject LIKE 'ip:%' AND subject NOT LIKE 'ip:h:%') "
        "OR (subject LIKE 'fp:%' AND subject NOT LIKE 'fp:h:%')"
    ).fetchall()
    for day, subject, count in rows:
        kind, raw = subject.split(":", 1)
        new_subject = f"{kind}:{_privacy_hash(f'quota-{kind}', raw, str(day))}"
        conn.execute(
            "INSERT INTO usage_daily(day,subject,count) VALUES(?,?,?) "
            "ON CONFLICT(day,subject) DO UPDATE SET count=MAX(count,excluded.count)",
            (day, new_subject, count))
        conn.execute("DELETE FROM usage_daily WHERE day=? AND subject=?",
                     (day, subject))

with _db_lock:
    _c = _db()
    _c.execute("PRAGMA journal_mode=WAL")      # 允许并发读，写不阻塞读
    _c.executescript(_SCHEMA)
    _c.execute("CREATE INDEX IF NOT EXISTS idx_reqlog_user ON request_logs(user_id)")
    # 就地补列（无迁移框架）：所有 ALTER 都按 PRAGMA 探测，老库可直接升级。
    def _ensure_columns(table, columns):
        have = {r[1] for r in _c.execute(f"PRAGMA table_info({table})")}
        for col, typ in columns:
            if col not in have:
                _c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    _ensure_columns("share_events", (
        ("source", "TEXT"), ("stage", "TEXT"), ("detail", "TEXT"), ("ms", "INTEGER"),
        ("next_src", "TEXT"), ("event_key", "TEXT")))
    _ensure_columns("users", (
        ("api_trial_granted_cents", "INTEGER DEFAULT 0"),
        ("balance_cents", "INTEGER NOT NULL DEFAULT 0"),
        ("reserved_cents", "INTEGER NOT NULL DEFAULT 0"),
        ("spent_cents", "INTEGER NOT NULL DEFAULT 0"),
        ("wallet_version", "INTEGER NOT NULL DEFAULT 0")))
    _ensure_columns("quota_reservations", (
        ("user_id", "INTEGER"), ("free_units", "INTEGER"),
        ("price_cents", "INTEGER NOT NULL DEFAULT 0")))
    _ensure_columns("api_keys", (
        ("reserved_cents", "INTEGER DEFAULT 0"), ("deleted_at", "INTEGER")))
    _ensure_columns("jobs", (
        ("price_cents", "INTEGER DEFAULT 0"), ("updated", "INTEGER"),
        ("request_id", "TEXT")))
    _ensure_columns("job_items", (
        ("price_cents", "INTEGER DEFAULT 0"), ("reserved", "INTEGER DEFAULT 0"),
        ("result", "TEXT"), ("error", "TEXT"),
        ("attempts", "INTEGER DEFAULT 0"), ("lease_owner", "TEXT"),
        ("lease_until", "INTEGER"), ("started", "INTEGER"), ("finished", "INTEGER")))
    _ensure_columns("api_logs", (("item_idx", "INTEGER"),))
    _ensure_columns("shares", (
        ("parse_status", "TEXT DEFAULT 'ready'"), ("source_url", "TEXT"),
        ("parse_error_code", "TEXT"), ("attempts", "INTEGER DEFAULT 0"),
        ("next_attempt_at", "INTEGER DEFAULT 0"), ("lease_owner", "TEXT"),
        ("lease_until", "INTEGER"), ("quota_reservation_id", "TEXT"),
        ("owner_scope", "TEXT"), ("idem_key_hash", "TEXT"),
        ("request_hash", "TEXT"), ("source_hash", "TEXT"),
        ("assigned_origin", "TEXT"), ("updated", "INTEGER"),
        ("ready_at", "INTEGER")))
    _ensure_columns("atc_cache", (("work_url", "TEXT"),))
    _ensure_columns("parse_snapshots", (("source_url", "TEXT"), ("canonical_url", "TEXT")))
    # 升级时把旧媒体缓存的来源转存到元数据记录，后续清理媒体缓存不丢刷新依据。
    for _table in ("parse_snapshots", "shares"):
        _c.execute(
            f"UPDATE {_table} SET source_url=(SELECT work_url FROM atc_cache "
            f"WHERE atc_cache.item_id={_table}.item_id) WHERE COALESCE(source_url,'')='' "
            f"AND EXISTS(SELECT 1 FROM atc_cache WHERE atc_cache.item_id={_table}.item_id "
            "AND COALESCE(work_url,'')<>'')")
    _ensure_columns("atc_jobs", (
        ("lease_owner", "TEXT"), ("lease_until", "INTEGER"),
        ("quota_reservation_id", "TEXT")))
    _c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idem ON jobs(key,request_id) "
        "WHERE request_id IS NOT NULL")
    _c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_apilog_item ON api_logs(job_id,item_idx) "
        "WHERE item_idx IS NOT NULL")
    _c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_item_reserve "
        "ON api_ledger(job_id,item_idx) WHERE event='reserve'")
    _c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_item_settle "
        "ON api_ledger(job_id,item_idx) WHERE event IN ('charge','refund')")
    _c.execute(
        "CREATE INDEX IF NOT EXISTS idx_job_items_claim "
        "ON job_items(status,lease_until,job_id,idx)")
    _c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_shares_idempotency "
        "ON shares(owner_scope,idem_key_hash) WHERE idem_key_hash IS NOT NULL")
    _c.execute(
        "CREATE INDEX IF NOT EXISTS idx_shares_parse_claim "
        "ON shares(parse_status,next_attempt_at,lease_until,created)")
    _c.execute(
        "CREATE INDEX IF NOT EXISTS idx_shares_owner_created "
        "ON shares(owner_scope,created)")
    _c.execute(
        "CREATE INDEX IF NOT EXISTS idx_shares_owner_parse "
        "ON shares(owner_scope,parse_status)")
    _c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_share_events_dedupe "
        "ON share_events(event_key) WHERE event_key IS NOT NULL")
    _c.execute(
        "CREATE INDEX IF NOT EXISTS idx_atc_jobs_claim "
        "ON atc_jobs(status,lease_until,id)")
    # 老库中已有密钥的用户已经领取过开通余额；把这个事实固化到
    # users，避免账本明细按保留期清理后又重新领取。
    _c.execute(
        "UPDATE users SET api_trial_granted_cents=? "
        "WHERE COALESCE(api_trial_granted_cents,0)=0 AND EXISTS("
        "SELECT 1 FROM api_keys WHERE api_keys.user_id=users.id)",
        # 此处发生在运行时配置定义之前；只需持久化“已领取”
        # 事实，不猜测历史赠送金额，也不受本次启动环境变量影响。
        (1,))
    _migrate_privacy_data(_c)
    _privacy_vacuum_needed = not _c.execute(
        "SELECT 1 FROM app_settings WHERE k='privacy_v2_vacuumed'"
    ).fetchone()
    _c.commit()
    if _privacy_vacuum_needed:
        try:
            _c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            _c.execute("VACUUM")
            _c.execute(
                "INSERT OR REPLACE INTO app_settings(k,v) VALUES('privacy_v2_vacuumed','1')")
            _c.commit()
        except sqlite3.OperationalError:
            # 滚动部署期间老进程可能仍占用 WAL；下次启动继续尝试，绝不标记为完成。
            pass
    _c.close()


def _secure_data_permissions() -> None:
    """限制数据库、WAL、代理配置和应用密钥仅供服务账号读写。"""
    try:
        DATA_DIR.chmod(0o700)
    except OSError:
        pass
    for path in (DB_FILE, Path(str(DB_FILE) + "-wal"), Path(str(DB_FILE) + "-shm"),
                 STORE_FILE, DATA_DIR / ".app-secret"):
        try:
            if path.exists():
                path.chmod(0o600)
        except OSError:
            pass


_secure_data_permissions()


# ---------------------------------------------------------------- 防薅羊毛 / 限频

def _today() -> int:
    return int(time.time() // 86400)


# 只有来自可信反代时才采信 X-Forwarded-For，否则客户端可伪造头绕过所有基于 IP 的风控。
# 设 TRUST_PROXY=1 表示部署在反代后（Nginx/Cloudflare 等），此时才读 XFF。
TRUST_PROXY = os.environ.get("TRUST_PROXY", "").lower() in ("1", "true", "yes")
TRUST_PROXY_HOPS = max(1, int(os.environ.get("TRUST_PROXY_HOPS", "1")))
# 会话 cookie 是否加 Secure（仅走 HTTPS 发送）。生产（反代/HTTPS）应为真；本地 http 调试默认关。
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true", "yes") or TRUST_PROXY


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "?"
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # proxy_add_x_forwarded_for 会保留客户端伪造的左侧值；从右侧按可信代理层数取值。
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if len(parts) >= TRUST_PROXY_HOPS:
                return parts[-TRUST_PROXY_HOPS][:64]
    return str(peer)[:64]


def _client_fp(request: Request) -> str:
    return (request.headers.get("x-fp") or "")[:64]


def _stored_ip(request: Request, purpose: str = "security",
               scope: str = "") -> str:
    return _privacy_hash(f"{purpose}-ip", _client_ip(request), scope)


def _stored_fp(request: Request, purpose: str = "security",
               scope: str = "") -> str:
    return _privacy_hash(f"{purpose}-visitor", _client_fp(request), scope)


def _coarse_ua(request: Request) -> str:
    """只保留兼容诊断需要的粗粒度环境，不落完整 UA、机型或 build 字符串。"""
    ua = request.headers.get("user-agent") or ""
    os_name = "ios" if re.search(r"iphone|ipad|ipod", ua, re.I) else (
        "android" if re.search(r"android", ua, re.I) else (
            "windows" if re.search(r"windows", ua, re.I) else (
                "macos" if re.search(r"mac os", ua, re.I) else "other")))
    browser = "wechat" if re.search(r"micromessenger", ua, re.I) else (
        "chrome" if re.search(r"(?:chrome|crios)/", ua, re.I) else (
            "safari" if re.search(r"safari/", ua, re.I) else "other"))
    m = re.search(r"(?:MicroMessenger|Chrome|CriOS|Version)/(\d+)", ua, re.I)
    return f"{browser}/{m.group(1) if m else 'x'} {os_name}"


def _log_link(value: str) -> str:
    """运营日志只存用途隔离的短摘要，不保存用户粘贴的完整链接/文案。"""
    if not value:
        return ""
    return _privacy_hash("submitted-link", value)[:26]


def _free_user_daily_in_conn(conn) -> int:
    row = conn.execute("SELECT v FROM app_settings WHERE k='free_user_daily'").fetchone()
    try:
        value = int(row[0]) if row else FREE_USER_DAILY
    except (TypeError, ValueError):
        value = FREE_USER_DAILY
    return max(0, min(10000, value))


def free_user_daily() -> int:
    """登录用户的每日免费解析次数；后台保存后立即生效。"""
    with _db_lock:
        conn = _db()
        try:
            return _free_user_daily_in_conn(conn)
        finally:
            conn.close()


def _quota_subjects(request: Request):
    """登录用户按账户和后台限额计数；匿名按用途摘要和独立限额计数。"""
    u = current_user(request)
    if u:
        return [f"user:{u['id']}"], free_user_daily()
    scope = str(_today())
    subs = [f"ip:{_stored_ip(request, 'quota', scope)}"]
    fp = _stored_fp(request, "quota", scope)
    if fp:
        subs.append(f"fp:{fp}")
    return subs, FREE_ANON_DAILY


def quota_status(request: Request):
    """返回 (limit, used, remaining)。"""
    day = _today()
    subs, limit = _quota_subjects(request)
    marks = ",".join("?" for _ in subs)
    with _db_lock:
        conn = _db()
        try:
            rows = conn.execute(
                f"SELECT count FROM usage_daily WHERE day=? AND subject IN ({marks})",
                (day, *subs)).fetchall()
        finally:
            conn.close()
    used = max((int(r[0]) for r in rows), default=0)
    return limit, used, max(0, limit - used)


def _web_price_in_conn(conn, purpose: str = "parse") -> int:
    key = "transcript_price_cents" if purpose == "transcript" else "web_parse_price_cents"
    row = conn.execute("SELECT v FROM app_settings WHERE k=?", (key,)).fetchone()
    try:
        return max(0, min(100000, int(row[0]))) if row else 3
    except (TypeError, ValueError):
        return 3


def _wallet_change(conn, uid: int, event: str, event_key: str,
                   balance: int = 0, reserved: int = 0, spent: int = 0,
                   note: str = "", request_hash: str = "") -> None:
    """仅在写事务内调用；余额与不可重复的流水一起提交。"""
    changed = conn.execute(
        "UPDATE users SET balance_cents=balance_cents+?,reserved_cents=reserved_cents+?,"
        "spent_cents=spent_cents+?,wallet_version=wallet_version+1 "
        "WHERE id=? AND balance_cents+?>=0 AND reserved_cents+?>=0 AND spent_cents+?>=0",
        (balance, reserved, spent, uid, balance, reserved, spent)).rowcount
    if changed != 1:
        raise ApiError(409, "账户余额已变化，请刷新后重试")
    conn.execute(
        "INSERT INTO wallet_ledger(user_id,ts,event,event_key,request_hash,"
        "balance_delta,reserved_delta,spent_delta,note) VALUES(?,?,?,?,?,?,?,?,?)",
        (uid, int(time.time()), event, event_key, request_hash, balance, reserved, spent, note))


def _wallet_public(row) -> dict:
    return {k: int(row[k] or 0) for k in
            ("balance_cents", "reserved_cents", "spent_cents", "wallet_version")}


def _reserve_quota_in_conn(conn, day: int, subjects: list[str], limit: int,
                           n: int = 1, partial: bool = False,
                           endpoint: str = "web") -> dict:
    """在调用者已经开启的写事务里预占额度。异步任务用它把占位页和额度原子落库。"""
    marks = ",".join("?" for _ in subjects)
    rows = conn.execute(
        f"SELECT count FROM usage_daily WHERE day=? AND subject IN ({marks})",
        (day, *subjects)).fetchall()
    used = max((int(r[0]) for r in rows), default=0)
    available = max(0, limit - used)
    # 同一事务读取价格和余额：免费优先，余额只为超出免费部分预授权。
    subject = subjects[0] if len(subjects) == 1 else ""
    match = re.fullmatch(r"(?:atc:)?user:(\d+)", subject)
    uid = int(match[1]) if match else None
    user = conn.execute("SELECT * FROM users WHERE id=? AND disabled=0", (uid,)).fetchone() if uid else None
    price = _web_price_in_conn(conn, "transcript" if endpoint == "atc_transcript" else "parse")
    affordable = (int(user["balance_cents"]) // price if price else n) if user else 0
    capacity = available + affordable
    take = min(n, capacity) if partial else (n if n <= capacity else 0)
    free = min(take, available)
    charge = (take - free) * price
    reservation_id = ""
    if take:
        for subject in subjects:
            conn.execute(
                "INSERT INTO usage_daily(day,subject,count) VALUES(?,?,?) "
                "ON CONFLICT(day,subject) DO UPDATE SET count=count+excluded.count",
                (day, subject, free))
        reservation_id = "qr_" + secrets.token_urlsafe(12)
        now = int(time.time())
        conn.execute(
            "INSERT INTO quota_reservations("
            "id,day,subjects,units,committed_units,status,endpoint,created,lease_until,"
            "user_id,free_units,price_cents"
            ") VALUES(?,?,?,?,0,'pending',?,?,?,?,?,?)",
            (reservation_id, day, json.dumps(subjects, ensure_ascii=False),
             take, endpoint[:40], now, now + QUOTA_RESERVATION_TTL,
             uid if user else None, free, price))
        if charge:
            _wallet_change(conn, uid, "reserve", reservation_id + ":reserve",
                           balance=-charge, reserved=charge)
    return {"ok": take == n, "reserved": take, "limit": limit,
            "used_before": used, "used_after": used + free,
            "remaining": max(0, limit - used - free),
            "free_units": free, "price_cents": price, "reserved_cents": charge,
            "insufficient_balance": bool(uid and take < n),
            "day": day, "subjects": subjects, "id": reservation_id}


def reserve_quota(request: Request, n: int = 1, partial: bool = False,
                  endpoint: str = "web") -> dict:
    """在 BEGIN IMMEDIATE 事务中原子预占；失败调用可用 release_quota 精确退回。"""
    n = max(0, int(n))
    day = _today()
    subs, limit = _quota_subjects(request)
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            reservation = _reserve_quota_in_conn(
                conn, day, subs, limit, n, partial, endpoint)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return reservation


def _settle_quota_in_conn(conn, reservation_id: str,
                          committed_units: int) -> Optional[int]:
    """在调用者事务里幂等结算预占；返回实际提交数，已结算则返回 None。"""
    if not reservation_id:
        return None
    row = conn.execute(
        "SELECT * FROM quota_reservations WHERE id=? AND status='pending'",
        (reservation_id,)).fetchone()
    if not row:
        return None
    units = int(row["units"])
    committed = min(units, max(0, int(committed_units)))
    free = units if row["free_units"] is None else int(row["free_units"])
    refund = free - min(free, committed)
    for subject in json.loads(row["subjects"] or "[]"):
        conn.execute(
            "UPDATE usage_daily SET count=MAX(0,count-?) WHERE day=? AND subject=?",
            (refund, int(row["day"]), subject))
    held = (units - free) * int(row["price_cents"])
    cost = max(0, committed - free) * int(row["price_cents"])
    if held:
        _wallet_change(conn, row["user_id"], "settle", reservation_id + ":settle",
                       balance=held - cost, reserved=-held, spent=cost)
    status = "settled" if committed else "refunded"
    conn.execute(
        "UPDATE quota_reservations SET committed_units=?,status=?,settled=? "
        "WHERE id=? AND status='pending'",
        (committed, status, int(time.time()), reservation_id))
    return committed


def _admin_refund_quota_in_conn(conn, reservation_id: str) -> Optional[int]:
    """平台下架/删除专用：pending 直接退款，也可原子撤销已经结算的用量。"""
    settled = _settle_quota_in_conn(conn, reservation_id, 0)
    if settled is not None or not reservation_id:
        return settled
    row = conn.execute(
        "SELECT * FROM quota_reservations WHERE id=? AND status='settled' "
        "AND COALESCE(committed_units,0)>0", (reservation_id,)).fetchone()
    if not row:
        return None
    committed = int(row["committed_units"] or 0)
    free = int(row["units"]) if row["free_units"] is None else int(row["free_units"])
    cost = max(0, committed - free) * int(row["price_cents"])
    for subject in json.loads(row["subjects"] or "[]"):
        conn.execute(
            "UPDATE usage_daily SET count=MAX(0,count-?) WHERE day=? AND subject=?",
            (min(free, committed), int(row["day"]), subject))
    if cost:
        _wallet_change(conn, row["user_id"], "refund", reservation_id + ":admin_refund",
                       balance=cost, spent=-cost)
    changed = conn.execute(
        "UPDATE quota_reservations SET committed_units=0,status='refunded',settled=? "
        "WHERE id=? AND status='settled' AND COALESCE(committed_units,0)>0",
        (int(time.time()), reservation_id)).rowcount
    return 0 if changed == 1 else None


def settle_quota(reservation: Optional[dict], committed_units: int) -> None:
    """幂等结算持久化预占：成功次数保留，失败/未处理部分原子退款。"""
    if not reservation:
        return
    reservation_id = reservation.get("id", "")
    if not reservation_id:
        return
    committed_units = max(0, int(committed_units))
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            committed = _settle_quota_in_conn(
                conn, reservation_id, committed_units)
            if committed is None:
                conn.rollback()
                return
            conn.commit()
            reservation["reserved"] = committed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def release_quota(reservation: Optional[dict]) -> None:
    settle_quota(reservation, 0)


def _refund_stale_quota_reservations() -> int:
    """进程崩溃后，租约到期的网页额度预占会在后台自动全额退回。"""
    now = int(time.time())
    refunded = 0
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM quota_reservations "
                "WHERE status='pending' AND lease_until<? "
                "AND NOT EXISTS(SELECT 1 FROM shares s "
                "WHERE s.quota_reservation_id=quota_reservations.id "
                "AND s.parse_status IN ('pending','processing')) "
                "AND NOT EXISTS(SELECT 1 FROM atc_jobs j "
                "WHERE j.quota_reservation_id=quota_reservations.id "
                "AND j.status IN ('pending','submitting','submitted'))",
                (now,)).fetchall()
            for row in rows:
                _settle_quota_in_conn(conn, row["id"], 0)
                refunded += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return refunded


def log_request(request: Request, kind: str, link: str, ok: bool):
    try:
        u = current_user(request)
        db_exec("INSERT INTO request_logs(ts,kind,subject,ip,ua,link,ok,path,user_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (int(time.time()), kind,
                 (f"user:{u['id']}" if u else (
                     _stored_fp(request, "request", str(_today()))
                     or _stored_ip(request, "request", str(_today())))),
                 _stored_ip(request, "request", str(_today())),
                 _coarse_ua(request),
                 _log_link(link), 1 if ok else 0, request.url.path,
                 u["id"] if u else None))
    except Exception:
        pass


def log_pageview(request: Request):
    try:
        day = str(_today())
        visitor = (_stored_fp(request, "analytics", day)
                   or _stored_ip(request, "analytics", day))
        db_exec("INSERT INTO page_views(ts,ip,ua,path,fp) VALUES(?,?,?,?,?)",
                (int(time.time()), visitor, "", request.url.path, ""))
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


USER_SESSION_TTL = 30 * 86400


def _user_session_hash(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def _new_user_session(uid: int, previous_token: str = "") -> str:
    """浏览器保存随机 HttpOnly Cookie，数据库只存摘要；重启不丢会话。"""
    tok = secrets.token_urlsafe(32)
    now = int(time.time())
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO user_sessions(token_hash,user_id,created_at,expires_at) "
                "VALUES(?,?,?,?)", (_user_session_hash(tok), uid, now, now + USER_SESSION_TTL))
            if previous_token:
                conn.execute("DELETE FROM user_sessions WHERE token_hash=?",
                             (_user_session_hash(previous_token),))
            conn.commit()
        finally:
            conn.close()
    return tok


def _delete_user_session(tok: str):
    if tok:
        db_exec("DELETE FROM user_sessions WHERE token_hash=?", (_user_session_hash(tok),))


def current_user(request: Request):
    """从 cookie 取当前登录用户（dict）或 None。"""
    tok = request.cookies.get("sess", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", tok):
        return None
    row = db_exec(
        "SELECT u.* FROM users u JOIN user_sessions s ON s.user_id=u.id "
        "WHERE s.token_hash=? AND s.expires_at>? AND u.disabled=0",
        (_user_session_hash(tok), int(time.time())), "one")
    return dict(row) if row else None


# 算术题是滑块的前置门禁；答案不随响应下发，题目只能尝试一次。
_math_challenges: dict = {}   # cid -> (answer, expires_at, ip)
_math_grants: dict = {}       # token -> (expires_at, ip, remaining_slider_loads)
_math_lock = threading.Lock()
AUTH_MATH_TTL = 300


def _sweep_math(now: float):
    # 调用方持有 _math_lock。
    for cid, entry in list(_math_challenges.items()):
        if entry[1] <= now:
            _math_challenges.pop(cid, None)
    for token, entry in list(_math_grants.items()):
        if entry[0] <= now:
            _math_grants.pop(token, None)


def _make_math_challenge(request: Request) -> dict:
    a, b = secrets.randbelow(10) + 1, secrets.randbelow(10) + 1
    subtract = bool(secrets.randbelow(2))
    if subtract:
        a, b = max(a, b), min(a, b)
    cid = secrets.token_urlsafe(18)
    expires = int(time.time()) + AUTH_MATH_TTL
    with _math_lock:
        _sweep_math(time.time())
        if len(_math_challenges) >= 3000:
            raise ApiError(429, "验证请求较多，请稍后再试")
        _math_challenges[cid] = (a - b if subtract else a + b, expires, _client_ip(request))
    return {"cid": cid, "question": f"{a} {'−' if subtract else '+'} {b} = ?",
            "expires_at": expires}


def _verify_math_challenge(cid: str, answer: str, request: Request) -> str:
    with _math_lock:
        entry = _math_challenges.pop(cid, None)
        now = int(time.time())
        if not entry or entry[1] <= now or entry[2] != _client_ip(request):
            raise ApiError(400, "算术题已失效，请换一题重试")
        if not re.fullmatch(r"\d{1,2}", answer.strip()) or int(answer) != entry[0]:
            raise ApiError(400, "答案不正确，请回答新题目")
        _sweep_math(now)
        if len(_math_grants) >= 3000:
            raise ApiError(429, "验证请求较多，请稍后再试")
        token = secrets.token_urlsafe(24)
        _math_grants[token] = (now + AUTH_MATH_TTL, entry[2], 10)
    return token


def _require_math_grant(request: Request):
    token = request.headers.get("X-Auth-Math", "")
    with _math_lock:
        entry = _math_grants.get(token)
        if (not entry or entry[0] <= time.time() or entry[1] != _client_ip(request)
                or entry[2] <= 0):
            raise ApiError(403, "请先回答算术题，再进行滑块验证")
        _math_grants[token] = (entry[0], entry[1], entry[2] - 1)


# ---- 滑块验证码（服务端 PNG 缺口 + 行为轨迹 + PoW + 蜜罐 + 一次性签名令牌）----
# 缺口坐标只存在服务端与像素里，绝不出现在返回的标记中——无法靠抓包/解析拿到答案。
_captchas: dict = {}          # cid -> (gap_x, gap_y, issued_at, ip)
CAPTCHA_W, CAPTCHA_H, PIECE = 300, 170, 50
POW_BITS = 14                 # 工作量证明，抬高批量自动化成本
CAPTCHA_SECRET = (os.environ.get("CAPTCHA_SECRET") or "").encode() or APP_SECRET
# 未配置时使用 DATA_DIR/.app-secret；多 worker 也能共享稳定密钥。
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
    if not math.isfinite(x):
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
    if (not all(math.isfinite(value) for value in (*ts, *xs))
            or any(ts[i + 1] < ts[i] for i in range(len(ts) - 1))
            or any(abs(value) > 10000 for value in xs)):
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
_rate_lock = threading.RLock()
AUTH_MAX_PER_HOUR = 20
CAPTCHA_MAX_PER_MIN = 40


def _rate_ok(store: dict, ip: str, window: float, cap: int) -> bool:
    now = time.time()
    cap = max(1, int(cap))
    with _rate_lock:
        hits = [t for t in store.get(ip, []) if 0 <= now - t < window]
        if len(hits) >= cap:
            # 被拒绝的流量不得继续扩大记录，否则限流器本身会变成
            # 内存与 O(n) CPU 攻击面。
            store[ip] = hits[-cap:]
            return False
        hits.append(now)
        store[ip] = hits
        return True


def _auth_rate_ok(ip: str) -> bool:
    return _rate_ok(_auth_hits, ip, 3600, AUTH_MAX_PER_HOUR)


def _captcha_rate_ok(ip: str) -> bool:
    return _rate_ok(_captcha_hits, ip, 60, CAPTCHA_MAX_PER_MIN)


# 管理后台登录防爆破：单 IP 在窗口内失败超限即临时锁定（成功登录清零）。
# 注意与全站一致：只有 TRUST_PROXY=1 时 _client_ip 才采信 XFF，否则按直连 IP 计。
ADMIN_LOGIN_MAX_FAILS = 5
ADMIN_LOGIN_WINDOW = 900          # 15 分钟
_admin_fails: dict = {}           # ip -> [失败时间戳]


def _admin_fail_count(ip: str) -> int:
    now = time.time()
    with _rate_lock:
        fails = [t for t in _admin_fails.get(ip, [])
                 if 0 <= now - t < ADMIN_LOGIN_WINDOW]
        if fails:
            _admin_fails[ip] = fails[-ADMIN_LOGIN_MAX_FAILS:]
        else:
            _admin_fails.pop(ip, None)
        return len(fails)


def _admin_record_fail(ip: str):
    with _rate_lock:
        fails = _admin_fails.setdefault(ip, [])
        if len(fails) < ADMIN_LOGIN_MAX_FAILS:
            fails.append(time.time())


def _sweep_memory():
    """周期清理会话/令牌/限频等内存字典，防止无界增长。"""
    now = time.time()
    with _math_lock:
        _sweep_math(now)
    for tok, exp in list(_passes.items()):
        if exp < now:
            _passes.pop(tok, None)
    for cid, v in list(_captchas.items()):
        if now - v[2] > 300:
            _captchas.pop(cid, None)
    stores = [(_auth_hits, 3600), (_captcha_hits, 60),
              (globals().get("_share_hits", {}), 3600),
              (globals().get("_share_event_hits", {}), 60),
              (globals().get("_report_hits", {}), 3600),
              (_admin_fails, ADMIN_LOGIN_WINDOW)]
    with _rate_lock:
        for store, win in stores:
            for ip, hits in list(store.items()):
                fresh = [t for t in hits if 0 <= now - t < win]
                if fresh:
                    store[ip] = fresh
                else:
                    store.pop(ip, None)
    _sweep_media_limits()
    cleaner = globals().get("_sweep_douyin_memory")
    if cleaner:
        cleaner()


_last_data_cleanup = 0.0


def _cleanup_retained_data(force: bool = False) -> None:
    """清理超过保留期的访问/播放/任务明细；汇总计数与账户余额不受影响。"""
    global _last_data_cleanup
    now = time.time()
    if not force and now - _last_data_cleanup < 300:
        return
    cutoff = int(now) - DATA_RETENTION_DAYS * 86400
    cutoff_day = _today() - DATA_RETENTION_DAYS
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for table in ("request_logs", "page_views", "share_events", "api_logs"):
                conn.execute(f"DELETE FROM {table} WHERE ts<?", (cutoff,))
            # 投诉中的可选联系方式同样受保留期约束，不能因“未处理”而无限保存。
            conn.execute("DELETE FROM reports WHERE ts<?", (cutoff,))
            # 含今天在内只保留 30 个自然日桶，不能多留第 31 天。
            conn.execute("DELETE FROM usage_daily WHERE day<=?", (cutoff_day,))
            conn.execute(
                "DELETE FROM quota_reservations "
                "WHERE status<>'pending' AND COALESCE(settled,created)<?", (cutoff,))
            conn.execute(
                "DELETE FROM job_items WHERE job_id IN "
                "(SELECT id FROM jobs WHERE finished IS NOT NULL AND finished<?)",
                (cutoff,))
            conn.execute("DELETE FROM jobs WHERE finished IS NOT NULL AND finished<?",
                         (cutoff,))
            conn.execute("DELETE FROM api_ledger WHERE ts<?", (cutoff,))
            # 异步分享提交记录是不可退款的限频事实源，不随分享页删除；保留两天足够
            # 覆盖小时窗口和跨日边界，同时避免审计摘要无限增长。
            conn.execute("DELETE FROM share_submissions WHERE ts<?",
                         (int(now) - 2 * 86400,))
            conn.execute("DELETE FROM parse_snapshots WHERE expires_at<=?", (int(now),))
            conn.execute("DELETE FROM user_sessions WHERE expires_at<=? "
                         "OR user_id NOT IN (SELECT id FROM users WHERE disabled=0)", (int(now),))
            expired_shares = conn.execute(
                "SELECT quota_reservation_id FROM shares "
                "WHERE expires_at>0 AND expires_at<?", (int(now),)).fetchall()
            for share in expired_shares:
                _settle_quota_in_conn(conn, share["quota_reservation_id"], 0)
            conn.execute(
                "DELETE FROM shares WHERE expires_at>0 AND expires_at<?",
                (int(now),))
            # 转发流量是无个人标识的按天聚合，保留 1 年供带宽/成本复盘
            conn.execute("DELETE FROM media_traffic WHERE day<?",
                         (_today() - 366,))
            conn.execute(
                "DELETE FROM api_keys WHERE enabled=0 AND deleted_at IS NOT NULL "
                "AND deleted_at<? AND COALESCE(reserved_cents,0)=0 "
                "AND NOT EXISTS(SELECT 1 FROM jobs WHERE jobs.key=api_keys.key)",
                (cutoff,))
            conn.commit()
            _last_data_cleanup = now
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _sweeper():
    while True:
        time.sleep(300)
        try:
            _sweep_memory()
            _refund_stale_quota_reservations()
            _cleanup_retained_data()
            _flush_media_traffic()
            _atc_cleanup()
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
    "365yg.com",  # 抖音解析可能返回的字节系视频 CDN（如 v26-default）
)

CACHE_TTL = 1800      # 解析结果缓存 30 分钟
PARSE_SNAPSHOT_TTL = 86400  # 成功元数据暂存 24 小时；分享页另按自身有效期保存

# 代理测试目标
TEST_URL_IP = "https://api.ipify.org?format=json"     # 出口 IP
TEST_URL_DOUYIN = "https://www.iesdouyin.com/"        # 抖音可达性

SUPPORTED_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4", "socks4a")

DEFAULT_SETTINGS = {
    "force_proxy": False,         # 默认代理优先，无可用代理时允许服务器直连
    "proxy_policy_version": 1,    # 旧版本的强制代理默认值只迁移一次
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
                if not d.get("settings", {}).get("proxy_policy_version"):
                    self.settings["force_proxy"] = False
                    self.settings["proxy_policy_version"] = 1
                    self._save()
            except Exception:
                pass

    def _save(self):
        tmp = STORE_FILE.with_name(STORE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(
            {"proxies": self.proxies, "settings": self.settings},
            ensure_ascii=False, indent=2), "utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, STORE_FILE)

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

    def remove_many(self, ids: set) -> int:
        """批量删除（跳过 managed 托管条目，其生命周期归 mihomo 面板管理）。"""
        with self._lock:
            n = len(self.proxies)
            self.proxies = [p for p in self.proxies
                            if p["id"] not in ids or p.get("managed")]
            self._save()
            return n - len(self.proxies)

    def set_enabled_many(self, ids: set, enabled: bool) -> int:
        """批量启停（跳过 managed 条目），语义与 toggle() 一致。"""
        changed = 0
        with self._lock:
            for p in self.proxies:
                if p["id"] not in ids or p.get("managed"):
                    continue
                p["enabled"] = enabled
                p["auto_off"] = False
                if enabled:
                    p["fail"] = 0
                    p["banned"] = False          # 手动启用即解除封禁标记
                    p["banned_reason"] = None
                changed += 1
            if changed:
                self._save()
        return changed

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

    def sync_managed(self, url: Optional[str], enabled: bool,
                     note: str = "内置机场加速（mihomo）"):
        """维护唯一一条「托管」代理条目（内置 mihomo 落地的本地端口）。
        url=None 时移除该条目；否则 upsert 并按 enabled 启停。与用户手动加的代理隔离。"""
        with self._lock:
            m = next((p for p in self.proxies if p.get("managed")), None)
            if url is None:
                if m:
                    self.proxies = [p for p in self.proxies if not p.get("managed")]
                    self._save()
                return
            if m:
                changed = (m["url"] != url) or (m["enabled"] != enabled)
                m["url"], m["note"] = url, note
                m["enabled"] = enabled
                if changed:
                    m["banned"] = False
                    m["banned_reason"] = None
            else:
                self.proxies.append({
                    "id": "mihomo", "url": url, "enabled": enabled, "managed": True,
                    "auto_off": False, "note": note, "added_at": int(time.time()),
                    "ok": 0, "fail": 0, "last_used": None, "last_ok": None,
                    "latency_ms": None, "exit_ip": None, "douyin_ok": None,
                    "banned": False, "banned_at": None, "banned_reason": None,
                })
            self._save()

    # ---- 选择与打点 ----
    @property
    def force_proxy(self) -> bool:
        return bool(self.settings.get("force_proxy", False))

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


# ---------------------------------------------------------------- 内置 mihomo 内核（机场订阅）
#
# 机场订阅里是 vmess/vless/trojan 等加密协议，本项目出站层（urllib+PySocks）不会解，
# 无法直接进代理池。这里内置一个 mihomo（Clash.Meta 内核）子进程：吃订阅 → 在本地
# 落地成一个 socks5 端口 → 作为一条「托管」代理喂给代理池。多节点测速/切换由 mihomo 负责。
#
# 隔离底线（保证只有本项目能用、绝不影响同服务器其他项目）：
#   · 不设任何系统代理环境变量、不开 TUN/透明代理 → 别的项目联网完全无感
#   · 只绑 127.0.0.1 + allow-lan:false      → 外部机器连不到
#   · 随机高位端口 + 账号密码鉴权 + skip-auth-prefixes 置空（本机也必须带凭证）
#                                           → 同机别的进程即使连到端口也被拒
#   · 子进程只写 data/mihomo/，以本服务同一用户运行，不碰别的项目文件
#
# 默认关闭：只有在后台配置了订阅 URL 后才下载内核并启动。单 worker 运行前提下才安全
# （多 worker 会重复拉起子进程），本项目本就要求单 worker。

MIHOMO_DIR = DATA_DIR / "mihomo"
MIHOMO_BIN = MIHOMO_DIR / "mihomo"
MIHOMO_CFG = MIHOMO_DIR / "config.yaml"
MIHOMO_PID = MIHOMO_DIR / "mihomo.pid"
MIHOMO_LOG = MIHOMO_DIR / "run.log"
MIHOMO_VERSION = os.environ.get("MIHOMO_VERSION", "v1.18.10")
# 国内服务器连不上 github 时，用 MIHOMO_DL_BASE 换镜像（形如 .../releases/download）
MIHOMO_DL_BASE = os.environ.get(
    "MIHOMO_DL_BASE", "https://github.com/MetaCubeX/mihomo/releases/download").rstrip("/")
MIHOMO_OFF = os.environ.get("MIHOMO_OFF", "").lower() in ("1", "true", "yes")


def _mihomo_asset() -> str:
    """按当前平台拼 mihomo release 资源名。"""
    system = platform.system().lower()          # linux / darwin
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        raise RuntimeError(f"不支持的 CPU 架构：{machine}")
    if system not in ("linux", "darwin"):
        raise RuntimeError(f"不支持的系统：{system}")
    # linux-amd64 用 compatible 变体，兼容不支持 x86-64-v3 指令集的老 CPU
    if system == "linux" and arch == "amd64":
        return f"mihomo-linux-amd64-compatible-{MIHOMO_VERSION}.gz"
    return f"mihomo-{system}-{arch}-{MIHOMO_VERSION}.gz"


_MIHOMO_CFG_TMPL = """\
# 由 server.py 自动生成，请勿手改（改后会被覆盖）。含订阅 token，权限 600。
mixed-port: {port}
bind-address: 127.0.0.1
allow-lan: false
authentication:
  - "{user}:{password}"
skip-auth-prefixes: []
tun:
  enable: false
mode: rule
log-level: warning
# 一个永远连不通的占位节点：保证 auto 组永不为空，从而阻止 mihomo 注入
# COMPATIBLE(=DIRECT) 兜底节点。机场无可用节点时流量落到它 → 直接失败（fail-closed），
# 绝不退回服务器真实 IP 直连——这是本项目「绝不暴露服务器 IP」底线在 mihomo 层的落实。
proxies:
  - name: blackhole
    type: socks5
    server: 127.0.0.1
    port: 1
proxy-providers:
  jichang:
    type: http
    url: "{sub_url}"
    path: ./providers/jichang.yaml
    interval: 3600
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204
      interval: 300
proxy-groups:
  # fallback：按顺序选第一个「健康」的节点，机场节点全挂时只剩 blackhole → 失败。
  # 不用 url-test：空 provider 的 url-test 会被注入 COMPATIBLE 直连节点而漏 IP。
  - name: auto
    type: fallback
    use: [jichang]
    proxies: [blackhole]
    url: https://www.gstatic.com/generate_204
    interval: 300
rules:
  - MATCH,auto
"""


class MihomoManager:
    """内置 mihomo 子进程的下载、配置、启停与守护。线程安全。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._state = "stopped"        # stopped/downloading/starting/running/error
        self._last_error = ""
        self._next_try = 0.0           # 失败退避：下次允许启动的时间戳

    # ---- 凭证与本地代理地址（首次生成后持久化）----
    def _creds(self) -> tuple[str, str, str]:
        port = app_setting("mihomo_port")
        user = app_setting("mihomo_user")
        pw = app_setting("mihomo_pass")
        if not (port and user and pw):
            port = str(random.randint(20000, 60000))
            user = "dy" + secrets.token_hex(3)
            pw = secrets.token_hex(8)
            set_app_setting("mihomo_port", port)
            set_app_setting("mihomo_user", user)
            set_app_setting("mihomo_pass", pw)
        return port, user, pw

    def proxy_url(self) -> str:
        port, user, pw = self._creds()
        return f"socks5://{user}:{pw}@127.0.0.1:{port}"

    def sub_url(self) -> str:
        return (app_setting("mihomo_sub_url") or "").strip()

    # ---- 二进制 ----
    def ensure_binary(self):
        if MIHOMO_BIN.exists() and os.access(MIHOMO_BIN, os.X_OK):
            return
        MIHOMO_DIR.mkdir(parents=True, exist_ok=True)
        asset = _mihomo_asset()
        url = f"{MIHOMO_DL_BASE}/{MIHOMO_VERSION}/{asset}"
        gz = MIHOMO_DIR / asset
        self._state = "downloading"
        req = urlreq.Request(url, headers={"User-Agent": "douyin-dl"})
        with urlreq.urlopen(req, timeout=180) as r, open(gz, "wb") as f:
            shutil.copyfileobj(r, f)
        with gzip.open(gz, "rb") as fi, open(MIHOMO_BIN, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        os.chmod(MIHOMO_BIN, 0o755)
        try:
            gz.unlink()
        except OSError:
            pass

    # ---- 配置 ----
    def write_config(self):
        MIHOMO_DIR.mkdir(parents=True, exist_ok=True)
        port, user, pw = self._creds()
        cfg = _MIHOMO_CFG_TMPL.format(port=port, user=user, password=pw,
                                      sub_url=self.sub_url())
        MIHOMO_CFG.write_text(cfg, "utf-8")
        try:
            os.chmod(MIHOMO_CFG, 0o600)          # 含订阅 token
        except OSError:
            pass

    # ---- 进程 ----
    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _kill_stale(self):
        """清理上一次残留的 mihomo（防孤儿/端口占用）。"""
        if not MIHOMO_PID.exists():
            return
        try:
            pid = int(MIHOMO_PID.read_text().strip())
        except (ValueError, OSError):
            MIHOMO_PID.unlink(missing_ok=True)
            return
        if self._proc and self._proc.pid == pid:
            return
        if self._alive(pid):
            try:
                os.kill(pid, 15)
                for _ in range(20):
                    if not self._alive(pid):
                        break
                    time.sleep(0.1)
                if self._alive(pid):
                    os.kill(pid, 9)
            except OSError:
                pass
        MIHOMO_PID.unlink(missing_ok=True)

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _start_locked(self):
        if not self.sub_url() or self.running():
            return
        self.ensure_binary()
        self.write_config()
        self._kill_stale()
        self._state = "starting"
        logf = open(MIHOMO_LOG, "ab", buffering=0)
        # 用绝对路径：DATA_DIR 可能是相对路径，exec 不受进程 cwd 影响
        self._proc = subprocess.Popen(
            [str(MIHOMO_BIN.resolve()), "-d", str(MIHOMO_DIR.resolve())],
            stdout=logf, stderr=logf,
        )
        MIHOMO_PID.write_text(str(self._proc.pid))
        time.sleep(1.5)
        if self.running():
            self._state = "running"
            self._last_error = ""
        else:
            self._state = "error"
            self._last_error = self._log_tail() or f"mihomo 启动即退出（码 {self._proc.returncode}）"
            self._next_try = time.time() + 30

    def _log_tail(self, n: int = 500) -> str:
        try:
            data = MIHOMO_LOG.read_bytes()[-n:]
            return data.decode("utf-8", "replace").strip()
        except OSError:
            return ""

    def stop(self):
        with self._lock:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    try:
                        self._proc.kill()
                    except OSError:
                        pass
            self._proc = None
            self._kill_stale()
            self._state = "stopped"

    def reload(self):
        """订阅或凭证变更后：有订阅则重建配置并重启，无订阅则停掉。"""
        with self._lock:
            if not self.sub_url():
                self._next_try = 0
        if not self.sub_url():
            self.stop()
            proxy_mgr.sync_managed(None, False)
            return
        self.stop()
        with self._lock:
            self._next_try = 0
            try:
                self._start_locked()
            except Exception as e:            # noqa: BLE001 下载/启动失败不能崩主服务
                self._state = "error"
                self._last_error = str(e)
                self._next_try = time.time() + 30
        proxy_mgr.sync_managed(self.proxy_url(), self.running())

    def _tick(self):
        sub = self.sub_url()
        if not sub:
            if self.running():
                self.stop()
            proxy_mgr.sync_managed(None, False)
            return
        with self._lock:
            if not self.running() and time.time() >= self._next_try:
                try:
                    self._start_locked()
                except Exception as e:        # noqa: BLE001
                    self._state = "error"
                    self._last_error = str(e)
                    self._next_try = time.time() + 30
        proxy_mgr.sync_managed(self.proxy_url(), self.running())

    def supervise(self):
        while True:
            time.sleep(5)
            if MIHOMO_OFF:
                continue
            try:
                self._tick()
            except Exception as e:            # noqa: BLE001 守护线程绝不能挂
                self._last_error = str(e)

    def status(self) -> dict:
        sub = self.sub_url()
        managed = next((p for p in proxy_mgr.proxies if p.get("managed")), None)
        return {
            "enabled": bool(sub),
            "sub_url_masked": _mask_secret(sub) if sub else "",
            "state": self._state,
            "running": self.running(),
            "binary_ready": MIHOMO_BIN.exists() and os.access(MIHOMO_BIN, os.X_OK),
            "version": MIHOMO_VERSION,
            "last_error": self._last_error[-300:],
            "exit_ip": managed.get("exit_ip") if managed else None,
            "douyin_ok": managed.get("douyin_ok") if managed else None,
            "latency_ms": managed.get("latency_ms") if managed else None,
        }


def _mask_secret(s: str) -> str:
    """打码：保留头尾，中间星号（用于订阅 URL/token 回显）。"""
    if len(s) <= 12:
        return s[:2] + "***" + s[-2:] if len(s) > 4 else "***"
    return s[:18] + "***" + s[-6:]


def _proxy_public_label(proxy: Optional[dict]) -> str:
    """公开错误中只显示协议和节点，不回显 userinfo/密码。"""
    if not proxy:
        return "direct"
    try:
        p = urlparse.urlsplit(proxy.get("url", ""))
        host = p.hostname or "unknown"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{p.scheme or 'proxy'}://{host}{':' + str(p.port) if p.port else ''}"
    except Exception:
        return "proxy"


def _redact_proxy_error(value) -> str:
    text = str(value or "")
    # 异常库有时会把完整代理 URL 带回来，先替换已知配置，再兜底清理任意 URL userinfo。
    for proxy in getattr(proxy_mgr, "proxies", []):
        raw = proxy.get("url", "")
        if raw:
            text = text.replace(raw, _proxy_public_label(proxy))
    return re.sub(r"([a-zA-Z][\w+.-]*://)[^/@\s]+@", r"\1***@", text)[:180]


mihomo_mgr = MihomoManager()


# ---------------------------------------------------------------- 应用设置 + 开放 API 计费

NEW_KEY_BALANCE = _clamped_env_int(
    "NEW_KEY_BALANCE", 100, 0, 1000000)   # 每个用户仅首次密钥赠送（分）
USER_ACTIVE_KEY_LIMIT = 10


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
    now = int(time.time())
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            opening_balance = NEW_KEY_BALANCE
            if user_id is not None:
                user = conn.execute(
                    "SELECT api_trial_granted_cents FROM users WHERE id=? AND disabled=0",
                    (user_id,)).fetchone()
                if not user:
                    raise ApiError(401, "用户不存在或已停用")
                active = conn.execute(
                    "SELECT COUNT(*) FROM api_keys WHERE user_id=? AND enabled=1 "
                    "AND deleted_at IS NULL", (user_id,)).fetchone()[0]
                if int(active or 0) >= USER_ACTIVE_KEY_LIMIT:
                    raise ApiError(400, f"每个账号最多 {USER_ACTIVE_KEY_LIMIT} 个密钥")
                already_granted = int(user[0] or 0)
                opening_balance = NEW_KEY_BALANCE if already_granted <= 0 else 0
                if opening_balance:
                    conn.execute(
                        "UPDATE users SET api_trial_granted_cents="
                        "COALESCE(api_trial_granted_cents,0)+? WHERE id=?",
                        (opening_balance, user_id))
            conn.execute(
                "INSERT INTO api_keys("
                "key,user_id,name,created,enabled,balance_cents,spent_cents,calls,reserved_cents"
                ") VALUES(?,?,?,?,1,?,0,0,0)",
                (key, user_id, (name or "未命名")[:60], now,
                 opening_balance))
            if opening_balance:
                conn.execute(
                    "INSERT INTO api_ledger(ts,key,event,balance_delta,reason) "
                    "VALUES(?,?,?,?,?)",
                    (now, key, "opening", opening_balance, "new_key_balance"))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return get_api_key(key)


def get_api_key(key: str) -> Optional[dict]:
    row = db_exec("SELECT * FROM api_keys WHERE key=?", (key,), "one")
    return dict(row) if row else None


def list_api_keys(user_id: Optional[int] = None) -> list:
    if user_id is None:
        rows = db_exec("SELECT * FROM api_keys ORDER BY created DESC", (), "all")
    else:
        rows = db_exec(
            "SELECT * FROM api_keys WHERE user_id=? AND enabled=1 "
            "AND deleted_at IS NULL ORDER BY created DESC",
            (user_id,), "all")
    return [dict(r) for r in rows]


def revoke_api_key(key: str, user_id: Optional[int] = None) -> bool:
    k = get_api_key(key)
    if not k or (user_id is not None and k["user_id"] != user_id):
        return False
    db_exec("UPDATE api_keys SET enabled=0,deleted_at=? WHERE key=?",
            (int(time.time()), key))
    return True


def recharge_key(key: str, cents: int) -> bool:
    if not get_api_key(key):
        return False
    cents = int(cents)
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            n = conn.execute(
                "UPDATE api_keys SET balance_cents=balance_cents+? WHERE key=?",
                (cents, key)).rowcount
            if not n:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO api_ledger(ts,key,event,balance_delta,reason) "
                "VALUES(?,?,?,?,?)",
                (int(time.time()), key, "recharge", cents, "admin_recharge"))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return True


def api_key_check(key: str):
    """校验 key（不扣费）。返回 (rec, error)。"""
    if not key:
        return None, "缺少 API Key（请通过 X-API-Key 请求头传入）"
    rec = get_api_key(key)
    if not rec or not rec["enabled"] or rec.get("deleted_at"):
        return None, "无效或已禁用的 API Key"
    return rec, None


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
             timeout: int = 30, retry_http_statuses: tuple = (),
             ban_on_auth_error: bool = True,
             proxy_override: Optional[dict] = None):
    """出站请求核心：代理优先，无代理或代理重试失败时默认服务器直连。

    管理员仍可显式开启严格代理模式；绑定签名出口的请求始终保留指定代理。
    返回 (response, proxy_used_or_None)。
    """
    hdrs = {"User-Agent": pick_ua()}
    if headers:
        hdrs.update(headers)

    # A signed Douyin CDN URL can be bound to the IP that created it.  Internal
    # callers may pin this request to the browser's proxy; request parameters
    # never reach this argument.
    cands = ([proxy_override] if proxy_override else proxy_mgr.candidates())
    if not cands:                                   # 无可用代理
        if proxy_override:
            raise ApiError(502, "指定代理暂时不可用，请稍后重试")
        if proxy_mgr.force_proxy:
            raise ApiError(503, "没有可用代理，且已开启「禁止服务器直连」——为避免暴露服务器 IP，"
                                "不会直连抖音。请在管理后台添加并启用代理。")
        r = _raw_open(url, follow, hdrs, timeout, None)
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
            # NoRedirect 将短链的 30x 作为 HTTPError 返回；调用方需要读取
            # Location/正文来解析官方短链，而不是把一次正常跳转误报成失败。
            # 这条分支仅在 follow=False 时生效，普通出站请求仍按原语义抛错。
            if (not follow) and e.code in (301, 302, 303, 307, 308):
                proxy_mgr.mark_ok(p, int((time.time() - t0) * 1000))
                return e, p
            if e.code in (403, 401):                # 抖音封禁该代理 IP → 落库+禁用+换代理
                if ban_on_auth_error:
                    proxy_mgr.mark_banned(p, f"抖音返回 {e.code}，IP 被封禁")
                    errors.append(f"{_proxy_public_label(p)} → 被封禁(HTTP {e.code})")
                else:
                    # 媒体 CDN 的 401/403 也可能只针对当前资源/域名，不能据此永久封禁出口。
                    errors.append(
                        f"{_proxy_public_label(p)} → 媒体请求 HTTP {e.code}")
                try:
                    e.close()
                except Exception:
                    pass
                continue
            if e.code in retry_http_statuses:
                # 代理网关也会生成 5xx；媒体请求应换出口验证，不能把故障代理记为健康。
                # 同时不能累计连接失败：源站 429/5xx 可能只针对当前资源，避免毒死代理池。
                errors.append(f"{_proxy_public_label(p)} → HTTP {e.code}")
                try:
                    e.close()
                except Exception:
                    pass
                continue
            # 其他 4xx/5xx 是源站问题，不怪代理，直接上抛
            proxy_mgr.mark_ok(p, int((time.time() - t0) * 1000))
            raise
        except Exception as e:                      # 连接/超时 → 代理故障，自动转移
            msg = str(e).lower()
            if "403" in msg or "forbidden" in msg or "tunnel connection failed" in msg:
                if ban_on_auth_error:
                    proxy_mgr.mark_banned(p, "代理无法连接抖音（403/被封禁）")
                    errors.append(f"{_proxy_public_label(p)} → 被封禁(403)")
                else:
                    # HTTP CONNECT/tunnel 失败未必代表出口 IP 被抖音永久封禁；
                    # 媒体 CDN 线路只轮换本次请求，不能污染代理池持久健康状态。
                    errors.append(
                        f"{_proxy_public_label(p)} → 媒体连接失败(403)")
            else:
                proxy_mgr.mark_fail(p)
                errors.append(
                    f"{_proxy_public_label(p)} → {type(e).__name__}: "
                    f"{_redact_proxy_error(e)}")

    # Pinned requests must not silently fall back to the server's public IP;
    # the caller will refresh the signed URL and choose another proxy.
    if proxy_override:
        raise ApiError(502, "指定代理暂时不可用，请稍后重试")
    # 所有代理都连不通
    if proxy_mgr.force_proxy:
        raise ApiError(502, "全部代理均不可用，且已禁止服务器直连抖音。"
                            "请在管理后台检查代理状态。")
    r = _raw_open(url, follow, hdrs, timeout, None)
    proxy_mgr.mark_ok(None)
    return r, None


# ---------------------------------------------------------------- 工具函数

app = FastAPI(title="多平台无水印下载器", version=APP_VERSION)


class _RequestBodyLimitMiddleware:
    """在 JSON/Pydantic 解析前限制 API 请求体，覆盖 Content-Length 与 chunked。"""
    def __init__(self, app_, limits: Optional[dict] = None,
                 default_max_bytes: int = 0):
        self.app = app_
        self.limits = dict(limits or {})
        self.default_max_bytes = max(0, int(default_max_bytes or 0))

    def _limit(self, scope) -> int:
        path = str(scope.get("path") or "")
        if path in self.limits:
            return int(self.limits[path])
        if re.fullmatch(r"/api/share/[A-Za-z0-9_-]{1,64}/event", path):
            return 8192
        if path.startswith("/api/"):
            return self.default_max_bytes
        return 0

    async def __call__(self, scope, receive, send):
        max_bytes = self._limit(scope)
        if (scope.get("type") != "http"
                or scope.get("method") not in ("POST", "PUT", "PATCH")
                or max_bytes <= 0):
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        try:
            declared = int(headers.get(b"content-length", b"0") or 0)
        except ValueError:
            declared = 0
        if declared < 0 or declared > max_bytes:
            response = JSONResponse(
                status_code=413,
                content={"error": "payload_too_large: 请求体超过上限"},
                headers={"Cache-Control": "private, no-store", "Pragma": "no-cache",
                         "Connection": "close"})
            await response(scope, receive, send)
            return

        # Starlette 会把 receive() 抛出的普通异常改写成 400，因此必须在进入
        # FastAPI 前最多预读 max_bytes；未超限时再把原消息重放给请求解析器。
        buffered = []
        received = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message.get("type") != "http.request":
                break
            received += len(message.get("body") or b"")
            if received > max_bytes:
                response = JSONResponse(
                    status_code=413,
                    content={"error": "payload_too_large: 请求体超过上限"},
                    headers={"Cache-Control": "private, no-store",
                             "Pragma": "no-cache", "Connection": "close"})
                await response(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        index = 0

        async def replay_receive():
            nonlocal index
            if index < len(buffered):
                message = buffered[index]
                index += 1
                return message
            return await receive()

        await self.app(scope, replay_receive, send)


class _AsyncShareBodyLimitMiddleware(_RequestBodyLimitMiddleware):
    """旧测试/外部引用的兼容包装：只限制 ``POST /api/shares``。"""
    def __init__(self, app_, max_bytes: int):
        super().__init__(app_, {"/api/shares": max_bytes})


app.add_middleware(
    _RequestBodyLimitMiddleware,
    limits={
        "/api/shares": ASYNC_SHARE_BODY_MAX,
        "/api/share": PARSE_TEXT_MAX + 4096,
        "/api/parse": PARSE_TEXT_MAX + 2048,
        "/api/parse/batch": BATCH_TEXT_MAX + 2048,
        "/api/v1/jobs": API_JOB_BODY_MAX,
        "/api/export/xlsx": EXPORT_BODY_MAX,
        "/api/report": 8192,
    },
    default_max_bytes=1048576)


@app.middleware("http")
async def _private_api_responses(request: Request, call_next):
    """API 响应可能含签名地址、账号或密钥，禁止浏览器与共享代理持久化。"""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Pragma"] = "no-cache"
    return response


def _public_error(message, fallback: str = "服务暂时不可用，请稍后重试") -> str:
    """公开错误只保留可操作说明，拦截服务商、凭据和上游连接细节。"""
    text = str(message or "").strip()
    if not text or re.search(
            r"any[\W_]*(?:to|two|2)[\W_]*copy|\batc\b|https?://|"
            r"api[_ -]?(?:key|secret)\s*[:=]|Traceback|urlopen error",
            text, re.I):
        return fallback
    return text[:300]


class ApiError(Exception):
    def __init__(self, status: int, message: str, headers: Optional[dict] = None):
        self.status, self.message = status, _public_error(message)
        self.headers = headers or {}


@app.exception_handler(ApiError)
async def _api_error(request: Request, exc: ApiError):
    fallback = {400: "Invalid request.", 401: "Please sign in and try again.",
                403: "This request could not be verified. Refresh the page and try again.",
                404: "The requested content is unavailable.", 422: "Please check the supplied information.",
                429: "Too many requests. Please try again later.",
                504: "The request timed out. Please try again."}.get(exc.status, "The service is unavailable. Please try again later.")
    message = _ui_text(exc.message, _pick_lang(request), fallback)
    return JSONResponse(status_code=exc.status, content={"error": message},
                        headers=exc.headers)


def _host_allowed(url: str) -> bool:
    if not isinstance(url, str) or len(url) > 4096:
        return False
    try:
        p = urlparse.urlsplit(url)
        if p.scheme not in ("http", "https") or p.username or p.password:
            return False
        if p.port not in (None, 80, 443):
            return False
        host = (p.hostname or "").lower().rstrip(".")
    except (ValueError, TypeError):
        return False
    return any(host == s or host.endswith("." + s) for s in ALLOWED_HOST_SUFFIXES)


def _primary_media_allowed(url: str) -> bool:
    """主解析媒体只允许已知 CDN；TikTok 不扩大抖音官方媒体的白名单。"""
    if not _atc_public_url(url):
        return False
    if _host_allowed(url):
        return True
    host = (urlparse.urlsplit(url).hostname or "").lower().rstrip(".")
    suffixes = ("tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com", "tiktokv.com")
    return (any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)
            or bool(re.fullmatch(r"v\d+[\w.-]*\.tiktok\.com", host)))


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


def _media_signature(kind: str, resource: str, exp: int) -> str:
    payload = f"media:v1\n{kind}\n{resource}\n{int(exp)}".encode()
    return hmac.new(APP_SECRET, payload, hashlib.sha256).hexdigest()


def _media_token(kind: str, resource: str, ttl: int = MEDIA_TOKEN_TTL) -> tuple:
    exp = int(time.time()) + max(60, int(ttl))
    return exp, _media_signature(kind, resource, exp)


def _require_media_token(kind: str, resource: str, exp: int, sig: str) -> None:
    now = int(time.time())
    if (not re.fullmatch(r"[0-9a-f]{64}", sig or "")
            or exp < now or exp > now + MEDIA_TOKEN_TTL + 300):
        raise ApiError(403, "媒体链接已过期，请重新解析或刷新页面")
    expected = _media_signature(kind, resource, exp)
    if not hmac.compare_digest(sig, expected):
        raise ApiError(403, "媒体链接签名无效")


def _video_proxy_url(vid: str) -> str:
    exp, sig = _media_token("video", vid)
    return f"/api/video/{vid}?" + urlparse.urlencode({"exp": exp, "sig": sig})


def _douyin_video_proxy_url(item_id: str) -> str:
    """抖音官方签名媒体的同源播放地址（按作品 ID 惰性刷新）。"""
    exp, sig = _media_token("douyin_direct", item_id)
    return f"/api/douyin/video/{item_id}?" + urlparse.urlencode({
        "exp": exp, "sig": sig})


def _douyin_video_download_url(item_id: str,
                               filename: str = "video.mp4") -> str:
    exp, sig = _media_token("douyin_direct", item_id)
    return f"/api/douyin/video/{item_id}?" + urlparse.urlencode({
        "exp": exp, "sig": sig, "dl": "1",
        "name": filename or "video.mp4",
    })


def _douyin_image_proxy_url(item_id: str, index: int,
                            filename: str = "image.jpeg",
                            download: bool = False) -> str:
    resource = f"{item_id}:{int(index)}"
    exp, sig = _media_token("douyin_image", resource)
    params = {"exp": exp, "sig": sig}
    if download:
        params.update({"dl": "1", "name": filename or "image.jpeg"})
    return f"/api/douyin/image/{item_id}/{int(index)}?" + urlparse.urlencode(params)


def _atc_video_proxy_url(item_id: str) -> str:
    """ATC 解析结果的同源播放地址；URL 只能从服务端缓存取得。"""
    exp, sig = _media_token("atc_video", item_id)
    return f"/api/media/video/{item_id}?" + urlparse.urlencode({
        "exp": exp, "sig": sig})


def _atc_video_download_url(item_id: str,
                            filename: str = "video.mp4") -> str:
    exp, sig = _media_token("atc_video", item_id)
    return f"/api/media/video/{item_id}?" + urlparse.urlencode({
        "exp": exp, "sig": sig, "dl": "1",
        "name": filename or "video.mp4",
    })


def _video_download_refresh_url(item_id: str) -> str:
    """只授权刷新指定作品的下载地址，不能提交任意来源或媒体 URL。"""
    exp, sig = _media_token("download_link", item_id)
    return f"/api/media/video/{item_id}/download-link?" + urlparse.urlencode({
        "exp": exp, "sig": sig})


def _stream(resp, chunk=256 * 1024, on_close=None):
    try:
        while True:
            block = resp.read(chunk)
            if not block:
                break
            yield block
    finally:
        if on_close:
            on_close()
        else:
            resp.close()


def _parse_content_range(value: str):
    """解析单段 Content-Range，返回 (start, end, total|None)。"""
    match = re.fullmatch(
        r"bytes\s+(\d+)-(\d+)/(\d+|\*)", (value or "").strip(),
        flags=re.IGNORECASE)
    if not match:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    total = None if match.group(3) == "*" else int(match.group(3))
    if end < start or (total is not None and (total <= 0 or end >= total)):
        return None
    return start, end, total


def _video_response_shape(resp, requested_range: str):
    """校验上游 Range 语义，返回流的绝对边界与期望字节数。

    返回 (status, start, end|None, total|None, expected|None)。缺少长度时
    expected 为 None，此时仍可流式传输，但无法判定提前 EOF。
    """
    status = resp.status if hasattr(resp, "status") else resp.getcode()
    content_range = _parse_content_range(
        resp.headers.get("Content-Range") or "")
    raw_length = (resp.headers.get("Content-Length") or "").strip()
    if raw_length and not re.fullmatch(r"\d+", raw_length):
        raise ValueError("invalid video Content-Length")
    content_length = int(raw_length) if raw_length else None

    if requested_range:
        if status != 206 or content_range is None:
            raise ValueError("video upstream did not honor Range")
        start, end, total = content_range
        expected = end - start + 1
        if content_length is not None and content_length != expected:
            raise ValueError("video range length mismatch")

        match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested_range.strip())
        if not match:
            raise ValueError("invalid requested video Range")
        req_start, req_end = match.groups()
        if req_start:
            if start != int(req_start):
                raise ValueError("video range start mismatch")
            if req_end:
                wanted_end = int(req_end)
                if total is not None:
                    wanted_end = min(wanted_end, total - 1)
                if end != wanted_end:
                    raise ValueError("video range end mismatch")
            elif total is not None and end != total - 1:
                raise ValueError("open video range ended early")
        else:
            # 后缀 Range 必须知道资源总长，才能证明返回的是最后 N 字节。
            suffix = int(req_end)
            if total is None or end != total - 1:
                raise ValueError("invalid suffix video range")
            if start != max(0, total - suffix):
                raise ValueError("video suffix range mismatch")
        return status, start, end, total, expected

    if status != 200:
        raise ValueError("unexpected partial response for full video")
    if content_range is not None:
        raise ValueError("unexpected Content-Range for full video")
    if content_length is None:
        return status, 0, None, None, None
    return status, 0, content_length - 1, content_length, content_length


class _ResumeBudgetExceeded(Exception):
    pass


class _ResumableVideoStream:
    """在上游长连接提前 EOF/读取异常后，从精确字节偏移续传。"""
    _MAX_CONSECUTIVE_RESUME_FAILURES = MEDIA_RESUME_MAX_FAILURES
    _MAX_TOTAL_RESUME_ATTEMPTS = MEDIA_RESUME_MAX_ATTEMPTS
    _MAX_RESUME_SECONDS = MEDIA_RESUME_MAX_SECONDS

    def __init__(self, vid: str, initial, request_headers: dict,
                 start: int, end: Optional[int], total: Optional[int],
                 expected: Optional[int], chunk: int = 256 * 1024,
                 opener=None):
        self.vid = vid
        self.opener = opener
        self.request_headers = dict(request_headers)
        self.start = start
        self.end = end
        self.total = total
        self.expected = expected
        self.chunk = chunk
        self.sent = 0
        self.current = initial
        self.closed = False
        self._lock = threading.Lock()
        self._on_close = None
        self._resume_attempts = 0
        self._resume_started = None

    def set_on_close(self, callback) -> None:
        self._on_close = callback

    def _take_current(self):
        with self._lock:
            return None if self.closed else self.current

    def _replace_current(self, replacement) -> bool:
        with self._lock:
            if self.closed:
                accepted = False
            else:
                self.current = replacement
                accepted = True
        if not accepted:
            _close_upstream(replacement)
        return accepted

    def _resume(self):
        with self._lock:
            if self.closed:
                return None
            now = time.monotonic()
            if self._resume_started is None:
                self._resume_started = now
            if (self._resume_attempts >= self._MAX_TOTAL_RESUME_ATTEMPTS
                    or now - self._resume_started
                    >= self._MAX_RESUME_SECONDS):
                raise _ResumeBudgetExceeded()
            self._resume_attempts += 1
        offset = self.start + self.sent
        if self.end is not None and offset > self.end:
            return None
        headers = dict(self.request_headers)
        headers["Range"] = (
            f"bytes={offset}-{self.end}"
            if self.end is not None else f"bytes={offset}-")

        def validate(candidate):
            status, start, end, total, expected = _video_response_shape(
                candidate, headers["Range"])
            if status != 206 or start != offset:
                raise ValueError("resumed video range start mismatch")
            if self.end is not None and end is not None and end > self.end:
                raise ValueError("resumed video range exceeded response")
            if self.total is not None and total != self.total:
                raise ValueError("video size changed while resuming")
            if expected is not None and self.expected is not None:
                remaining = self.expected - self.sent
                if expected > remaining:
                    raise ValueError("resumed video range is too long")

        replacement = (self.opener(headers, validate) if self.opener
                       else _open_video_upstream(
                           self.vid, headers, validator=validate))
        if (time.monotonic() - self._resume_started
                >= self._MAX_RESUME_SECONDS):
            _close_upstream(replacement)
            raise _ResumeBudgetExceeded()
        if not self._replace_current(replacement):
            return None
        return replacement

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            current, self.current = self.current, None
        if current is not None:
            _close_upstream(current)

    def __iter__(self):
        consecutive_failures = 0
        try:
            while self.expected is None or self.sent < self.expected:
                current = self._take_current()
                if current is None:
                    break
                remaining = (
                    self.chunk if self.expected is None
                    else min(self.chunk, self.expected - self.sent))
                try:
                    block = current.read(remaining)
                except Exception as exc:
                    # http.client.IncompleteRead 等异常会携带已收到的 partial；
                    # 必须先转发并推进 offset，否则重开 Range 会重复这些字节。
                    partial = getattr(exc, "partial", b"")
                    block = bytes(partial) if isinstance(
                        partial, (bytes, bytearray, memoryview)) else b""
                    if self.expected is not None:
                        block = block[:self.expected - self.sent]
                    if block:
                        self.sent += len(block)
                        consecutive_failures = 0
                        yield block
                    if self.expected is None or self.sent >= self.expected:
                        break
                    _close_upstream(current)
                    consecutive_failures += 1
                else:
                    if block:
                        if self.expected is not None:
                            block = block[:self.expected - self.sent]
                        self.sent += len(block)
                        consecutive_failures = 0
                        yield block
                        continue
                    if self.expected is None or self.sent >= self.expected:
                        break
                    # 已声明长度却提前 EOF：关闭断流连接，从 sent 对应的
                    # 绝对偏移重开单段 Range，避免重复或缺失字节。
                    _close_upstream(current)
                    consecutive_failures += 1

                if (consecutive_failures
                        > self._MAX_CONSECUTIVE_RESUME_FAILURES):
                    raise OSError(
                        "video upstream repeatedly ended early") from None
                while True:
                    try:
                        resumed = self._resume()
                        break
                    except _ResumeBudgetExceeded:
                        raise OSError(
                            "video upstream resume budget exhausted") from None
                    except Exception:
                        consecutive_failures += 1
                        if (consecutive_failures
                                > self._MAX_CONSECUTIVE_RESUME_FAILURES):
                            raise OSError(
                                "video upstream resume failed") from None
                if resumed is None:
                    break
        finally:
            callback = self._on_close
            if callback:
                callback()
            else:
                self.close()


_media_limit_lock = threading.Lock()
_media_hits: dict = {}
_media_active: dict = {}


class _MediaLease:
    def __init__(self, key: str):
        self.key = key
        self.released = False


def _media_lease(request: Request,
                 requests_per_min: int = MEDIA_REQUESTS_PER_MIN) -> _MediaLease:
    """为一次媒体流申请 IP 级请求/并发租约；仅在流关闭时释放并发计数。"""
    key = _client_ip(request)
    now = time.time()
    with _media_limit_lock:
        hits = [t for t in _media_hits.get(key, []) if now - t < 60]
        if len(hits) >= max(1, int(requests_per_min)):
            raise ApiError(429, "媒体请求过于频繁，请稍后再试",
                           {"Retry-After": "60"})
        if _media_active.get(key, 0) >= MEDIA_MAX_CONCURRENT:
            raise ApiError(429, "同时播放或下载的媒体过多，请稍后再试",
                           {"Retry-After": "2"})
        hits.append(now)
        _media_hits[key] = hits
        _media_active[key] = _media_active.get(key, 0) + 1
    return _MediaLease(key)


def _media_release(lease: _MediaLease) -> None:
    with _media_limit_lock:
        if lease.released:
            return
        lease.released = True
        active = _media_active.get(lease.key, 0) - 1
        if active > 0:
            _media_active[lease.key] = active
        else:
            _media_active.pop(lease.key, None)


class _MediaStreamingResponse(StreamingResponse):
    """无论 ASGI 在响应头、首块或流中何处中断，都释放媒体并发租约。"""
    def __init__(self, *args, finalize, **kwargs):
        self._media_finalize = finalize
        super().__init__(*args, **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._media_finalize()


def _media_finalizer(upstream, lease: _MediaLease, on_close=None):
    """返回线程安全、幂等的上游关闭 + 并发租约释放函数。

    on_close 在首次 finalize 时执行一次（用于流量计量等收尾统计），
    必须是廉价的内存操作——finalize 可能跑在事件循环线程，阻塞不得。"""
    lock = threading.Lock()
    closed = False

    def finalize():
        nonlocal closed
        with lock:
            if closed:
                return
            closed = True
        try:
            upstream.close()
        except Exception:
            pass
        finally:
            _media_release(lease)
            if on_close:
                try:
                    on_close()
                except Exception:
                    pass

    return finalize


def _sweep_media_limits() -> None:
    now = time.time()
    with _media_limit_lock:
        for key, hits in list(_media_hits.items()):
            fresh = [t for t in hits if now - t < 60]
            if fresh:
                _media_hits[key] = fresh
            else:
                _media_hits.pop(key, None)


# 媒体转发流量统计（后台「转发流量统计」）：统计经 /api/video、
# /api/douyin/video 与 /api/atc/video 同源转发的字节；浏览器直连媒体的流量
# 不经过本服务器。
# 内存累加 + 定期落库（media_traffic 按天/用途聚合，无任何个人标识）——
# 不能在流结束回调里直接写 SQLite：finalize 可能跑在事件循环线程。
_traffic_lock = threading.Lock()
_traffic_pending: dict = {}          # (day, scope) -> [requests, bytes]


def _traffic_add(scope: str, nbytes: int) -> None:
    key = (_today(), scope)
    with _traffic_lock:
        cur = _traffic_pending.setdefault(key, [0, 0])
        cur[0] += 1
        cur[1] += max(0, int(nbytes or 0))


def _flush_media_traffic() -> None:
    global _traffic_pending
    with _traffic_lock:
        if not _traffic_pending:
            return
        pending, _traffic_pending = _traffic_pending, {}
    try:
        with _db_lock:
            conn = _db()
            try:
                conn.execute("BEGIN IMMEDIATE")
                for (day, scope), (n, b) in pending.items():
                    conn.execute(
                        "INSERT INTO media_traffic(day,scope,requests,bytes) "
                        "VALUES(?,?,?,?) ON CONFLICT(day,scope) DO UPDATE SET "
                        "requests=requests+excluded.requests, "
                        "bytes=bytes+excluded.bytes",
                        (day, scope, n, b))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
    except Exception:
        # 落库失败把计数放回内存，等下一轮 sweeper 重试，不丢数据
        with _traffic_lock:
            for key, (n, b) in pending.items():
                cur = _traffic_pending.setdefault(key, [0, 0])
                cur[0] += n
                cur[1] += b


def _valid_single_range(value: str) -> bool:
    """仅接受单段 bytes Range，拒绝多段请求放大与异常长请求头。"""
    if not value:
        return True
    value = value.strip()
    if len(value) > 100:
        return False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if not match or not any(match.groups()):
        return False
    start, end = match.groups()
    return not ((start and end and int(start) > int(end))
                or (not start and int(end) <= 0))


def _content_disposition(name: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|]+', "_", name)[:80]
    return f"attachment; filename*=UTF-8''{urlparse.quote(safe)}"


# ---------------------------------------------------------------- 核心解析

_cache: dict = {}
_cache_lock = threading.RLock()
_author_cache: dict = {}          # item_id -> (ts, 作者结构化详情)
_metadata_retry_lock = threading.RLock()
_metadata_retries: dict = {}      # item_id -> (下次重试时间, 是否进行中)
_metadata_last_failure: dict = {}
METADATA_RETRY_SECONDS = 60
CDN_HEADERS = {
    "Referer": "https://www.douyin.com/",
    "Accept-Encoding": "identity",
}


def _cache_get(key: str):
    with _cache_lock:
        return _cache.get(key)


def _cache_put(key: str, value: dict, now: Optional[float] = None) -> None:
    now = time.time() if now is None else float(now)
    with _cache_lock:
        _cache[str(key)] = (now, value)
        if len(_cache) <= 500:
            return
        for cache_key, (ts, _) in list(_cache.items()):
            if now - ts > CACHE_TTL:
                _cache.pop(cache_key, None)
        if len(_cache) > 500:
            oldest = sorted(_cache.items(), key=lambda item: item[1][0])
            for cache_key, _ in oldest[:len(_cache) - 500]:
                _cache.pop(cache_key, None)


def _play_api(vid: str) -> str:
    """无水印播放接口地址。交给用户浏览器直接请求：

    浏览器 GET 该地址 → 302 → 跟随到 CDN 直链（按浏览器自身 IP/地区解析）→ 播放。
    普通浏览器优先用它，让视频字节不经过本服务器（省带宽、不暴露服务器 IP），
    且 CDN 直链与浏览器同 IP，避免"服务器/代理 IP 解析的直链换个 IP 打不开"。
    实测该接口对桌面 UA / 无 UA 均返回 200，浏览器可直连。
    """
    return f"https://aweme.snssdk.com/aweme/v1/play/?video_id={vid}&ratio=1080p&line=0"


def _play_api_alt(vid: str) -> str:
    """备用播放域名。与 aweme.snssdk.com 互为备份（见 docs/产品文档.md §风险表）。

    微信内不同机型/内核对这两个域名的可达性不一致（部分环境 snssdk 被拦、
    iesdouyin 可播，反之亦然）。所有环境（含微信）都在服务器代理前尝试
    此线路以节省带宽，同源代理只做兜底。
    """
    return f"https://www.iesdouyin.com/aweme/v1/play/?video_id={vid}&ratio=1080p&line=0"


def _video_download_url(vid: str, filename: str = "video.mp4") -> str:
    """同源下载地址：经 Range 预检后由浏览器原生流式保存。"""
    exp, sig = _media_token("video", vid)
    return f"/api/video/{vid}?" + urlparse.urlencode({
        "exp": exp,
        "sig": sig,
        "dl": "1",
        "name": filename or "video.mp4",
    })


def _card_cover(cover: str) -> str:
    """把抖音封面直链转成"适合当社交卡片图"的形式：**去签名 + 转 JPEG**。

    抖音给的封面是 `https://p26-sign.douyinpic.com/...webp?x-expires=...&x-signature=...`，
    当 og:image 有两个硬伤：① `.webp` 微信卡片缩略图支持不稳定；② 签名 ~14 天过期，
    过期后存量分享页全变无图卡片。

    实测（见 README 更新日志 v1.7.0）：把主机的 `-sign` 去掉、扩展名换成 `.jpeg`，
    抖音会返回 **无签名、不过期的 JPEG**（同一张图，体积略大）。签名覆盖了路径，
    所以只换扩展名不去 -sign 主机会 403，两步必须一起做。

    只认白名单内的抖音图床，转换失败就原样返回（宁可用 webp，也不要吐出个坏链接）。
    """
    if not cover or not cover.startswith("https://"):
        return cover
    try:
        if not _host_allowed(cover):
            return cover
        p = urlparse.urlsplit(cover)
        host, path = p.netloc, p.path
        if "-sign." not in host or not path.lower().endswith((".webp", ".jpeg", ".jpg")):
            return cover
        host = host.replace("-sign.", ".", 1)
        path = re.sub(r"\.webp$", ".jpeg", path, flags=re.I)
        return f"https://{host}{path}"        # 丢掉 query（签名参数），无签名主机不需要
    except Exception:
        return cover


ATC_PLATFORM_HOSTS = {
    "douyin": ("douyin.com", "iesdouyin.com"),
    "xiaohongshu": ("xiaohongshu.com", "xhslink.com"),
    "kuaishou": ("kuaishou.com", "kuaishou.cn", "gifshow.com"),
    "bilibili": ("bilibili.com", "b23.tv"),
    "pinduoduo": ("pinduoduo.com", "yangkeduo.com"),
    "x": ("x.com", "twitter.com"),
    "toutiao": ("toutiao.com",),
    "shipinhao": ("channels.weixin.qq.com", "weixin.qq.com"),
    "weibo": ("weibo.com", "weibo.cn", "t.cn"),
    "tiktok": ("tiktok.com",),
    "youtube": ("youtube.com", "youtu.be"),
}


# ---------------------------------------------------------------- 抖音官方解析
#
# 抖音网页目前不再把作品数据塞进分享页 HTML，而是在浏览器运行官方网页
# JavaScript 后请求 ``/aweme/v1/web/aweme/detail/``。该请求包含由抖音
# webmssdk 在浏览器上下文生成的动态签名，服务端用 urllib 直接拼 URL 会得到
# 空响应或风控页。主解析缺失信息或失败时，使用以下官方补充链路：
#
#   短链 → 官方作品页（受控 Chromium，捕获 detail JSON）→ 原生字段归一化
#   → 短时 CDN 地址（仅内存缓存）→ 同源 Range 流（播放/下载兜底）
#
# 不依赖第三方解析 API，也不伪造 ``a_bogus``/``x-secsdk`` 签名。没有浏览器
# 的部署仍会尝试解析官方 SSR/JSON-LD 元数据；若官方没有返回媒体地址，会给出
# 可重试的 503，而不会把“只有标题”的结果冒充成可下载视频。

DOUYIN_WORK_HOST_SUFFIXES = ("douyin.com", "iesdouyin.com")
DOUYIN_MEDIA_CACHE_TTL = _clamped_env_int(
    "DOUYIN_MEDIA_CACHE_TTL", 300, 30, 1800)
DOUYIN_BROWSER_TIMEOUT = _clamped_env_int(
    "DOUYIN_BROWSER_TIMEOUT", 35, 8, 90)
DOUYIN_BROWSER_START_TIMEOUT = _clamped_env_int(
    "DOUYIN_BROWSER_START_TIMEOUT", 8, 3, 30)
_douyin_media_cache: dict = {}       # item_id -> {url, urls, fetched_at, expires_at}
_douyin_note_media_cache: dict = {}  # item_id -> {urls, fetched_at, expires_at}
_douyin_result_cache: dict = {}      # item_id -> (timestamp, normalized result)
_douyin_media_lock = threading.RLock()
_douyin_item_locks: dict = {}
_douyin_item_locks_guard = threading.Lock()
_douyin_browser_lock = threading.Lock()
_douyin_browser_proxy_cache: dict = {}
_douyin_browser_proxy_lock = threading.RLock()


@contextmanager
def _douyin_item_lock(item_id: str):
    """按作品 ID 串行刷新；引用计数防止锁尚未 acquire 就被淘汰。"""
    item_id = str(item_id)
    with _douyin_item_locks_guard:
        entry = _douyin_item_locks.get(item_id)
        if entry is None:
            entry = {"lock": threading.Lock(), "refs": 0, "last": time.time()}
            _douyin_item_locks[item_id] = entry
        entry["refs"] += 1
        entry["last"] = time.time()
        lock = entry["lock"]
    try:
        with lock:
            yield
    finally:
        with _douyin_item_locks_guard:
            current = _douyin_item_locks.get(item_id)
            if current is entry:
                entry["refs"] = max(0, int(entry["refs"]) - 1)
                entry["last"] = time.time()
            if len(_douyin_item_locks) > 2048:
                idle = sorted(
                    ((key, value) for key, value in _douyin_item_locks.items()
                     if int(value.get("refs") or 0) == 0
                     and not value["lock"].locked()),
                    key=lambda pair: float(pair[1].get("last") or 0))
                for key, _ in idle[:max(0, len(_douyin_item_locks) - 1536)]:
                    _douyin_item_locks.pop(key, None)


def _douyin_work_host(value: str) -> str:
    try:
        parsed = urlparse.urlsplit(str(value or "").strip())
        if (parsed.scheme.lower() != "https" or parsed.username
                or parsed.password or parsed.port not in (None, 443)):
            return ""
        host = (parsed.hostname or "").lower().rstrip(".")
    except (TypeError, ValueError):
        return ""
    if any(host == suffix or host.endswith("." + suffix)
           for suffix in DOUYIN_WORK_HOST_SUFFIXES):
        return host
    return ""


def _is_douyin_work_url(value: str) -> bool:
    return bool(_douyin_work_host(value))


def _douyin_item_from_url(value: str) -> tuple[str, str]:
    """从官方作品/分享 URL 提取 (kind, aweme_id)，不跟随外站跳转。"""
    try:
        parsed = urlparse.urlsplit(str(value or "").strip())
    except (TypeError, ValueError):
        return "", ""
    if not _douyin_work_host(value):
        return "", ""
    path = urlparse.unquote(parsed.path or "")
    patterns = (
        r"/(?:share/)?(video|note|slides)/(\d{8,30})(?:/|$)",
        r"/(video|note|slides)/(\d{8,30})(?:/|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, path, re.I)
        if match:
            kind = "note" if match.group(1).lower() in ("note", "slides") else "video"
            return kind, match.group(2)
    query = urlparse.parse_qs(parsed.query or "")
    for key in ("aweme_id", "awemeId", "item_id", "itemId", "modal_id"):
        candidate = (query.get(key) or [""])[0]
        if re.fullmatch(r"\d{8,30}", str(candidate)):
            return ("note" if "note" in path.lower() else "video", str(candidate))
    return "", ""


def _douyin_work_url(kind: str, item_id: str) -> str:
    kind = "note" if str(kind or "").lower() in ("note", "slides") else "video"
    return f"https://www.douyin.com/{kind}/{item_id}/"


def _douyin_response_location(resp) -> str:
    """读取重定向位置而不把可能含分享参数的 URL 写入日志。"""
    headers = getattr(resp, "headers", None)
    if headers:
        return str(headers.get("Location") or headers.get("location") or "")
    return ""


def _douyin_resolve_share_url(short_url: str) -> tuple[str, str, str]:
    """只跟随抖音官方跳转，返回 ``(kind, item_id, canonical_url)``。"""
    if not _is_douyin_work_url(short_url):
        raise ApiError(400, "不是有效的抖音作品链接")
    current = str(short_url).strip()
    visited = set()
    for _ in range(6):
        if current in visited:
            raise ApiError(502, "抖音官方链接出现循环跳转，请稍后重试")
        visited.add(current)
        kind, item_id = _douyin_item_from_url(current)
        if item_id:
            return kind, item_id, _douyin_work_url(kind, item_id)
        resp = None
        try:
            resp, _ = open_url(
                current, follow=False,
                headers={"Accept": "text/html,application/xhtml+xml",
                         "Referer": "https://www.douyin.com/"},
                timeout=20)
            location = _douyin_response_location(resp)
            if not location:
                geturl = getattr(resp, "geturl", None)
                candidate = geturl() if callable(geturl) else ""
                # urllib 在未跳转的 200 响应上也会返回原 URL。
                # 将它当 Location 会在下一轮命中 visited，从而误报循环，
                # 并且永远不会解析页面内的 canonical 链接。
                if candidate:
                    resolved = urlparse.urljoin(current, str(candidate))
                    if resolved.rstrip("/") != current.rstrip("/"):
                        location = candidate
            if location:
                target = urlparse.urljoin(current, str(location))
                if not _is_douyin_work_url(target):
                    raise ApiError(400, "抖音官方短链跳转目标无效")
                current = target
                continue
            if hasattr(resp, "read"):
                try:
                    body = resp.read(256 * 1024)
                except TypeError:
                    body = resp.read()
            else:
                body = b""
            text = (body.decode("utf-8", "ignore")
                    if isinstance(body, bytes) else str(body)).replace("\\/", "/")
            for candidate in re.findall(r"https://[^\"'<>\s]+", text, re.I):
                if not _is_douyin_work_url(candidate):
                    continue
                kind, item_id = _douyin_item_from_url(candidate)
                if item_id:
                    return kind, item_id, _douyin_work_url(kind, item_id)
            break
        except ApiError:
            raise
        except urlerr.HTTPError as exc:
            if exc.code in (404, 410):
                raise ApiError(404, "抖音作品链接已失效或不存在")
            raise ApiError(502, "抖音官方链接暂时无法访问，请稍后重试")
        except (urlerr.URLError, TimeoutError, OSError):
            raise ApiError(502, "抖音官方链接暂时无法访问，请稍后重试")
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
    raise ApiError(404, "未能从抖音短链得到作品 ID，请确认链接未过期")


class _CDPConnection:
    """极小的 Chrome DevTools Protocol WebSocket 客户端（仅标准库）。

    生产镜像不必安装 websocket/Playwright 依赖；协议只用到文本帧、ping/pong
    和关闭帧。连接生命周期严格限制在一次解析内，浏览器 profile 也会被删除。
    """

    def __init__(self, ws_url: str, timeout: float = 10):
        parsed = urlparse.urlsplit(ws_url)
        if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
            raise ValueError("invalid CDP websocket URL")
        if parsed.scheme == "wss":
            raise ValueError("wss CDP endpoints are not supported")
        self.sock = socket.create_connection(
            (parsed.hostname, parsed.port or 80), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {parsed.path or '/'}{('?' + parsed.query) if parsed.query else ''} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port or 80}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response and len(response) < 65536:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            response += chunk
        if not response.startswith(b"HTTP/1.1 101"):
            self.close()
            raise OSError("CDP websocket handshake failed")
        self._next_id = 0
        self._pending_messages = []

    def _read_exact(self, size: int) -> bytes:
        out = bytearray()
        while len(out) < size:
            chunk = self.sock.recv(size - len(out))
            if not chunk:
                raise EOFError("CDP websocket closed")
            out.extend(chunk)
        return bytes(out)

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        length = len(payload)
        if length < 126:
            header = bytes((0x80 | opcode, 0x80 | length))
        elif length < (1 << 16):
            header = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack(">H", length)
        else:
            header = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack(">Q", length)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send_json(self, value: dict) -> None:
        self._send_frame(0x1, json.dumps(value, separators=(",", ":")).encode("utf-8"))

    def recv_json(self, use_pending: bool = True) -> dict:
        """读取一条 CDP 消息。

        ``command`` 发送请求后必须绕过旧事件队列直接读 socket；否则在页面
        导航期间积压的大量 Network 事件会把刚返回的 command response 挡在
        队列末尾，表现为 Target.attach/Network.getResponseBody 超时。
        """
        if use_pending and self._pending_messages:
            return self._pending_messages.pop(0)
        fragments = []
        while True:
            first, second = self._read_exact(2)
            opcode = first & 0x0F
            fin = bool(first & 0x80)
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length) if length else b""
            if masked:
                payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
            if opcode == 0x8:
                raise EOFError("CDP websocket closed")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode in (0x1, 0x0):
                fragments.append(payload)
                if fin:
                    return json.loads(b"".join(fragments).decode("utf-8"))
            elif opcode == 0xA:
                continue

    def command(self, method: str, params: Optional[dict] = None,
                session_id: str = "", timeout: float = 10) -> dict:
        self._next_id += 1
        ident = self._next_id
        message = {"id": ident, "method": method}
        if params is not None:
            message["params"] = params
        if session_id:
            message["sessionId"] = session_id
        self.send_json(message)
        # 先检查此前暂存的事件中是否已经有本次响应（例如上一个 command
        # 读取过头）；正常情况下响应会直接从 socket 到达。
        for index, queued in enumerate(self._pending_messages):
            if queued.get("id") == ident:
                self._pending_messages.pop(index)
                if "error" in queued:
                    raise RuntimeError(str(queued["error"]))
                return queued.get("result") or {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # 不经过 pending FIFO，避免事件洪峰饿死 command response。
            message = self.recv_json(use_pending=False)
            if message.get("id") == ident:
                if "error" in message:
                    raise RuntimeError(str(message["error"]))
                return message.get("result") or {}
            if len(self._pending_messages) < 2048:
                self._pending_messages.append(message)
        raise TimeoutError("CDP command timed out")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def _douyin_browser_binary() -> str:
    configured = (os.environ.get("DOUYIN_BROWSER_BIN") or "").strip()
    candidates = [configured] if configured else []
    candidates.extend([
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome-stable", "/usr/bin/google-chrome",
        "/usr/bin/chromium", "/usr/bin/chromium-browser",
    ])
    for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ""


def _douyin_browser_enabled() -> bool:
    value = (os.environ.get("DOUYIN_BROWSER_ENABLED", "auto") or "auto").strip().lower()
    if value in ("0", "false", "no", "off"):
        return False
    if value in ("1", "true", "yes", "on"):
        return True
    return bool(_douyin_browser_binary())


def _free_local_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _douyin_browser_proxy_candidate(proxy: dict):
    """把一条代理转成 Chromium 参数；不支持的记录返回 None。"""
    try:
        parsed = urlparse.urlsplit(str(proxy.get("url") or ""))
        if not parsed.hostname or parsed.port is None:
            return None
        scheme = (parsed.scheme or "http").lower()
        scheme = {"socks5h": "socks5", "socks4a": "socks4"}.get(scheme, scheme)
        if scheme not in ("http", "https", "socks4", "socks5"):
            return None
        # Chromium supports HTTP(S) proxy authentication through Fetch events.
        # A managed mihomo mixed-port accepts both protocols, so use HTTP for
        # the browser while retaining the original URL for urllib media fetches.
        browser_scheme = "http" if proxy.get("managed") and scheme.startswith("socks") else scheme
        if ((parsed.username or parsed.password)
                and browser_scheme not in ("http", "https")):
            return None
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return proxy, f"{browser_scheme}://{host}:{int(parsed.port)}"
    except (TypeError, ValueError):
        return None


def _douyin_browser_proxy_choices() -> list:
    """按代理池策略返回有界的 Chromium 出口候选。"""
    choices = []
    for proxy in proxy_mgr.candidates()[:max(1, proxy_mgr.retries)]:
        candidate = _douyin_browser_proxy_candidate(proxy)
        if candidate:
            choices.append(candidate)
    if not proxy_mgr.force_proxy:
        choices.append(None)  # 管理员显式允许时，所有代理失败后才直连。
    return choices or [False]


def _douyin_browser_proxy():
    """兼容旧调用；完整解析链会迭代 ``_douyin_browser_proxy_choices``。"""
    return _douyin_browser_proxy_choices()[0]


def _douyin_browser_credentials(proxy: Optional[dict], proxy_arg: str):
    """Extract bounded proxy credentials for CDP, never for process arguments."""
    if not proxy or not proxy_arg:
        return None
    try:
        parsed = urlparse.urlsplit(str(proxy.get("url") or ""))
        scheme = urlparse.urlsplit(proxy_arg).scheme.lower()
        if scheme not in ("http", "https") or not parsed.username:
            return None
        user = urlparse.unquote(parsed.username)
        password = urlparse.unquote(parsed.password or "")
        if (not user or len(user) > 256 or len(password) > 256
                or any(ch in user or ch in password for ch in "\r\n")):
            return None
        return user, password
    except (TypeError, ValueError):
        return None


def _douyin_browser_proxy_for_item(item_id: str) -> Optional[dict]:
    with _douyin_browser_proxy_lock:
        record = _douyin_browser_proxy_cache.get(str(item_id))
        if not isinstance(record, dict):
            return None
        if float(record.get("expires_at") or 0) <= time.time():
            _douyin_browser_proxy_cache.pop(str(item_id), None)
            return None
        value = record.get("proxy")
        if not isinstance(value, dict):
            return None
        # 后台停用/删除代理后，不得继续将媒体请求钉在旧出口。
        url = str(value.get("url") or "")
        active = any(p.get("enabled") and str(p.get("url") or "") == url
                     for p in list(proxy_mgr.proxies))
        if not active:
            _douyin_browser_proxy_cache.pop(str(item_id), None)
            return None
        return dict(value)


def _remember_douyin_browser_proxy(item_id: str, proxy: Optional[dict]) -> None:
    with _douyin_browser_proxy_lock:
        if proxy:
            _douyin_browser_proxy_cache[str(item_id)] = {
                "proxy": dict(proxy),
                "expires_at": time.time() + max(DOUYIN_MEDIA_CACHE_TTL, 300),
            }
            if len(_douyin_browser_proxy_cache) > 2048:
                oldest = sorted(
                    _douyin_browser_proxy_cache.items(),
                    key=lambda pair: float(pair[1].get("expires_at") or 0))
                for key, _ in oldest[:len(_douyin_browser_proxy_cache) - 1536]:
                    _douyin_browser_proxy_cache.pop(key, None)
        else:
            _douyin_browser_proxy_cache.pop(str(item_id), None)


def _douyin_browser_extract_once(item_id: str, kind: str,
                                 proxy_choice) -> Optional[dict]:
    """使用一个已验证出口启动隔离 Chromium 并捕获 detail JSON。"""
    browser_proxy, proxy_arg = proxy_choice or (None, "")
    browser_credentials = _douyin_browser_credentials(browser_proxy, proxy_arg)
    # A malformed/unsafe credential must never turn into an unauthenticated
    # request when the deployment requires a proxy.  In non-forced mode the
    # caller may still use the direct official page path.
    if browser_proxy and (browser_proxy.get("url") or "").find("@") >= 0 \
            and not browser_credentials and proxy_mgr.force_proxy:
        return None
    if not _douyin_browser_lock.acquire(timeout=max(1, DOUYIN_BROWSER_TIMEOUT)):
        return None
    profile = None
    process = None
    conn = None
    try:
        binary = _douyin_browser_binary()
        if not binary:
            return None
        profile = tempfile.mkdtemp(prefix="douyin-browser-")
        port = _free_local_port()
        command = [
            binary, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--disable-dev-shm-usage", "--disable-extensions", "--disable-sync",
            "--disable-background-networking", "--disable-blink-features=AutomationControlled",
            "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={port}",
            "--proxy-bypass-list=<-loopback>;127.0.0.1;localhost",
            f"--user-data-dir={profile}",
        ]
        if proxy_arg:
            command.append(f"--proxy-server={proxy_arg}")
        command.append("about:blank")
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        version = None
        start_deadline = time.monotonic() + DOUYIN_BROWSER_START_TIMEOUT
        while time.monotonic() < start_deadline and process.poll() is None:
            try:
                with urlreq.urlopen(
                        f"http://127.0.0.1:{port}/json/version", timeout=0.5) as response:
                    version = json.loads(response.read().decode("utf-8", "ignore"))
                break
            except Exception:
                time.sleep(0.08)
        if not isinstance(version, dict) or not version.get("webSocketDebuggerUrl"):
            return None
        conn = _CDPConnection(version["webSocketDebuggerUrl"], timeout=5)
        target_id = conn.command("Target.createTarget", {"url": "about:blank"}).get("targetId")
        if not target_id:
            return None
        attached = conn.command("Target.attachToTarget",
                                {"targetId": target_id, "flatten": True})
        session_id = attached.get("sessionId") or ""
        if not session_id:
            return None
        conn.command("Network.enable", {}, session_id)
        conn.command("Page.enable", {}, session_id)
        if browser_credentials:
            # Fetch is enabled only for authenticated proxy sessions.  Every
            # paused request is immediately continued below; auth challenges
            # receive credentials in the CDP channel, never in a URL/header.
            conn.command("Fetch.enable", {
                "handleAuthRequests": True,
                "patterns": [{"urlPattern": "*", "requestStage": "Request"}],
            }, session_id)
        try:
            conn.command("Network.setCacheDisabled", {"cacheDisabled": True}, session_id)
            conn.command("Network.setBlockedURLs", {"urls": [
                "*://*.douyinvod.com/*", "*://*.douyincdn.com/*",
                "*://*.ibytedtos.com/*",
            ]}, session_id)
        except Exception:
            pass
        conn.command("Page.navigate", {"url": _douyin_work_url(kind, item_id)},
                     session_id, timeout=15)
        pending = {}
        deadline = time.monotonic() + DOUYIN_BROWSER_TIMEOUT

        def decode_response(request_id: str):
            for _ in range(2):
                try:
                    body_result = conn.command(
                        "Network.getResponseBody", {"requestId": request_id},
                        session_id, timeout=8)
                    body = body_result.get("body") or ""
                    if body_result.get("base64Encoded"):
                        body = base64.b64decode(body).decode("utf-8", "replace")
                    if not body or len(body) > 8 * 1024 * 1024:
                        return None
                    payload = json.loads(body)
                    if not isinstance(payload, dict):
                        return None
                    detail = payload.get("aweme_detail")
                    return detail if isinstance(detail, dict) else payload
                except Exception:
                    time.sleep(0.05)
            return None

        while time.monotonic() < deadline:
            try:
                message = conn.recv_json()
            except socket.timeout:
                # CDP socket has a short read timeout so a quiet page does not
                # block shutdown forever; a slow official response may still
                # arrive before the overall browser deadline.
                continue
            if message.get("sessionId") != session_id:
                continue
            method = message.get("method")
            params = message.get("params") or {}
            if method == "Fetch.authRequired":
                request_id = str(params.get("requestId") or "")
                challenge = params.get("authChallenge") or {}
                if request_id:
                    source = str(challenge.get("source") or "").lower()
                    auth_response = "Default"
                    auth_params = {"response": auth_response}
                    if source == "proxy" and browser_credentials:
                        auth_params = {
                            "response": "ProvideCredentials",
                            "username": browser_credentials[0],
                            "password": browser_credentials[1],
                        }
                    try:
                        # CDP names this field authChallengeResponse (not
                        # authChallenge); the latter is the challenge received
                        # in the event and makes authenticated proxies hang.
                        conn.command("Fetch.continueWithAuth", {
                            "requestId": request_id,
                            "authChallengeResponse": auth_params,
                        }, session_id, timeout=5)
                    except Exception:
                        pass
                continue
            if method == "Fetch.requestPaused":
                request_id = str(params.get("requestId") or "")
                if request_id:
                    try:
                        conn.command("Fetch.continueRequest",
                                     {"requestId": request_id},
                                     session_id, timeout=5)
                    except Exception:
                        pass
                continue
            if method == "Network.responseReceived":
                response = params.get("response") or {}
                response_url = str(response.get("url") or "")
                try:
                    parsed_url = urlparse.urlsplit(response_url)
                    host = (parsed_url.hostname or "").lower().rstrip(".")
                    path = parsed_url.path.lower()
                except Exception:
                    host, path = "", ""
                official_host = any(
                    host == suffix or host.endswith("." + suffix)
                    for suffix in ("douyin.com", "iesdouyin.com", "snssdk.com"))
                if (not official_host
                        or ("/aweme/v1/web/aweme/detail" not in path
                            and "/aweme/v1/aweme/detail" not in path)
                        or int(response.get("status") or 0) != 200):
                    continue
                request_id = params.get("requestId")
                if request_id:
                    pending[str(request_id)] = True
            elif method == "Network.loadingFinished":
                request_id = str(params.get("requestId") or "")
                if request_id not in pending:
                    continue
                pending.pop(request_id, None)
                payload = decode_response(request_id)
                if payload is not None:
                    _remember_douyin_browser_proxy(item_id, browser_proxy)
                    return payload
        for request_id in list(pending)[:4]:
            payload = decode_response(request_id)
            if payload is not None:
                _remember_douyin_browser_proxy(item_id, browser_proxy)
                return payload
        return None
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if process is not None:
            try:
                pgid = os.getpgid(process.pid)
                os.killpg(pgid, signal.SIGTERM)
            except Exception:
                try:
                    process.terminate()
                except Exception:
                    pass
            try:
                process.wait(timeout=3)
            except Exception:
                try:
                    pgid = os.getpgid(process.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
        if profile:
            shutil.rmtree(profile, ignore_errors=True)
        _douyin_browser_lock.release()


def _douyin_browser_extract(item_id: str, kind: str = "video") -> Optional[dict]:
    """迭代代理池候选；单个坏代理不再让整次官方解析失败。"""
    if (not re.fullmatch(r"\d{8,30}", str(item_id or ""))
            or not _douyin_browser_enabled()):
        return None
    for index, proxy_choice in enumerate(_douyin_browser_proxy_choices()):
        if proxy_choice is False:
            continue
        if index:
            proxy_mgr.note_retry()
        payload = _douyin_browser_extract_once(item_id, kind, proxy_choice)
        if payload is not None:
            return payload
    return None

def _douyin_extract_json_after(text: str, marker: str):
    """从脚本标记后提取一个平衡的 JSON 对象/数组。"""
    start_at = text.find(marker)
    if start_at < 0:
        return None
    start = start_at + len(marker)
    while start < len(text) and text[start] not in "[{":
        start += 1
    if start >= len(text):
        return None
    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, min(len(text), start + 8 * 1024 * 1024)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:index + 1])
                except (TypeError, ValueError):
                    return None
    return None


def _douyin_html_payloads(text: str) -> list:
    payloads = []
    for marker in ("window._ROUTER_DATA", "window.__ROUTER_DATA",
                   "window._SSR_DATA", "window.__SSR_DATA"):
        value = _douyin_extract_json_after(text, marker)
        if value is not None:
            payloads.append(value)
    for match in re.finditer(
            r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
            text, re.I | re.S):
        try:
            value = json.loads(match.group(1).strip())
            if value:
                payloads.append(value)
        except (TypeError, ValueError):
            pass
    return payloads


def _douyin_fetch_html(kind: str, item_id: str) -> tuple[str, list]:
    url = _douyin_work_url(kind, item_id)
    response = None
    try:
        response, used_proxy = open_url(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
                "Referer": "https://www.douyin.com/",
            },
            timeout=20)
        if used_proxy:
            # SSR/JSON-LD 里的签名媒体也可能绑定抓取时出口；
            # 与 Chromium 链路一样固定后续图片/视频请求。
            _remember_douyin_browser_proxy(item_id, used_proxy)
        try:
            raw = response.read(4 * 1024 * 1024)
        except TypeError:
            raw = response.read()
        if (response.headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
        text = raw.decode("utf-8", "replace")
        return text, _douyin_html_payloads(text)
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def _douyin_first(mapping, *keys, default=None):
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _douyin_url_values(value, limit: int = 32) -> list[str]:
    """从 url_list/url/uri 结构收集字符串，保留顺序并限制规模。"""
    out, seen = [], set()
    def visit(node):
        if len(out) >= limit:
            return
        if isinstance(node, str):
            value = node.strip()
            if value and value not in seen:
                seen.add(value)
                out.append(value)
            return
        if isinstance(node, dict):
            for key in ("url_list", "urlList", "url", "src", "download_url",
                        "downloadUrl", "play_url", "playUrl", "image_url",
                        "imageUrl", "uri"):
                if key in node:
                    visit(node[key])
                    if len(out) >= limit:
                        return
            return
        if isinstance(node, (list, tuple)):
            for item in node:
                visit(item)
                if len(out) >= limit:
                    return
    visit(value)
    return out


def _douyin_public_url(value: str) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 4096:
        return ""
    try:
        parsed = urlparse.urlsplit(value)
        if (parsed.scheme.lower() != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443) or not parsed.hostname
                or not _host_allowed(value)):
            return ""
    except (TypeError, ValueError):
        return ""
    # CDN 签名覆盖路径本身；不能把 /playwm/ 擅自改成 /play/。
    return value


def _douyin_public_urls(value) -> list[str]:
    return list(dict.fromkeys(
        url for url in (_douyin_public_url(x) for x in _douyin_url_values(value))
        if url))


def _douyin_number(value):
    if value is None or value == "":
        return None
    try:
        number = int(value)
        return number
    except (TypeError, ValueError, OverflowError):
        try:
            return int(float(value))
        except (TypeError, ValueError, OverflowError):
            return None


def _douyin_find_item(payload, _depth: int = 0):
    """从官方响应/SSR 任意嵌套层找作品对象。"""
    if _depth > 16:
        return None
    if isinstance(payload, dict):
        # 先取明确的容器，避免把 author/statistics 子对象当作品。
        for key in ("aweme_detail", "item", "aweme", "post", "data"):
            value = payload.get(key)
            if isinstance(value, dict):
                found = _douyin_find_item(value, _depth + 1)
                if found:
                    return found
            elif isinstance(value, list):
                found = _douyin_find_item(value, _depth + 1)
                if found:
                    return found
        for key in ("item_list", "aweme_list", "items", "list"):
            value = payload.get(key)
            if isinstance(value, list):
                for entry in value:
                    found = _douyin_find_item(entry, _depth + 1)
                    if found:
                        return found
        if (any(key in payload for key in ("aweme_id", "awemeId", "item_id"))
                and any(key in payload for key in ("video", "images", "statistics", "author", "desc"))):
            return payload
        if payload.get("@type") in ("VideoObject", "ImageObject"):
            return payload
        for value in payload.values():
            found = _douyin_find_item(value, _depth + 1)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _douyin_find_item(value, _depth + 1)
            if found:
                return found
    return None


def _douyin_extract_video_id(urls, video_obj: dict) -> str:
    for url in urls:
        try:
            query = urlparse.parse_qs(urlparse.urlsplit(url).query)
        except Exception:
            query = {}
        for key in ("video_id", "videoId", "vid", "file_id", "fileId"):
            candidate = (query.get(key) or [""])[0]
            if re.fullmatch(r"[\w-]{8,120}", str(candidate)):
                return str(candidate)
    for key in ("video_id", "videoId", "vid", "file_id", "fileId", "uri"):
        candidate = str(video_obj.get(key) or "").strip()
        if re.fullmatch(r"[\w-]{8,120}", candidate):
            return candidate
    return ""


def _douyin_urls_expiry(urls: list[str], now: Optional[float] = None) -> float:
    now = time.time() if now is None else float(now)
    expires = now + DOUYIN_MEDIA_CACHE_TTL
    # 只要 URL 明确声明了过期时间，就必须尊重它，包括已过期值。
    # 不能将已死链接回退为默认 TTL，也不能强行延长临近过期链接。
    for url in urls:
        try:
            query = urlparse.parse_qs(urlparse.urlsplit(url).query)
            for key in ("x-expires", "expires", "expire"):
                raw = (query.get(key) or [""])[0]
                if str(raw).isdigit():
                    expires = min(expires, float(raw))
        except Exception:
            pass
    return expires


def _douyin_cache_media(item_id: str, urls: list[str],
                        proxy: Optional[dict] = None) -> dict:
    urls = list(dict.fromkeys(url for url in urls if _douyin_public_url(url)))
    if not urls:
        return {}
    now = time.time()
    record = {"url": urls[0], "urls": urls, "fetched_at": now,
              "expires_at": _douyin_urls_expiry(urls, now)}
    if proxy:
        record["proxy"] = dict(proxy)
    with _douyin_media_lock:
        _douyin_media_cache[str(item_id)] = record
    return record


def _douyin_cache_note_media(item_id: str, urls: list[str]) -> dict:
    urls = list(dict.fromkeys(url for url in urls if _douyin_public_url(url)))[:100]
    if not urls:
        return {}
    now = time.time()
    record = {"urls": urls, "fetched_at": now,
              "expires_at": _douyin_urls_expiry(urls, now)}
    with _douyin_media_lock:
        _douyin_note_media_cache[str(item_id)] = record
    return record


def _douyin_cached_note_media(item_id: str,
                              allow_stale: bool = False) -> Optional[dict]:
    with _douyin_media_lock:
        record = _douyin_note_media_cache.get(str(item_id))
        if not record:
            return None
        if not allow_stale and float(record.get("expires_at") or 0) <= time.time() + 10:
            return None
        return {**record, "urls": list(record.get("urls") or [])}


def _douyin_note_result_with_media(result: dict,
                                   item_id: str) -> Optional[dict]:
    """给图集快照注入当前有效的 CDN 与同源刷新端点。

    数据库/结果缓存只保留文件名等稳定元数据；原始签名
    URL 只住在短时内存缓存里，避免分享页长期下发死链。
    """
    record = _douyin_cached_note_media(item_id)
    if not record:
        return None
    urls = list(record.get("urls") or [])
    if not urls:
        return None
    cloned = json.loads(json.dumps(result or {}, ensure_ascii=False))
    old_images = cloned.get("images") if isinstance(cloned.get("images"), list) else []
    base = _safe_name(str(cloned.get("title") or ""), item_id)
    images = []
    for index, direct in enumerate(urls, 1):
        old = old_images[index - 1] if index <= len(old_images) else {}
        old = old if isinstance(old, dict) else {}
        filename = str(old.get("filename") or f"{base}_{index:02d}.jpeg")[:180]
        images.append({
            "index": index,
            "filename": filename,
            "url": direct,
            "proxy_url": _douyin_image_proxy_url(item_id, index, filename),
            "download_url": _douyin_image_proxy_url(
                item_id, index, filename, download=True),
        })
    cloned["images"] = images
    cloned["media_available"] = bool(images)
    return cloned


def _douyin_cached_media(item_id: str, allow_stale: bool = False) -> Optional[dict]:
    with _douyin_media_lock:
        record = _douyin_media_cache.get(str(item_id))
        if not record:
            return None
        if not allow_stale and float(record.get("expires_at") or 0) <= time.time() + 10:
            return None
        return dict(record)


def _sweep_douyin_memory() -> None:
    """清理短时官方媒体/结果缓存，避免长时间运行进程无界增长。"""
    now = time.time()
    with _douyin_media_lock:
        for item_id, record in list(_douyin_media_cache.items()):
            if float(record.get("expires_at") or 0) <= now:
                _douyin_media_cache.pop(item_id, None)
        for item_id, record in list(_douyin_note_media_cache.items()):
            if float(record.get("expires_at") or 0) <= now:
                _douyin_note_media_cache.pop(item_id, None)
        for item_id, (ts, _) in list(_douyin_result_cache.items()):
            if now - ts > CACHE_TTL:
                _douyin_result_cache.pop(item_id, None)
        for cache in (_douyin_media_cache, _douyin_note_media_cache,
                      _douyin_result_cache):
            if len(cache) > 2048:
                for key in list(cache)[:len(cache) - 1536]:
                    cache.pop(key, None)
    with _douyin_browser_proxy_lock:
        for item_id, record in list(_douyin_browser_proxy_cache.items()):
            if float(record.get("expires_at") or 0) <= now:
                _douyin_browser_proxy_cache.pop(item_id, None)
    # 作者浮层只是短时富化缓存，不得随作品数无界增长。
    for item_id, (ts, _) in list(_author_cache.items()):
        if now - ts > 3600:
            _author_cache.pop(item_id, None)
    if len(_author_cache) > 2048:
        oldest = sorted(_author_cache.items(), key=lambda pair: pair[1][0])
        for item_id, _ in oldest[:len(_author_cache) - 1536]:
            _author_cache.pop(item_id, None)


def _douyin_native_result(payload, work_url: str, item_id_hint: str = "",
                          kind_hint: str = "") -> Optional[dict]:
    """把 detail/SSR/JSON-LD 归一化成首页、分享页和 API 共用的结果契约。"""
    item = _douyin_find_item(payload)
    if not isinstance(item, dict):
        return None
    item_id = str(_douyin_first(
        item, "aweme_id", "awemeId", "item_id", "itemId", default=item_id_hint) or "").strip()
    expected_item_id = str(item_id_hint or "").strip()
    if (re.fullmatch(r"\d{8,30}", expected_item_id)
            and re.fullmatch(r"\d{8,30}", item_id)
            and item_id != expected_item_id):
        # CDP 页面可能同时发起推荐作品请求；不能把 B 的响应
        # 以 A 的缓存键保存，否则会返回错作品。
        return None
    if not re.fullmatch(r"\d{8,30}", item_id):
        item_id = str(item_id_hint or "").strip()
    if not re.fullmatch(r"\d{8,30}", item_id):
        return None

    author_data = _douyin_first(item, "author", "creator", default={})
    # Schema.org VideoObject 常用 creator/name，而官方 detail 使用 author。
    if not author_data:
        author_data = _douyin_first(item, "copyright_holder", default={})
    if not isinstance(author_data, dict):
        author_data = {"nickname": str(author_data or "")}
    author = str(_douyin_first(
        author_data, "nickname", "name", "display_name", default=
        _douyin_first(item, "author_name", "authorName", "nickname", default="")) or "")[:100]
    sec_uid = str(_douyin_first(author_data, "sec_uid", "secUid", default="") or "")[:200]
    unique_id = str(_douyin_first(
        author_data, "unique_id", "uniqueId", "short_id", "shortId", default="") or "")[:100]
    avatar_candidates = []
    for key in ("avatar_larger", "avatar_thumb", "avatar_medium", "avatar", "avatarUrl"):
        avatar_candidates.extend(_douyin_url_values(author_data.get(key)))
    avatar_candidates.extend(_douyin_url_values(
        _douyin_first(item, "avatar", "avatar_url", "avatarUrl", default="")))
    avatar = next(iter(dict.fromkeys(
        x for x in (_douyin_public_url(v) for v in avatar_candidates) if x)), "")
    author_url = next(iter(dict.fromkeys(
        x for x in (_douyin_public_url(v) for v in _douyin_url_values(
            _douyin_first(author_data, "url", "homepage", "profile_url", default=""))) if x)), "")
    if not author_url and sec_uid:
        author_url = f"https://www.douyin.com/user/{urlparse.quote(sec_uid, safe='')}"

    stats_data = _douyin_first(item, "statistics", "stats", "interaction", default={})
    if not isinstance(stats_data, dict):
        stats_data = {}
    # JSON-LD 的 interactionStatistic 是数组，不同站点会把名称写成
    # LikeAction/CommentAction/ShareAction 或 interactionType。
    interactions = item.get("interactionStatistic") or item.get("interaction_statistic") or []
    if isinstance(interactions, dict):
        interactions = [interactions]
    if isinstance(interactions, list):
        for interaction in interactions:
            if not isinstance(interaction, dict):
                continue
            name = str(_douyin_first(interaction, "interactionType", "name", default="") or "").lower()
            count = _douyin_first(interaction, "userInteractionCount", "count", "value", default=None)
            if count is None:
                continue
            if "like" in name or "digg" in name:
                stats_data.setdefault("digg_count", count)
            elif "comment" in name:
                stats_data.setdefault("comment_count", count)
            elif "share" in name:
                stats_data.setdefault("share_count", count)
            elif "collect" in name or "favorite" in name:
                stats_data.setdefault("collect_count", count)
    stats = {
        "digg": _douyin_number(_douyin_first(
            stats_data, "digg_count", "digg", "like_count", "likeCount",
            default=_douyin_first(item, "digg_count", "diggCount", "like_count", default=None))),
        "comment": _douyin_number(_douyin_first(
            stats_data, "comment_count", "comment", "commentCount",
            default=_douyin_first(item, "comment_count", "commentCount", default=None))),
        "collect": _douyin_number(_douyin_first(
            stats_data, "collect_count", "collect", "collectCount", "favorite_count",
            default=_douyin_first(item, "collect_count", "collectCount", default=None))),
        "share": _douyin_number(_douyin_first(
            stats_data, "share_count", "share", "shareCount",
            default=_douyin_first(item, "share_count", "shareCount", default=None))),
    }

    title = str(_douyin_first(
        item, "desc", "title", "name", "preview_title", "item_title",
        "description", default="（无标题）") or "（无标题）").strip()
    title = title[:1000] or "（无标题）"
    content = str(_douyin_first(item, "desc", "description", "content", default=title) or title)
    tags = list(dict.fromkeys(re.findall(r"#\s*([^\s#]+)", content)))[:50]
    for extra in item.get("text_extra") or item.get("textExtra") or []:
        if isinstance(extra, dict):
            name = str(_douyin_first(extra, "hashtag_name", "hashtagName", default="") or "").strip()
            if name and name not in tags:
                tags.append(name)
    tags = tags[:50]

    video_data = _douyin_first(item, "video", "video_info", "videoInfo", default={})
    if not isinstance(video_data, dict):
        video_data = {}
    play_values = _douyin_public_urls(_douyin_first(video_data, "play_addr", "playAddr", default={}))
    download_values = _douyin_public_urls(_douyin_first(video_data, "download_addr", "downloadAddr", default={}))
    # VideoObject fallback: contentUrl/url 就是可播放地址（仍经过域名白名单）。
    play_values += _douyin_public_urls(
        _douyin_first(item, "contentUrl", "content_url", "videoUrl", default=""))
    all_video_urls = list(dict.fromkeys(play_values + download_values))
    direct_url = all_video_urls[0] if all_video_urls else ""
    video_id = _douyin_extract_video_id(all_video_urls, video_data)

    cover_candidates = []
    for key in ("origin_cover", "cover", "dynamic_cover", "originCover", "coverUrl"):
        cover_candidates.extend(_douyin_url_values(video_data.get(key)))
    cover_candidates.extend(_douyin_url_values(_douyin_first(
        item, "cover", "cover_url", "thumbnailUrl", "thumbnail_url", default="")))
    cover = next(iter(dict.fromkeys(
        x for x in (_douyin_public_url(v) for v in cover_candidates) if x)), "")

    image_values = []
    raw_images = _douyin_first(item, "images", "image_list", "imageList", "original_images", default=[])
    if isinstance(raw_images, dict):
        raw_images = [raw_images]
    for image in (raw_images or []):
        # 每个 image 对象里的 url_list 是同一张图的多域名备选，
        # 不是多张图；只取首个可用候选，避免图集重复。
        candidates = _douyin_public_urls(image)
        if candidates:
            image_values.append(candidates[0])
    image_values = list(dict.fromkeys(image_values))
    work_type = str(_douyin_first(item, "work_type", "workType", default="") or "").lower()
    is_note = bool(image_values and not direct_url) or work_type in ("image", "images", "note", "slides")
    kind = "note" if is_note else "video"
    if kind_hint == "note" and not direct_url:
        kind = "note"
    base = _safe_name(title, item_id)

    author_detail = {
        "item_id": item_id, "nickname": author, "author": author,
        "avatar": avatar, "author_url": author_url, "sec_uid": sec_uid,
        "unique_id": unique_id,
        "short_id": str(_douyin_first(author_data, "short_id", "shortId", default="") or "")[:100],
        "signature": str(_douyin_first(author_data, "signature", default="") or "")[:500],
        "follower_count": _douyin_number(_douyin_first(author_data, "follower_count", "followerCount", default=None)),
        "total_favorited": _douyin_number(_douyin_first(author_data, "total_favorited", "totalFavorited", default=None)),
        "following_count": _douyin_number(_douyin_first(author_data, "following_count", "followingCount", default=None)),
        "aweme_count": _douyin_number(_douyin_first(author_data, "aweme_count", "awemeCount", default=None)),
        "enriched": False,
    }
    # 即使没有 sec_uid 也缓存基础作者信息，/api/author 不再因普通详情而 404。
    _author_cache[item_id] = (time.time(), author_detail)

    result = {
        "kind": kind, "item_id": item_id, "source": "douyin_direct",
        "metadata_source": "douyin_web", "title": title, "platform": "douyin",
        "share_supported": True, "author": author, "avatar": avatar,
        "author_url": author_url, "create_time": _douyin_number(
            _douyin_first(item, "create_time", "createTime", default=None)),
        "stats": stats, "tags": tags, "music": None, "location": None,
        "base": base, "cover": cover, "_link": work_url,
    }
    music = _douyin_first(item, "music", default={})
    if isinstance(music, dict):
        result["music"] = {
            "title": str(_douyin_first(music, "title", "name", default="") or "")[:200],
            "author": str(_douyin_first(music, "author", "artist", default="") or "")[:100],
        }
    location = _douyin_first(item, "poi_info", "location", "address", default="")
    if isinstance(location, dict):
        location = _douyin_first(location, "poi_name", "name", "address", default="")
    result["location"] = str(location or "")[:200] or None

    if kind == "note":
        if not image_values:
            return None
        _douyin_cache_note_media(item_id, image_values)
        result["images"] = [
            {"index": index, "filename": f"{base}_{index:02d}.jpeg"}
            for index, _ in enumerate(image_values, 1)]
        return _douyin_note_result_with_media(result, item_id)

    # 官方 video.duration 明确为毫秒；JSON-LD/item.duration 通常为 ISO-8601
    # 或秒。依据字段语义区分，避免把 1000 秒以上的长视频误当毫秒。
    explicit_ms = _douyin_first(
        video_data, "duration_ms", "durationMs", default=None)
    if explicit_ms is not None:
        duration_raw, duration_is_ms = explicit_ms, True
    elif video_data.get("duration") is not None:
        duration_raw, duration_is_ms = video_data.get("duration"), True
    else:
        item_ms = _douyin_first(item, "duration_ms", "durationMs", default=None)
        duration_raw = item_ms if item_ms is not None else item.get("duration", 0)
        duration_is_ms = item_ms is not None
    duration_ms = _atc_duration_ms(duration_raw, assume_ms=duration_is_ms)
    filename = f"{base}.mp4"
    video = {
        "source": "douyin_direct", "url": direct_url,
        "direct_url": direct_url, "alt_url": _play_api_alt(video_id) if video_id else "",
        "filename": filename, "width": _douyin_first(video_data, "width", default=None),
        "height": _douyin_first(video_data, "height", default=None),
        "video_id": video_id, "media_available": bool(direct_url),
    }
    if direct_url:
        _douyin_cache_media(
            item_id, all_video_urls,
            proxy=_douyin_browser_proxy_for_item(item_id))
    # 即使当前响应没有 URL，也提供受签名保护的刷新端点；端点会重新走官方网页。
    video["proxy_url"] = _douyin_video_proxy_url(item_id)
    video["download_url"] = _douyin_video_download_url(item_id, filename)
    result["video"] = video
    result["duration_ms"] = duration_ms
    return result


def _parse_douyin_item_direct(kind: str, item_id: str,
                              work_url: str = "", *, allow_metadata: bool = False,
                              refresh: bool = False) -> dict:
    if not re.fullmatch(r"\d{8,30}", str(item_id or "")):
        raise ApiError(400, "非法的抖音作品 ID")
    lock = _douyin_item_lock(item_id)
    with lock:
        cached = _douyin_result_cache.get(item_id)
        if not refresh and cached and time.time() - cached[0] < CACHE_TTL:
            result = cached[1]
            if result.get("kind") == "note":
                fresh_note = _douyin_note_result_with_media(result, item_id)
                if fresh_note:
                    return fresh_note
                # 图集 CDN 签名已过期，继续重抓官方页刷新。
                result = None
            media = _douyin_cached_media(item_id)
            if result is not None and media:
                result = json.loads(json.dumps(result, ensure_ascii=False))
                result.setdefault("video", {})["url"] = media["url"]
                result["video"]["direct_url"] = media["url"]
                result["video"]["media_available"] = True
                return result
            # 视频/图集 CDN 签名已过期：不能把 result cache 中的旧 URL
            # 重新缓存，继续向下走官方网页捕获新签名。

        payload = _douyin_browser_extract(item_id, kind)
        result = _douyin_native_result(
            payload, work_url or _douyin_work_url(kind, item_id), item_id, kind) if payload else None
        if result and (result.get("kind") == "note"
                       or (result.get("video") or {}).get("media_available")):
            _douyin_result_cache[item_id] = (time.time(), result)
            return result

        # 没有 Chromium 时仍解析官方页面中的 SSR/JSON-LD；这些字段足够展示
        # 标题、作者、封面和统计，但不把缺媒体结果标成“可下载”。
        try:
            _, payloads = _douyin_fetch_html(kind, item_id)
        except ApiError:
            raise
        except Exception:
            payloads = []
        for candidate in payloads:
            normalized = _douyin_native_result(
                candidate, work_url or _douyin_work_url(kind, item_id), item_id, kind)
            if normalized and (normalized.get("kind") == "note"
                               or (normalized.get("video") or {}).get("media_available")):
                _douyin_result_cache[item_id] = (time.time(), normalized)
                return normalized
            if normalized and result is None:
                result = normalized
        if result:
            # 官方 HTML 可能只有元数据；允许调用方看到作者/点赞，但明确媒体不可用。
            result["metadata_only"] = True
            result.setdefault("video", {}).setdefault("media_available", False)
            if allow_metadata:
                return result
            raise ApiError(503, "抖音官方暂未返回视频地址，请稍后重试")
        raise ApiError(503, "抖音官方内容暂时无法获取，请稍后重试")


def _parse_douyin_share_direct(work_url: str) -> dict:
    kind, item_id, canonical = _douyin_resolve_share_url(work_url)
    if not item_id:
        raise ApiError(404, "未能识别抖音作品 ID")
    return _parse_douyin_item_direct(kind, item_id, canonical)


def _atc_platform_for_url(value: str) -> str:
    """识别常用内容平台；仅接受公开 HTTPS 作品链接。"""
    try:
        parsed = urlparse.urlsplit(str(value or "").strip())
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443)):
            return ""
        host = (parsed.hostname or "").lower().rstrip(".")
    except (TypeError, ValueError):
        return ""
    for platform_id, suffixes in ATC_PLATFORM_HOSTS.items():
        if any(host == suffix or host.endswith("." + suffix)
               for suffix in suffixes):
            return platform_id
    return ""


_RESERVED_WORK_HOSTS = frozenset({
    "localhost", "example.com", "example.net", "example.org",
})
_RESERVED_WORK_SUFFIXES = (
    ".localhost", ".local", ".lan", ".internal", ".test", ".example",
    ".invalid", ".onion",
)


def _atc_work_url_allowed(value: str) -> bool:
    """仅接受公开 HTTPS 作品链接，具体平台支持由主服务决定。"""
    try:
        parsed = urlparse.urlsplit(str(value or "").strip())
        if (parsed.scheme.lower() != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443)):
            return False
        host = (parsed.hostname or "").lower().rstrip(".")
        if (not host or "." not in host or host in _RESERVED_WORK_HOSTS
                or host.endswith(_RESERVED_WORK_SUFFIXES)):
            return False
        # Content platforms use hostnames. Rejecting IP literals also prevents
        # localhost/private-network URLs from being relayed to the extraction API.
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            return True
    except (TypeError, ValueError):
        return False


def _extract_supported_work_urls(text: str, limit: int = 50) -> list[str]:
    """从整段分享文案中安全提取受支持平台链接，保留平台必要查询参数。"""
    raw_text = str(text or "")[:50000]
    found, seen = [], set()
    for raw in re.findall(
            r"https://[^\s<>'\"。，、；！）】》\]}]+", raw_text, re.I):
        candidate = raw.rstrip("。，、；！）】》]}>,.!;)")
        if len(candidate) > 4096:
            continue
        try:
            parsed = urlparse.urlsplit(candidate)
            if (parsed.scheme.lower() != "https" or parsed.username
                    or parsed.password or parsed.port not in (None, 443)):
                continue
            host = (parsed.hostname or "").lower().rstrip(".")
            normalized = urlparse.urlunsplit((
                "https", host, parsed.path or "/", parsed.query, ""))
        except (TypeError, ValueError):
            continue
        if not _atc_work_url_allowed(normalized) or normalized in seen:
            continue
        seen.add(normalized)
        found.append(normalized)
        if len(found) >= max(1, min(100, int(limit or 1))):
            break
    return found


def _parse_share(text: str) -> dict:
    """所有平台优先主解析服务；抖音缺失信息由官方链路补全。"""
    links = _extract_supported_work_urls(text, 1)
    if not links:
        raise ApiError(400, "未找到受支持的平台链接，请粘贴公开作品分享链接")
    return _atc_parse_work_url(links[0])


def _parse_item(kind: str, item_id: str) -> dict:
    """按真实来源刷新：主服务优先，抖音缺失信息走官方补充。"""
    work_url = _saved_parse_source(item_id)
    numeric_item = bool(re.fullmatch(r"\d{8,30}", item_id or ""))
    # 有明确来源链接时以来源为准：其他平台也可能使用纯数字作品 ID，
    # 不能因为 ID 看起来像 aweme_id 就把它误送到抖音官方页。旧版本没有
    # 来源链接的数字快照才按抖音 aweme_id 兼容处理，且仍不会进入 ATC。
    if _is_douyin_work_url(work_url) or (numeric_item and not work_url):
        official_url = (work_url if _is_douyin_work_url(work_url)
                        else _douyin_work_url(kind, item_id))
        return _atc_parse_work_url(official_url, item_id_hint=item_id, kind_hint=kind)
    if not work_url:
        raise ApiError(404, "解析来源已过期，请重新粘贴原平台分享链接")
    return _atc_parse_work_url(work_url, item_id_hint=item_id, kind_hint=kind)


def _remember_parse_result(key: str, data: dict, now: float) -> dict:
    """成功后先持久化展示快照，再返回结果；网页、批量和开放 API 共用。"""
    data = dict(data)
    data["snapshot_at"] = int(now)
    detail = _snapshot_author(data)
    previous = _get_parse_snapshot(str(data.get("item_id") or "")) or {}
    if previous:
        try:
            data = _merge_metadata_snapshot(data, previous)
        except ApiError:
            pass
    previous_author = previous.get("author_detail")
    if isinstance(previous_author, dict):
        detail = _merge_missing_fields(detail, previous_author)
    if detail:
        data["author_detail"] = detail
    if data.get("kind") == "video" and re.fullmatch(r"[\w-]{8,40}", str(data.get("item_id") or "")):
        data.setdefault("video", {})["download_refresh_url"] = _video_download_refresh_url(data["item_id"])
    _save_parse_snapshot(data, source_text=key)
    _backfill_share_metadata(data)
    _cache_put(key, data, now)
    _cache_put(data["item_id"], data, now)
    return data


def _parse_cached(text: str) -> dict:
    key = text.strip()
    if not key:
        raise ApiError(400, "请粘贴公开作品分享链接")
    if len(key) > BATCH_TEXT_MAX:
        raise ApiError(413, "payload_too_large: 解析文本超过上限")
    now = time.time()
    hit = _cache_get(key)
    if hit and now - hit[0] < CACHE_TTL:
        cached_data = hit[1]
        cached_video = (cached_data.get("video")
                        if isinstance(cached_data, dict) else {})
        cached_source = str((cached_data or {}).get("source") or "").lower()
        cached_video_source = str((cached_video or {}).get("source") or "").lower()
        if (cached_source in ("atc", "parser") and cached_data.get("kind") != "note"
                and not _atc_url_fresh(_atc_cache_get(cached_data.get("item_id")),
                                       _atc_cfg()["url_ttl"])):
            hit = None
        else:
            direct_cached = (
                cached_source in ("douyin_direct", "douyin_web")
                or cached_video_source in ("douyin_direct", "douyin_web"))
            item_id = str(cached_data.get("item_id") or "")
            note = cached_data.get("kind") == "note"
            has_fresh_media = (
                bool(_douyin_cached_note_media(item_id)) if note
                else bool(_douyin_cached_media(item_id)))
            # 直连结果的短时媒体地址过期时，惰性刷新官方 detail。
            if direct_cached and not has_fresh_media:
                try:
                    refreshed = _atc_parse_work_url(
                        cached_data.get("_link") or _douyin_work_url(
                            cached_data.get("kind") or "video", item_id),
                        item_id_hint=item_id,
                        kind_hint=cached_data.get("kind") or "video",
                    )
                    return _remember_parse_result(key, refreshed, time.time())
                except Exception:
                    # 刷新失败时只返回元数据和可再刷新的同源端点，
                    # 不能回退为结果缓存里已过期的 CDN 签名 URL。
                    safe = json.loads(json.dumps(cached_data, ensure_ascii=False))
                    if note:
                        for index, image in enumerate(safe.get("images") or [], 1):
                            if not isinstance(image, dict):
                                continue
                            for field in ("url", "direct_url", "proxy_url",
                                          "download_url"):
                                image.pop(field, None)
                            filename = image.get("filename") or f"image_{index:02d}.jpeg"
                            if re.fullmatch(r"\d{8,30}", item_id):
                                image["proxy_url"] = _douyin_image_proxy_url(
                                    item_id, index, filename)
                                image["download_url"] = _douyin_image_proxy_url(
                                    item_id, index, filename, download=True)
                        safe["media_available"] = bool(safe.get("images"))
                    else:
                        safe_video = safe.setdefault("video", {})
                        for field in ("url", "direct_url", "atc_url"):
                            safe_video.pop(field, None)
                        safe_video["media_available"] = False
                        if re.fullmatch(r"\d{8,30}", item_id):
                            filename = safe_video.get("filename") or "video.mp4"
                            safe_video["proxy_url"] = _douyin_video_proxy_url(item_id)
                            safe_video["download_url"] = _douyin_video_download_url(
                                item_id, filename)
                    return safe
            return _retry_cached_metadata(key, cached_data)
    data = _parse_share(text)
    return _remember_parse_result(key, data, time.time())


# ---------------------------------------------------------------- 分享页
#
# 目标：抖音链接发到微信打不开 —— 生成一个「微信里点开就能看」的作品页。
# 原则（与全站一致）：只存净化后的元数据快照 + item_id，**不落地任何媒体字节**。
#   · 抖音视频：短时官方签名地址只在内存缓存中保存，过期后由同源 Range 端点按 item_id 刷新
#   · 兼容平台视频/图集：短时地址保存在服务端缓存，过期时由后台任务惰性刷新
#   · 源作品被删：刷新失败后按稳定错误状态展示，并保留必要署名

SHARE_TTL_ANON = int(os.environ.get("SHARE_TTL_ANON_DAYS", "7")) * 86400
SHARE_TTL_USER = int(os.environ.get("SHARE_TTL_USER_DAYS", "30")) * 86400
SHARE_MAX_PER_HOUR = 30                # 匿名创建限频（每 IP）
SHARE_PARSE_WORKERS = _clamped_env_int("SHARE_PARSE_WORKERS", 2, 1, 4)
SHARE_PARSE_LEASE_SECONDS = _clamped_env_int(
    "SHARE_PARSE_LEASE_SECONDS", 180, 60, 900)
SHARE_PARSE_HEARTBEAT_SECONDS = _clamped_env_int(
    "SHARE_PARSE_HEARTBEAT_SECONDS", 30, 10,
    max(10, SHARE_PARSE_LEASE_SECONDS // 2))
SHARE_PARSE_MAX_ATTEMPTS = _clamped_env_int(
    "SHARE_PARSE_MAX_ATTEMPTS", 4, 1, 8)
SHARE_PARSE_DEADLINE_SECONDS = _clamped_env_int(
    "SHARE_PARSE_DEADLINE_SECONDS", 900, 120, 3600)
SHARE_PARSE_QUEUE_MAX = _clamped_env_int(
    "SHARE_PARSE_QUEUE_MAX", 200, 10, 5000)
SHARE_PARSE_SHUTDOWN_TIMEOUT = _clamped_env_int(
    "SHARE_PARSE_SHUTDOWN_TIMEOUT", 35, 5, 120)
SHARE_PARSE_GLOBAL_PER_MINUTE = _clamped_env_int(
    "SHARE_PARSE_GLOBAL_PER_MINUTE", 120, 10, 10000)
SHARE_PARSE_GLOBAL_PER_HOUR = _clamped_env_int(
    "SHARE_PARSE_GLOBAL_PER_HOUR", 3000, 100, 100000)
SHARE_PARSE_IP_PER_HOUR = _clamped_env_int(
    "SHARE_PARSE_IP_PER_HOUR", 60, 10, 1000)
SHARE_FAILED_TTL = _clamped_env_int(
    "SHARE_FAILED_TTL", 86400, 3600, 7 * 86400)
SHARE_PARSE_ANON_INFLIGHT = _clamped_env_int(
    "SHARE_PARSE_ANON_INFLIGHT", 2, 1, 20)
SHARE_PARSE_USER_INFLIGHT = _clamped_env_int(
    "SHARE_PARSE_USER_INFLIGHT", 5, 1, 50)
SHARE_PARSE_IP_INFLIGHT = _clamped_env_int(
    "SHARE_PARSE_IP_INFLIGHT", 10, 2, 100)
_share_hits: dict = {}
_share_event_hits: dict = {}
_report_hits: dict = {}
SHARE_EVENT_MAX_PER_MIN = _clamped_env_int(
    "SHARE_EVENT_MAX_PER_MIN", 120, 10, 1000)
REPORT_MAX_PER_HOUR = _clamped_env_int(
    "REPORT_MAX_PER_HOUR", 5, 1, 100)

# ---- 分享域名池 ----
# 微信封"下载/侵权类"域名是常态而非意外，因此分享链接与主站域名物理隔离，并可轮换。
# 短码与域名解耦：同一个 sid 在任意域名下都能打开，某域名被封时切换即可救活存量分享。
# 配置：SHARE_DOMAINS="https://s1.example.com,https://s2.example.com"
def _normalize_origin_value(value: str) -> str:
    """规范化可信配置中的 origin；拒绝凭据、路径及非 HTTP(S) scheme。"""
    value = str(value or "").strip().rstrip("/")
    if not value:
        return ""
    try:
        parsed = urlparse.urlsplit(value)
        if (parsed.scheme.lower() not in ("http", "https")
                or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            return ""
        port = parsed.port
        hostname = parsed.hostname.lower().rstrip(".")
    except (TypeError, ValueError):
        return ""
    if (not hostname or any(c.isspace() or ord(c) < 32 for c in hostname)
            or len(hostname) > 253):
        return ""
    if ":" in hostname:
        if not re.fullmatch(r"[0-9a-f:.]+", hostname):
            return ""
    else:
        if not re.fullmatch(r"[a-z0-9.-]+", hostname):
            return ""
        labels = hostname.split(".")
        if any(not label or len(label) > 63 or label.startswith("-")
               or label.endswith("-") for label in labels):
            return ""
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    suffix = f":{port}" if port and port != default_port else ""
    return f"{parsed.scheme.lower()}://{rendered_host}{suffix}"


PUBLIC_ORIGIN = _normalize_origin_value(os.environ.get("PUBLIC_ORIGIN", ""))
SHARE_DOMAINS = [origin for origin in (
    _normalize_origin_value(d) for d in
    os.environ.get("SHARE_DOMAINS", "").split(",")
) if origin]
if (TRUST_PROXY and not PUBLIC_ORIGIN and not SHARE_DOMAINS
        and not _normalize_origin_value(app_setting("share_primary_domain", ""))):
    print("⚠️  反代模式尚未配置 PUBLIC_ORIGIN 或分享域名；返回的绝对链接可能只在内网可用。",
          file=sys.stderr)
_share_dom_rr = 0


def _domains_off() -> set:
    """被管理员标记为"已被封"的域名，暂时不再分配给新链接。"""
    try:
        return set(json.loads(app_setting("share_domains_off", "[]")))
    except Exception:
        return set()


def _share_origin(request: Request) -> str:
    """给**新生成的分享链接**分配域名。
    优先级：后台配置的主分享域名（app_settings.share_primary_domain）→ SHARE_DOMAINS
    域名池（轮换）→ 当前请求来源。主域名让所有新链接固定落在同一个"微信可打开"的域名上，
    无需改环境变量、后台即时生效；被标记封禁后自动退回域名池/请求来源。"""
    global _share_dom_rr
    primary = _normalize_origin_value(app_setting("share_primary_domain", ""))
    if primary and primary not in _domains_off():
        return primary
    pool = [d for d in SHARE_DOMAINS if d not in _domains_off()]
    if not pool:
        return PUBLIC_ORIGIN or _origin(request)
    _share_dom_rr = (_share_dom_rr + 1) % len(pool)
    return pool[_share_dom_rr]

# 短码字母表：去掉 0/O/1/l/I 等易混字符
_SID_ALPHABET = "23456789abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"


def _new_sid(n: int = 7) -> str:
    for _ in range(6):
        sid = "".join(secrets.choice(_SID_ALPHABET) for _ in range(n))
        if not db_exec("SELECT id FROM shares WHERE id=?", (sid,), "one"):
            return sid
    return "".join(secrets.choice(_SID_ALPHABET) for _ in range(n + 3))


def _is_wechat(request: Request) -> bool:
    return "micromessenger" in (request.headers.get("user-agent") or "").lower()


def _share_state(row: dict) -> str:
    """分享页公开状态；审核/过期优先于首次异步解析状态。"""
    if row["status"] in ("dead", "takedown"):
        return row["status"]
    if row["expires_at"] and row["expires_at"] < time.time():
        return "expired"
    parse_status = (row.get("parse_status") or "ready").lower()
    if parse_status in ("pending", "processing", "failed"):
        return parse_status
    return "ok"


_LEGACY_DIRECT_SOURCES = frozenset(("douyin_direct", "douyin_web"))


def _is_legacy_direct_data(data: dict) -> bool:
    """识别旧版本抖音快照，访问时按官方链路刷新签名媒体地址。"""
    if not isinstance(data, dict):
        return False
    video = data.get("video") if isinstance(data.get("video"), dict) else {}
    return (str(data.get("source") or "").lower() in _LEGACY_DIRECT_SOURCES
            or str(video.get("source") or "").lower() in _LEGACY_DIRECT_SOURCES)


# 保留旧内部名称，避免第三方扩展导入时直接崩溃；它只做数据识别，不会触发直取。
_is_douyin_direct_data = _is_legacy_direct_data


def _refresh_share(row: dict) -> dict:
    """临时媒体地址过期时按 item_id 重新走对应的官方/兼容解析链路。"""
    try:
        data = _parse_item(row["kind"], row["item_id"])
    except ApiError as exc:
        # 鉴权、网络或上游忙都是瞬时故障，不能把存量分享页误判为已删除。
        if exc.status not in (400, 404):
            return row
        now = int(time.time())
        with _db_lock:
            conn = _db()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE shares SET status='dead',refreshed_at=? "
                    "WHERE id=? AND status='ok' "
                    "AND COALESCE(parse_status,'ready')='ready'",
                    (now, row["id"]))
                latest = conn.execute(
                    "SELECT * FROM shares WHERE id=?", (row["id"],)).fetchone()
                conn.commit()
                return dict(latest) if latest else row
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
    except Exception:
        return row
    vid = ""
    if row["kind"] != "note":
        play_url = ((data.get("video") or {}).get("url") or "")
        match = re.search(r"[?&]video_id=([\w-]+)", play_url)
        if match:
            vid = match.group(1)
    data = _share_storage_payload(data)
    now = int(time.time())
    kind, item_id = _share_item_key(data)
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM shares WHERE id=?", (row["id"],)).fetchone()
            if not current:
                conn.rollback()
                return row
            if (current["status"] != "ok"
                    or (current["parse_status"] or "ready") != "ready"):
                conn.rollback()
                return dict(current)
            if item_id and conn.execute(
                    "SELECT 1 FROM blocked_share_items WHERE kind=? AND item_id=?",
                    (kind, item_id)).fetchone():
                conn.execute(
                    "UPDATE shares SET status='takedown',updated=? "
                    "WHERE id=? AND status='ok' "
                    "AND COALESCE(parse_status,'ready')='ready'",
                    (now, row["id"]))
            else:
                conn.execute(
                    "UPDATE shares SET payload=?,cover=?,vid=?,refreshed_at=?,updated=? "
                    "WHERE id=? AND status='ok' "
                    "AND COALESCE(parse_status,'ready')='ready'",
                    (json.dumps(_share_storage_payload(data), ensure_ascii=False), data.get("cover", ""),
                     vid, now, now, row["id"]))
            latest = conn.execute(
                "SELECT * FROM shares WHERE id=?", (row["id"],)).fetchone()
            conn.commit()
            return dict(latest) if latest else row
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _share_view(row: dict, origin: str = "") -> dict:
    """把 shares 行转成分享页要用的数据结构（含重拼后的播放地址）。"""
    data = json.loads(row["payload"] or "{}")
    state = _share_state(row)
    cfg = _atc_cfg() if state == "ok" else {
        "enabled": False, "play_enhance": False,
        "url_ttl": 0, "play_priority": ["atc", "proxy", "dy1", "dy2"]}
    is_note = row["kind"] == "note"
    video = data.setdefault("video", {}) if not is_note else None
    is_douyin_direct = bool(
        (not is_note and video and
         (video.get("source") in ("douyin_direct", "douyin_web")
          or data.get("source") in ("douyin_direct", "douyin_web")))
        or (is_note and data.get("source") in ("douyin_direct", "douyin_web")))
    is_atc = bool(video and (
        video.get("source") in ("atc", "parser") or data.get("source") in ("atc", "parser")))
    # 原先走官方兜底的快照，完成主地址续期后也复用持久媒体缓存。
    if (state == "ok" and is_douyin_direct and video
            and _atc_url_fresh(_atc_cache_get(row["item_id"]), cfg["url_ttl"])):
        data["source"] = video["source"] = "parser"
        is_atc, is_douyin_direct = True, False
    # v1.20 之前的分享快照可能把抖音数字作品保存成 ``douyin`` 或
    # ``atc``。只要能确认来源是抖音且 ID 是标准 aweme_id，就在内存中
    # 迁移到官方链路；这样存量链接不会再次进入第三方播放任务。
    legacy_source = str(
        data.get("source") or ((video or {}).get("source")) or "").lower()
    source_link = str(data.get("_link") or data.get("work_url") or "")
    legacy_douyin = (
        _is_douyin_work_url(source_link)
        or legacy_source in ("douyin", "douyin_direct", "douyin_web"))
    if (not is_douyin_direct and not is_atc and legacy_douyin
            and re.fullmatch(r"\d{8,30}", str(row["item_id"] or ""))):
        data["source"] = "douyin_direct"
        if video is not None:
            video["source"] = "douyin_direct"
        is_douyin_direct = True
        is_atc = False
    if state == "ok" and is_note:
        raw_images = data.get("images") if isinstance(data.get("images"), list) else []
        if is_douyin_direct and re.fullmatch(
                r"\d{8,30}", str(row["item_id"] or "")):
            cached_note = _douyin_cached_note_media(row["item_id"])
            direct_urls = list((cached_note or {}).get("urls") or [])
            count = max(len(raw_images), len(direct_urls))
            base = _safe_name(row.get("title") or data.get("title") or "",
                              row["item_id"])
            images = []
            for index in range(1, min(100, count) + 1):
                old = raw_images[index - 1] if index <= len(raw_images) else {}
                old = old if isinstance(old, dict) else {}
                filename = str(
                    old.get("filename") or f"{base}_{index:02d}.jpeg")[:180]
                image = {
                    "index": index,
                    "filename": filename,
                    "proxy_url": _douyin_image_proxy_url(
                        row["item_id"], index, filename),
                    "download_url": _douyin_image_proxy_url(
                        row["item_id"], index, filename, download=True),
                }
                if index <= len(direct_urls):
                    image["url"] = direct_urls[index - 1]
                images.append(image)
            data["images"] = images
            data["media_available"] = bool(images)
        else:
            # 兼容服务图集没有可按 item_id 安全刷新的服务端
            # 媒体缓存；新建分享已在入口拒绝，旧快照也不再下发
            # 已过期的签名地址。
            if legacy_source in ("atc", "parser"):
                for image in raw_images:
                    if isinstance(image, dict):
                        for field in ("url", "direct_url", "proxy_url",
                                      "download_url"):
                            image.pop(field, None)
                data["media_available"] = False
            else:
                data["media_available"] = any(
                    isinstance(image, dict)
                    and bool(_atc_public_url(image.get("url")))
                    for image in raw_images)
    elif state == "ok" and is_douyin_direct:
        filename = video.get("filename") or (
            _safe_name(row["title"] or "", row["item_id"]) + ".mp4")
        video["filename"] = filename
        cached = _douyin_cached_media(row["item_id"])
        direct = ((cached or {}).get("url") or video.get("direct_url")
                  or video.get("url") or "")
        # 没有内存缓存时，只继续下发刚刷新且仍有效的地址；过期地址交给
        # 同源端点按 item_id 惰性刷新，避免微信拿到必定 403 的签名 URL。
        fresh_payload = bool(
            direct and (cached or
                        (row.get("refreshed_at") and
                         time.time() - row["refreshed_at"] < DOUYIN_MEDIA_CACHE_TTL)))
        if direct and fresh_payload and _douyin_public_url(direct):
            video["url"] = direct
            video["direct_url"] = direct
            video["media_available"] = True
        else:
            for key in ("url", "direct_url"):
                video.pop(key, None)
            video["media_available"] = False
        # 同源端点只接受合法的数字作品 ID 和 HMAC；旧快照可能使用了
        # 短/非数字占位 ID，这类记录只能显示元数据，不能生成一个必然 400
        # 的播放或下载按钮。
        if re.fullmatch(r"\d{8,30}", str(row["item_id"] or "")):
            video["proxy_url"] = _douyin_video_proxy_url(row["item_id"])
            video["download_url"] = _douyin_video_download_url(
                row["item_id"], filename)
        else:
            video.pop("proxy_url", None)
            video.pop("download_url", None)
    elif state == "ok" and is_atc:
        filename = video.get("filename") or (
            _safe_name(row["title"] or "", row["item_id"]) + ".mp4")
        video["filename"] = filename
        cached = _atc_cache_get(row["item_id"])
        # Legacy snapshots may still contain a signed URL or a proxy token.
        # Remove those fields before deciding whether the cache is usable so an
        # expired/missing cache can never make the page advertise a dead link.
        for key in ("url", "direct_url", "atc_url", "proxy_url", "download_url",
                    "download_refresh_url"):
            video.pop(key, None)
        video["media_available"] = False
        if re.fullmatch(r"[\w-]{8,40}", row["item_id"]):
            video["download_refresh_url"] = _video_download_refresh_url(row["item_id"])
        if _atc_url_fresh(cached, cfg["url_ttl"]):
            direct = cached["video_url"]
            video["url"] = direct
            video["direct_url"] = direct
            if _primary_media_allowed(direct):
                video["proxy_url"] = _atc_video_proxy_url(row["item_id"])
                video["download_url"] = _atc_video_download_url(
                    row["item_id"], filename)
                video["media_available"] = True
        else:
            # 展示只读快照与已有媒体缓存，不能因访问/轮询创建解析任务。
            # 抖音签名地址在用户点击播放或下载后由同源媒体端点按需刷新。
            source_link = (data.get("_link") or data.get("work_url")
                           or row.get("source_url") or "")
            # 主媒体缓存缺失时，抖音仍可通过同源端点按真实来源进行补充。
            fallback_link = source_link or (cached or {}).get("work_url") or ""
            if (_is_douyin_work_url(fallback_link)
                    or (not fallback_link and re.fullmatch(r"\d{8,30}", row["item_id"]))):
                video["proxy_url"] = _atc_video_proxy_url(row["item_id"])
                video["download_url"] = _atc_video_download_url(row["item_id"], filename)
    elif state == "ok" and row["kind"] != "note" and row["vid"]:
        data.setdefault("video", {})
        data["video"]["url"] = _play_api(row["vid"])          # 每次重拼，保持新鲜
        data["video"]["alt_url"] = _play_api_alt(row["vid"])   # 备用抖音域名
        data["video"]["proxy_url"] = _video_proxy_url(row["vid"])
        data["video"]["source"] = "douyin"
        filename = data["video"].get("filename") or (
            _safe_name(row["title"] or "", row["item_id"]) + ".mp4")
        data["video"]["filename"] = filename
        data["video"]["download_url"] = _video_download_url(row["vid"], filename)
        # 旧分享页只附加已有的新鲜地址；访问本身不再入队。
        if cfg["enabled"] and cfg["play_enhance"]:
            cached = _atc_cache_get(row["item_id"])
            if _atc_url_fresh(cached, cfg["url_ttl"]):
                data["video"]["atc_url"] = cached["video_url"]
    # 官方签名地址由服务端出口生成，微信/访客优先走同源代理。
    if state == "ok":
        data["play_priority"] = (["proxy", "dy1", "dy2"]
                                  if is_douyin_direct else cfg["play_priority"])
        if video is not None and re.fullmatch(r"[\w-]{8,40}", str(row["item_id"] or "")):
            video["download_refresh_url"] = _video_download_refresh_url(row["item_id"])
    # 分享页只公开展示字段和服务端生成的相对端点。内部来源链接、兼容
    # 服务工作 URL 等可能含有追踪参数或凭据，不能随 payload 回传给访客。
    for private_key in ("_link", "work_url", "source_url"):
        data.pop(private_key, None)
    if isinstance(video, dict):
        for private_key in ("work_url", "source_url"):
            video.pop(private_key, None)
    view = {
        "sid": row["id"],
        "kind": row["kind"],
        "item_id": row["item_id"],
        "title": row["custom_title"] or data.get("title") or row["title"] or (
            "视频正在准备中" if state in ("pending", "processing") else "（无标题）"),
        "author": row["author"] or "",
        "avatar": row["avatar"] or "",
        "cover": row["cover"] or "",
        # 社交分享用的封面（无签名 JPEG、不过期）——JS-SDK 卡片图与海报都用它
        "card_cover": _card_cover(row["cover"] or ""),
        "created": row["created"],
        "snapshot_at": data.get("snapshot_at") or row.get("ready_at") or row["created"],
        "expires_at": row["expires_at"],
        "state": state,
        "ready": state == "ok",
        "shareable": state == "ok",
        "url": f"{origin}/s/{row['id']}" if origin else f"/s/{row['id']}",
        "views": row["views"], "plays": row["plays"], "downloads": row["downloads"],
        "data": data,
    }
    if is_note:
        media_available = bool(data.get("media_available") and data.get("images"))
    else:
        current_video = data.get("video") if isinstance(data.get("video"), dict) else {}
        media_available = bool(
            current_video.get("media_available")
            or current_video.get("url") or current_video.get("proxy_url"))
    media_pending = state in ("pending", "processing")
    view.update({
        "media_available": media_available,
        "media_pending": media_pending,
        "media_poll_after_ms": 2000 if media_pending else 0,
    })
    if state in ("pending", "processing"):
        view["poll_after_ms"] = _share_poll_after_ms(row, state)
    elif state == "failed":
        code = row.get("parse_error_code") or "parse_failed"
        messages = {
            "unsupported_link": "该链接指向的内容类型暂不支持。",
            "content_unavailable": "作品不存在、已删除，或已设为私密。",
            "parse_timeout": "内容获取超时，请稍后重新生成。",
            "upstream_unavailable": "抖音内容暂时无法获取，请稍后重试。",
        }
        view["error"] = {"code": code,
                         "message": messages.get(code, "内容获取失败，请稍后重试。")}
    return view


def _share_create(request: Request, data: dict, custom_title: str = "",
                  reservation: Optional[dict] = None) -> dict:
    if data.get("share_supported") is False:
        raise ApiError(400, "该平台暂不支持生成公开分享页")
    u = current_user(request)
    # 先在锁外确定有效期；current_user() 可能读取数据库，不能放进事务锁。
    share_ttl = SHARE_TTL_USER if u else SHARE_TTL_ANON
    sid = _new_sid()
    now = int(time.time())
    item_id = str(data.get("item_id") or "")
    kind = str(data.get("kind") or "video")
    reservation_id = (reservation or {}).get("id") or None
    stored_data = _share_storage_payload(data)
    stored_data.setdefault("snapshot_at", now)
    vid = ""
    if data.get("video", {}).get("url"):
        vm = re.search(r"video_id=([\w-]+)", data["video"]["url"])
        vid = vm.group(1) if vm else ""
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if item_id and conn.execute(
                    "SELECT 1 FROM blocked_share_items WHERE kind=? AND item_id=?",
                    (kind, item_id)).fetchone():
                raise ApiError(451, "content_blocked: 该作品已被下架")
            conn.execute(
                "INSERT INTO shares(id,item_id,kind,vid,owner_user_id,owner_fp,owner_ip,"
                "title,author,avatar,cover,payload,custom_title,visibility,expires_at,"
                "refreshed_at,status,created,quota_reservation_id,source_url) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, item_id, kind, vid, u["id"] if u else None, "", "",
                 (data.get("title") or "")[:300], (data.get("author") or "")[:100],
                 data.get("avatar", ""), data.get("cover", ""),
                 json.dumps(stored_data, ensure_ascii=False), (custom_title or "")[:300],
                 "link", now + share_ttl, now, "ok", now,
                 reservation_id, _saved_source_in_conn(conn, item_id)
                 or _normalize_parse_source(data.get("_link") or data.get("original_url") or "")))
            if reservation_id:
                committed = _settle_quota_in_conn(conn, reservation_id, 1)
                if committed != 1:
                    raise RuntimeError("share quota reservation is not pending")
            row = dict(conn.execute(
                "SELECT * FROM shares WHERE id=?", (sid,)).fetchone())
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return _share_view(row, _share_origin(request))      # 新链接按域名池分配


def _share_event(request: Request, sid: str, kind: str, source: str = "",
                 stage: str = "", detail: str = "", ms: int = 0,
                 next_src: str = ""):
    """记录分享页埋点。播放类事件额外带 source/stage/detail/ms/next_src，用于诊断
    「微信里哪些视频能播、走的哪条线路、失败在哪一步、失败后接着重试哪条」。
    注意：只记线路名，不记带签名的完整媒体地址（隐私红线）。"""
    if (not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", str(sid or ""))
            or kind not in SHARE_EVENT_KINDS):
        return "missing"
    ip = _client_ip(request)
    if not _rate_ok(
            _share_event_hits, ip, 60, SHARE_EVENT_MAX_PER_MIN):
        return "limited"
    now = int(time.time())
    col = {"view": "views", "play": "plays",
           "download": "downloads", "cta": "cta_clicks"}.get(kind)
    # 同 IP/链接/类型在一分钟内只记一次；诊断事件额外带线路和阶段，
    # 保留真实的 fallback 链，同时防止简单重放污染统计。
    discriminator = (f"{source[:24]}:{stage[:24]}:{next_src[:24]}"
                     if kind.startswith("play_") or kind == "fallback" else "")
    event_key = hmac.new(
        APP_SECRET,
        f"share-event:v1:{ip}:{sid}:{kind}:{discriminator}:{now // 60}".encode(),
        hashlib.sha256).hexdigest()
    try:
        with _db_lock:
            conn = _db()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT status,expires_at FROM shares WHERE id=?", (sid,)).fetchone()
                if (not row or row["status"] in ("dead", "takedown")
                        or (row["expires_at"] and int(row["expires_at"]) <= now)):
                    conn.rollback()
                    return "missing"
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO share_events("
                    "ts,sid,kind,ip,ua,referer,wechat,fp,source,stage,detail,ms,"
                    "next_src,event_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now, sid, kind, "", _coarse_ua(request), "",
                     1 if _is_wechat(request) else 0, "", source[:24], stage[:24],
                     detail[:120], max(0, min(300000, int(ms or 0))),
                     next_src[:24], event_key)).rowcount
                if inserted and col:
                    conn.execute(f"UPDATE shares SET {col}={col}+1 WHERE id=?", (sid,))
                conn.commit()
                return "inserted" if inserted else "duplicate"
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
    except Exception:
        return "error"


_SHARE_SHORT_PATH = re.compile(r"/[A-Za-z0-9_-]{3,64}/?")
_SHARE_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def _normalize_share_short_link(text: str) -> str:
    """只做本地语法校验；不跟随短链、不产生任何出站请求。"""
    raw_text = str(text or "").strip()
    if not raw_text:
        raise ApiError(422, "invalid_douyin_link: 请提供抖音或 TikTok 分享链接")
    if len(raw_text.encode("utf-8")) > 4096:
        raise ApiError(413, "payload_too_large: 分享文案最多 4KB")
    urls = re.findall(r"https?://[^\s<>'\"]+", raw_text, re.I)
    valid = []
    saw_douyin = False
    for raw in urls:
        # 只去掉普通文案会紧跟在 URL 后的句末符号；不去 ?/#，避免接受 query/fragment。
        candidate = raw.rstrip("。，、；！）】》]}!,;)")
        try:
            parsed = urlparse.urlsplit(candidate)
            host = (parsed.hostname or "").lower().rstrip(".")
            if host in ("www.tiktok.com", "tiktok.com", "vm.tiktok.com", "vt.tiktok.com"):
                if (parsed.scheme == "https" and not parsed.username and not parsed.password
                        and parsed.port is None and not parsed.fragment
                        and (re.fullmatch(r"/@[^/\s]+/video/\d{8,30}/?", parsed.path)
                             or (host in ("vm.tiktok.com", "vt.tiktok.com")
                                 and re.fullmatch(r"/[\w-]{3,100}/?", parsed.path)))):
                    valid.append(urlparse.urlunsplit(("https", host, parsed.path, parsed.query, "")))
                continue
            if host == "v.douyin.com":
                saw_douyin = True
            if (parsed.scheme != "https" or host != "v.douyin.com"
                    or parsed.username or parsed.password or parsed.port is not None
                    or parsed.query or parsed.fragment
                    or not _SHARE_SHORT_PATH.fullmatch(parsed.path or "")):
                continue
            token = parsed.path.strip("/")
            valid.append(f"https://v.douyin.com/{token}/")
        except (TypeError, ValueError):
            continue
    if len(valid) > 1:
        raise ApiError(422, "multiple_links: 每次只能提交一个视频分享链接")
    if not valid:
        detail = "请粘贴有效的抖音短链或 TikTok 视频分享链接"
        if not saw_douyin:
            detail = "未找到抖音短链或 TikTok 视频分享链接"
        raise ApiError(422, f"invalid_douyin_link: {detail}")
    return valid[0]


def _share_owner_scope(request: Request, user: Optional[dict] = None) -> str:
    user = user if user is not None else current_user(request)
    if user:
        return f"user:{user['id']}"
    # 安全限频必须至少固定到来源 IP；若把可伪造的 X-FP 混入主键，攻击者只需轮换
    # 指纹就能不断获得新的在途桶并塞满队列。数据库中只落用途隔离 HMAC。
    return _privacy_hash("async-share-owner", _client_ip(request))


def _share_request_hash(source_url: str, title: str) -> str:
    return _privacy_hash("async-share-request", f"{source_url}\n{title}")


def _share_source_hash(source_url: str) -> str:
    return _privacy_hash("async-share-source", source_url)


def _share_item_key(data: dict) -> tuple[str, str]:
    return (str(data.get("kind") or "video"), str(data.get("item_id") or ""))


def _snapshot_author(data: dict) -> dict:
    """只取已获取的公开作者字段，不在保存/展示路径请求作者接口。"""
    detail = data.get("author_detail")
    detail = dict(detail) if isinstance(detail, dict) else {}
    cached = (_author_cache.get(str(data.get("item_id") or "")) or (0, {}))[1]
    detail = _merge_missing_fields(detail, cached)
    fields = ("nickname", "author", "avatar", "author_url", "unique_id",
              "short_id", "douyin_id", "signature", "follower_count",
              "total_favorited", "following_count", "aweme_count")
    return {key: detail[key] for key in fields
            if detail.get(key) is not None and detail[key] != ""}


_EMPTY_TITLES = ("", "（无标题）", "(无标题)", "暂无标题", "无标题")


def _metadata_missing(data: dict) -> list[str]:
    """核心展示字段的完整性；合法的 0 不视为空，不以视频可播放代替信息完整。"""
    missing = []
    if str(data.get("title") or "").strip() in _EMPTY_TITLES:
        missing.append("title")
    if str(data.get("author") or "").strip() in ("", "未知作者"):
        missing.append("author")
    stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    for key in ("digg", "comment", "share", "collect"):
        value = stats.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            missing.append(key)
    return missing


def _merge_metadata_snapshot(old: dict, fresh: dict) -> dict:
    """只补同一作品的展示信息，不替换媒体线路、有效计数或用户自定义标题。"""
    if (not old.get("item_id") or old.get("item_id") != fresh.get("item_id")
            or old.get("kind") != fresh.get("kind")
            or (old.get("platform") and fresh.get("platform")
                and old["platform"] != fresh["platform"])):
        raise ApiError(409, "作品身份不一致，已停止信息补齐")
    data = json.loads(json.dumps(old, ensure_ascii=False))
    for key in ("title", "author", "avatar", "author_url", "create_time",
                "stats", "tags", "music", "location", "author_detail"):
        if key not in fresh:
            continue
        if key == "author" and data.get(key) == "未知作者":
            data[key] = ""
        data[key] = _merge_missing_fields({key: data.get(key)}, {key: fresh[key]})[key]
    if not data.get("duration_ms") and fresh.get("duration_ms"):
        data["duration_ms"] = fresh["duration_ms"]
    video = data.get("video")
    if isinstance(video, dict):
        for key in ("width", "height"):
            value = (fresh.get("video") or {}).get(key)
            if not video.get(key) and value:
                video[key] = value
        if video.get("filename") in tuple(title + ".mp4" for title in _EMPTY_TITLES):
            if str(data.get("title") or "") not in _EMPTY_TITLES:
                video["filename"] = _safe_name(data["title"], data["item_id"]) + ".mp4"
    if str(data.get("base") or "") in _EMPTY_TITLES and data.get("title"):
        data["base"] = _safe_name(data["title"], data["item_id"])
    if data != old:
        data["snapshot_at"] = int(fresh.get("snapshot_at") or time.time())
    return data


def _backfill_share_metadata(data: dict, sid: str = "") -> int:
    """在解析/管理员修复时补旧快照；分享页 GET 永远不触发。"""
    item_id = str(data.get("item_id") or "")
    if not item_id:
        return 0
    fresh = _share_storage_payload(data)
    updated = 0
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id,title,author,avatar,cover,payload FROM shares WHERE item_id=? "
                "AND status='ok' AND COALESCE(parse_status,'ready')='ready' "
                "AND (expires_at=0 OR expires_at>?) "
                + ("AND id=? " if sid else "") + "ORDER BY created DESC LIMIT 500",
                (item_id, int(time.time()), sid) if sid else (item_id, int(time.time()))).fetchall()
            for row in rows:
                try:
                    old = json.loads(row["payload"] or "{}")
                    merged = _merge_metadata_snapshot(old, fresh)
                except (ValueError, TypeError, AttributeError, ApiError):
                    continue
                # 顶层字段也可能来自旧占位符；custom_title / expires_at 等保持原状。
                top = _merge_missing_fields(
                    {k: row[k] for k in ("title", "author", "avatar", "cover")}, merged)
                if merged == old and all(top[k] == row[k] for k in ("title", "author", "avatar", "cover")):
                    continue
                conn.execute("UPDATE shares SET title=?,author=?,avatar=?,cover=?,payload=? WHERE id=?",
                             (top["title"], top["author"], top["avatar"], top["cover"],
                              json.dumps(merged, ensure_ascii=False), row["id"]))
                updated += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    # 保留每个缓存原来的媒体和过期时间，让不同输入别名也能看到已补齐的字段。
    with _cache_lock:
        for key, (ts, cached) in list(_cache.items()):
            if cached.get("item_id") == item_id:
                try:
                    _cache[key] = (ts, _merge_metadata_snapshot(cached, data))
                except ApiError:
                    pass
    return updated


def _retry_cached_metadata(key: str, data: dict) -> dict:
    """缺信息的抖音缓存按作品限流重试，只补官方元数据，不重复提交主解析。"""
    if not _metadata_missing(data) or data.get("source") not in ("atc", "parser"):
        return data
    item_id = str(data.get("item_id") or "")
    work_url = data.get("_link") or (_atc_cache_get(item_id) or {}).get("work_url") or ""
    if not item_id or not _is_douyin_work_url(work_url):
        return data
    now = time.monotonic()
    with _metadata_retry_lock:
        next_at, busy = _metadata_retries.get(item_id, (0, False))
        if busy or now < next_at:
            return data
        _metadata_retries[item_id] = (now + METADATA_RETRY_SECONDS, True)
    try:
        refreshed = _complete_douyin_result(work_url, json.loads(json.dumps(data)))
        return _remember_parse_result(key, refreshed, time.time())
    except Exception:
        return data
    finally:
        with _metadata_retry_lock:
            _metadata_retries[item_id] = (time.monotonic() + METADATA_RETRY_SECONDS, False)


def _metadata_attempt(item_id: str) -> None:
    with _metadata_retry_lock:
        busy = _metadata_retries.get(item_id, (0, False))[1]
        _metadata_retries[item_id] = (time.monotonic() + METADATA_RETRY_SECONDS, busy)
        if len(_metadata_retries) > 500:
            for key, (_, active) in list(_metadata_retries.items()):
                if not active and key != item_id:
                    _metadata_retries.pop(key, None)
                    if len(_metadata_retries) <= 500:
                        break


def _record_metadata_failure(stage: str) -> None:
    # 只记录有限分类，不保存上游原文、作品链接、代理地址或凭据。
    code = "metadata_unavailable"
    if proxy_mgr.force_proxy and not proxy_mgr.candidates():
        code = "proxy_required"
    elif stage == "metadata" and not _douyin_browser_binary():
        code = "browser_missing"
    elif stage == "metadata" and not _douyin_browser_enabled():
        code = "browser_disabled"
    with _metadata_retry_lock:
        _metadata_last_failure.update(at=int(time.time()), stage=stage, code=code)


def _normalize_parse_source(text: str) -> str:
    """只保留可重解析的公开作品链接，不保存分享文案或无关抖音追踪参数。"""
    links = _extract_supported_work_urls(text, 1)
    if not links:
        return ""
    link = links[0]
    kind, item_id = _douyin_item_from_url(link)
    if item_id:
        return _douyin_work_url(kind, item_id)
    parsed = urlparse.urlsplit(link)
    if (parsed.hostname == "v.douyin.com"
            or (parsed.hostname or "").endswith("tiktok.com")):
        return urlparse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return link


def _saved_source_in_conn(conn, item_id: str) -> str:
    """媒体缓存清理或进程重启后，从有效快照/分享恢复来源；只读数据库。"""
    now = int(time.time())
    row = conn.execute(
        "SELECT canonical_url,source_url FROM parse_snapshots WHERE item_id=? AND expires_at>?",
        (item_id, now)).fetchone()
    candidates = [row["canonical_url"], row["source_url"]] if row else []
    rows = conn.execute(
        "SELECT source_url FROM shares WHERE item_id=? AND status='ok' "
        "AND COALESCE(parse_status,'ready')='ready' AND (expires_at=0 OR expires_at>?) "
        "AND source_url IS NOT NULL ORDER BY created DESC LIMIT 20", (item_id, now)).fetchall()
    candidates.extend(row["source_url"] for row in rows)
    cached = conn.execute("SELECT work_url FROM atc_cache WHERE item_id=?", (item_id,)).fetchone()
    if cached:
        candidates.append(cached["work_url"])
    return next((link for value in candidates if (link := _normalize_parse_source(value))), "")


def _saved_parse_source(item_id: str) -> str:
    with _db_lock:
        conn = _db()
        try:
            return _saved_source_in_conn(conn, item_id)
        finally:
            conn.close()


def _save_parse_snapshot(data: dict, source_text: str = "") -> None:
    item_id = str(data.get("item_id") or "")
    if not item_id:
        return
    now = int(time.time())
    stored = _share_storage_payload(data)
    stored.setdefault("snapshot_at", now)
    db_exec(
        "INSERT INTO parse_snapshots(item_id,payload,created,expires_at,source_url,canonical_url) "
        "VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(item_id) DO UPDATE SET payload=excluded.payload,"
        "created=excluded.created,expires_at=excluded.expires_at,"
        "source_url=COALESCE(NULLIF(excluded.source_url,''),parse_snapshots.source_url),"
        "canonical_url=COALESCE(NULLIF(excluded.canonical_url,''),parse_snapshots.canonical_url)",
        (item_id, json.dumps(stored, ensure_ascii=False), now, now + PARSE_SNAPSHOT_TTL,
         _normalize_parse_source(source_text),
         _normalize_parse_source(data.get("_link") or data.get("original_url") or "")))


def _get_parse_snapshot(item_id: str) -> Optional[dict]:
    row = db_exec("SELECT payload FROM parse_snapshots WHERE item_id=? AND expires_at>?",
                  (item_id, int(time.time())), "one")
    if not row:
        return None
    try:
        data = json.loads(row["payload"])
        return data if isinstance(data, dict) and data.get("item_id") == item_id else None
    except (TypeError, ValueError):
        return None


def _save_author_snapshot(item_id: str, detail: dict) -> None:
    """作者详情获取成功后补入已保存的快照；不延长保留期，不覆盖已有计数。"""
    public = _snapshot_author({"item_id": item_id, "author_detail": detail})
    if not public:
        return
    now = int(time.time())
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for table, key, condition in (
                ("parse_snapshots", "item_id", "expires_at>?"),
                ("shares", "id", "status='ok' AND COALESCE(parse_status,'ready')='ready' "
                 "AND (expires_at=0 OR expires_at>?)"),
            ):
                rows = conn.execute(
                    f"SELECT {key},payload FROM {table} WHERE item_id=? AND {condition}",
                    (item_id, now)).fetchall()
                for row in rows:
                    data = json.loads(row["payload"] or "{}")
                    old = data.get("author_detail")
                    data["author_detail"] = _merge_missing_fields(
                        old if isinstance(old, dict) else {}, public)
                    conn.execute(f"UPDATE {table} SET payload=? WHERE {key}=?",
                                 (json.dumps(data, ensure_ascii=False), row[key]))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _share_storage_payload(data: dict) -> dict:
    """保存分享快照前移除短时媒体地址。

    解析链路返回的媒体地址可能过期；分享页只保留元数据和作品 ID，访问时
    按抖音官方内存缓存或兼容平台缓存重新注入地址，避免数据库泄露签名。
    """
    try:
        stored = json.loads(json.dumps(data or {}, ensure_ascii=False))
    except (TypeError, ValueError):
        stored = dict(data or {}) if isinstance(data, dict) else {}
    detail = _snapshot_author(stored)
    if detail:
        stored["author_detail"] = detail
    for key in ("_link", "work_url", "source_url"):
        stored.pop(key, None)
    video = stored.get("video")
    source = str(
        stored.get("source")
        or (video.get("source") if isinstance(video, dict) else "") or "").lower()
    if source in _LEGACY_DIRECT_SOURCES or source in ("atc", "parser"):
        images = stored.get("images")
        if isinstance(images, list):
            for index, image in enumerate(images, 1):
                if not isinstance(image, dict):
                    continue
                for key in ("url", "proxy_url", "download_url", "direct_url"):
                    image.pop(key, None)
                image["index"] = index
        stored["media_available"] = False
    if not isinstance(video, dict):
        return stored
    for key in ("work_url", "source_url"):
        video.pop(key, None)
    if source in _LEGACY_DIRECT_SOURCES or source in ("atc", "parser"):
        for key in ("url", "atc_url", "direct_url", "proxy_url",
                    "download_url", "download_refresh_url", "alt_url"):
            video.pop(key, None)
        video["media_available"] = False
    return stored


def _require_share_item_allowed(data: dict) -> None:
    kind, item_id = _share_item_key(data)
    if not item_id:
        return
    row = db_exec(
        "SELECT 1 FROM blocked_share_items WHERE kind=? AND item_id=?",
        (kind, item_id), "one")
    if row:
        raise ApiError(451, "content_blocked: 该作品已被下架")


def _share_manage_token(sid: str, owner_scope: str) -> str:
    payload = f"share-manage:v1:{sid}:{owner_scope}".encode()
    return "sm1_" + hmac.new(APP_SECRET, payload, hashlib.sha256).hexdigest()


def _valid_share_manage_token(row: dict, token: str) -> bool:
    if row.get("owner_user_id") is not None or not token:
        return False
    expected = _share_manage_token(row["id"], row.get("owner_scope") or "")
    return hmac.compare_digest(expected, str(token).strip())


def _share_poll_after_ms(row: dict, state: str) -> int:
    if state not in ("pending", "processing"):
        return 0
    base = 1500 if state == "pending" else 2000
    retry_at = int(row.get("next_attempt_at") or 0)
    if state == "pending" and retry_at > int(time.time()):
        base = max(base, (retry_at - int(time.time())) * 1000)
    return max(800, min(10000, base))


def _share_async_payload(row: dict, origin: str) -> dict:
    state = _share_state(row)
    public_status = "ready" if state == "ok" else state
    assigned_origin = _normalize_origin_value(row.get("assigned_origin") or "") or origin
    share_url = f"{assigned_origin}/s/{row['id']}"
    data = {
        "sid": row["id"], "status": public_status,
        "ready": state == "ok", "shareable": state == "ok",
        "share_url": share_url,
        "status_url": f"/api/shares/{row['id']}",
        "expires_at": row["expires_at"],
        "poll_after_ms": _share_poll_after_ms(row, state),
    }
    if state == "ok":
        view = _share_view(row, origin)
        data.update({
            "kind": row["kind"], "item_id": row["item_id"],
            "title": row["custom_title"] or row["title"] or "（无标题）",
            "author": row["author"] or "", "cover": row["cover"] or "",
            "media_available": bool(view.get("media_available")),
            "media_pending": bool(view.get("media_pending")),
            "media_poll_after_ms": int(view.get("media_poll_after_ms") or 0),
            # 微信若曾抓取过 pending OG，完成后复制带版本的 URL 可降低命中旧卡片的概率。
            "share_url_versioned": f"{share_url}?v={int(row.get('ready_at') or 1)}",
        })
    elif state == "failed":
        view = _share_view(row, origin)
        data["error"] = view.get("error") or {
            "code": "parse_failed", "message": "内容获取失败，请稍后重试。"}
    return data


class ShareBody(BaseModel):
    text: str = Field(default="", max_length=PARSE_TEXT_MAX)
    item_id: str = Field(default="", max_length=64)
    title: str = Field(default="", max_length=300)


class AsyncShareBody(BaseModel):
    text: str = Field(default="", max_length=4096)
    title: str = Field(default="", max_length=300)

    class Config:
        extra = "forbid"


@app.post("/api/shares", status_code=202)
def api_share_create_async(body: AsyncShareBody, request: Request):
    """只校验并持久化任务，立即返回分享地址；抖音内容由后台 worker 获取。"""
    source_url = _normalize_share_short_link(body.text)
    custom_title = (body.title or "").strip()
    if len(custom_title) > 300:
        raise ApiError(422, "invalid_title: 自定义标题最多 300 个字符")
    idem_key = (request.headers.get("Idempotency-Key") or "").strip()
    if idem_key and not _SHARE_IDEMPOTENCY_KEY.fullmatch(idem_key):
        raise ApiError(
            422, "invalid_idempotency_key: 仅允许 1-128 位字母、数字、.-_:")

    user = current_user(request)
    owner_scope = _share_owner_scope(request, user)
    ip_scope = _privacy_hash("async-share-rate-ip", _client_ip(request))
    source_hash = _share_source_hash(source_url)
    assigned_origin = _share_origin(request)
    request_hash = _share_request_hash(source_url, custom_title)
    idem_hash = (_privacy_hash(
        "async-share-idempotency", f"{owner_scope}:{idem_key}") if idem_key else None)
    subjects, limit = _quota_subjects(request)
    sid = _new_sid()
    now = int(time.time())
    ttl = SHARE_TTL_USER if user else SHARE_TTL_ANON
    replay = False

    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = None
            if idem_hash:
                row = conn.execute(
                    "SELECT * FROM shares WHERE owner_scope=? AND idem_key_hash=?",
                    (owner_scope, idem_hash)).fetchone()
                if row and row["request_hash"] != request_hash:
                    raise ApiError(
                        409, "idempotency_conflict: 同一 Idempotency-Key 不能提交不同链接或标题")
                replay = bool(row)

            if not row:
                if conn.execute(
                        "SELECT 1 FROM blocked_share_sources WHERE source_hash=?",
                        (source_hash,)).fetchone():
                    raise ApiError(451, "content_blocked: 该作品已被下架")
                recent = conn.execute(
                    "SELECT COUNT(*) n FROM share_submissions "
                    "WHERE owner_scope=? AND ts>=?",
                    (owner_scope, now - 3600)).fetchone()["n"]
                if int(recent or 0) >= SHARE_MAX_PER_HOUR:
                    raise ApiError(
                        429, "rate_limited: 创建分享页过于频繁，请稍后再试",
                        {"Retry-After": "60"})
                ip_recent = conn.execute(
                    "SELECT COUNT(*) n FROM share_submissions WHERE ip_scope=? AND ts>=?",
                    (ip_scope, now - 3600)).fetchone()["n"]
                if int(ip_recent or 0) >= SHARE_PARSE_IP_PER_HOUR:
                    raise ApiError(
                        429, "rate_limited: 当前网络创建过于频繁，请稍后再试",
                        {"Retry-After": "60"})
                global_minute = conn.execute(
                    "SELECT COUNT(*) n FROM share_submissions WHERE ts>=?",
                    (now - 60,)).fetchone()["n"]
                global_hour = conn.execute(
                    "SELECT COUNT(*) n FROM share_submissions WHERE ts>=?",
                    (now - 3600,)).fetchone()["n"]
                if (int(global_minute or 0) >= SHARE_PARSE_GLOBAL_PER_MINUTE
                        or int(global_hour or 0) >= SHARE_PARSE_GLOBAL_PER_HOUR):
                    raise ApiError(
                        503, "queue_busy: 当前提交较多，请稍后再试",
                        {"Retry-After": "10"})
                backlog = conn.execute(
                    "SELECT COUNT(*) n FROM shares "
                    "WHERE parse_status IN ('pending','processing')").fetchone()["n"]
                if int(backlog or 0) >= SHARE_PARSE_QUEUE_MAX:
                    raise ApiError(
                        503, "queue_full: 当前任务较多，请稍后再试",
                        {"Retry-After": "10"})
                in_flight = conn.execute(
                    "SELECT COUNT(*) n FROM shares WHERE owner_scope=? "
                    "AND parse_status IN ('pending','processing')",
                    (owner_scope,)).fetchone()["n"]
                actor_limit = (SHARE_PARSE_USER_INFLIGHT if user
                               else SHARE_PARSE_ANON_INFLIGHT)
                if int(in_flight or 0) >= actor_limit:
                    raise ApiError(
                        429, "too_many_inflight: 请等待当前分享页准备完成",
                        {"Retry-After": "3"})
                ip_in_flight = conn.execute(
                    "SELECT COUNT(*) n FROM shares s JOIN share_submissions sub "
                    "ON sub.sid=s.id WHERE sub.ip_scope=? "
                    "AND s.parse_status IN ('pending','processing')",
                    (ip_scope,)).fetchone()["n"]
                if int(ip_in_flight or 0) >= SHARE_PARSE_IP_INFLIGHT:
                    raise ApiError(
                        429, "too_many_inflight: 当前网络待处理任务过多",
                        {"Retry-After": "3"})

                reservation = _reserve_quota_in_conn(
                    conn, _today(), subjects, limit, 1, False,
                    "share_parse_async")
                if not reservation["ok"]:
                    raise _quota_error(limit, reservation)
                conn.execute(
                    "INSERT INTO shares("
                    "id,item_id,kind,vid,owner_user_id,owner_fp,owner_ip,"
                    "title,author,avatar,cover,payload,custom_title,visibility,"
                    "expires_at,refreshed_at,status,created,parse_status,source_url,"
                    "parse_error_code,attempts,next_attempt_at,lease_owner,lease_until,"
                    "quota_reservation_id,owner_scope,idem_key_hash,request_hash,"
                    "source_hash,assigned_origin,updated,ready_at"
                    ") VALUES("
                    ":id,'','','',:owner_user_id,'','',"
                    "'','','','','{}',:custom_title,'link',"
                    ":expires_at,NULL,'ok',:created,'pending',:source_url,"
                    "NULL,0,:next_attempt_at,NULL,NULL,"
                    ":quota_reservation_id,:owner_scope,:idem_key_hash,:request_hash,"
                    ":source_hash,:assigned_origin,:updated,NULL)",
                    {"id": sid, "owner_user_id": user["id"] if user else None,
                     "custom_title": custom_title, "expires_at": now + ttl,
                     "created": now, "source_url": source_url,
                     "next_attempt_at": now,
                     "quota_reservation_id": reservation["id"],
                     "owner_scope": owner_scope, "idem_key_hash": idem_hash,
                     "request_hash": request_hash, "source_hash": source_hash,
                     "assigned_origin": assigned_origin, "updated": now})
                conn.execute(
                    "INSERT INTO share_submissions(sid,ts,owner_scope,ip_scope,user_id) "
                    "VALUES(?,?,?,?,?)",
                    (sid, now, owner_scope, ip_scope,
                     user["id"] if user else None))
                row = conn.execute(
                    "SELECT * FROM shares WHERE id=?", (sid,)).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    _wake_share_parse_workers()
    row_dict = dict(row)
    data = _share_async_payload(row_dict, _origin(request))
    if not user:
        # 管理凭证只随创建/幂等重放返回，绝不进入公开状态接口、URL 或页面。
        data["manage_token"] = _share_manage_token(
            row_dict["id"], row_dict.get("owner_scope") or "")
    response_status = 202 if data["status"] in ("pending", "processing") else 200
    return JSONResponse(
        status_code=response_status,
        content={"ok": True, "replayed": replay, "data": data},
        headers={"Location": f"/s/{row_dict['id']}",
                 "Retry-After": "2" if response_status == 202 else "0"})


@app.post("/api/share")
def api_share_create(body: ShareBody, request: Request):
    """生成分享页。已解析过的作品命中缓存 → 不重复解析、不再扣配额。"""
    if not _rate_ok(_share_hits, _client_ip(request), 3600, SHARE_MAX_PER_HOUR):
        raise ApiError(429, "创建分享页过于频繁，请稍后再试")

    data = None
    reservation = None
    if body.item_id:
        hit = _cache_get(body.item_id.strip())
        if hit and time.time() - hit[0] < CACHE_TTL:
            data = hit[1]
        else:
            data = _get_parse_snapshot(body.item_id.strip())
        if data is None:
            # 内存和临时快照已过期：只复用仍在有效期内的已完成分享。
            row = db_exec("SELECT * FROM shares WHERE item_id=? AND status='ok' "
                          "AND COALESCE(parse_status,'ready')='ready' "
                          "AND (expires_at=0 OR expires_at>?) "
                          "ORDER BY created DESC LIMIT 1",
                          (body.item_id.strip(), int(time.time())), "one")
            if row:
                data = json.loads(dict(row)["payload"] or "{}")
    if data is None:
        if not body.text.strip():
            raise ApiError(400, "解析结果已过期，请重新粘贴链接后再生成分享页")
        reservation = reserve_quota(request, 1, endpoint="share_parse")
        if not reservation["ok"]:
            raise _quota_error(reservation["limit"], reservation)
        try:
            data = _parse_cached(body.text)
        except Exception:
            release_quota(reservation)
            raise
    if not data.get("item_id"):
        release_quota(reservation)
        raise ApiError(400, "解析数据不完整，无法生成分享页")
    try:
        _require_share_item_allowed(data)
    except Exception:
        release_quota(reservation)
        raise
    try:
        return _share_create(request, data, body.title, reservation)
    except Exception:
        # 原子创建事务若因下架竞态或数据库故障回滚，预占仍在，需显式归还。
        release_quota(reservation)
        raise


@app.get("/api/share/{sid}")
def api_share_get(sid: str, request: Request):
    row = db_exec("SELECT * FROM shares WHERE id=?", (sid,), "one")
    if not row:
        raise ApiError(404, "分享页不存在或已被删除")
    return _share_view(dict(row), _origin(request))


@app.get("/api/shares/{sid}")
def api_share_status(sid: str, request: Request):
    """异步分享页的公开最小状态；不返回内部错误、源短链或签名媒体地址。"""
    row = db_exec("SELECT * FROM shares WHERE id=?", (sid,), "one")
    if not row:
        raise ApiError(404, "分享页不存在或已被删除")
    return {"ok": True, "data": _share_async_payload(
        dict(row), _origin(request))}


# ---- 首次分享解析队列（持久化 + 租约/CAS；与付费 API jobs 分离）----

_share_parse_stop = threading.Event()
_share_parse_wakeup = queue.Queue(maxsize=1)
_share_parse_threads: list[threading.Thread] = []
_share_parse_workers_guard = threading.Lock()
_share_parse_instance = "sw_" + secrets.token_urlsafe(9)


def _wake_share_parse_workers() -> None:
    try:
        _share_parse_wakeup.put_nowait(True)
    except queue.Full:
        pass


def _expire_share_parse_jobs() -> int:
    """终结超时、过期或已耗尽次数的任务，并在同一事务里退款。"""
    now = int(time.time())
    expired = 0
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id,quota_reservation_id FROM shares "
                "WHERE parse_status IN ('pending','processing') AND ("
                "created<=? OR (expires_at>0 AND expires_at<=?) OR "
                "(COALESCE(attempts,0)>=? AND (parse_status='pending' "
                "OR COALESCE(lease_until,0)<?)))",
                (now - SHARE_PARSE_DEADLINE_SECONDS, now,
                 SHARE_PARSE_MAX_ATTEMPTS, now)).fetchall()
            for row in rows:
                changed = conn.execute(
                    "UPDATE shares SET parse_status='failed',source_url=NULL,"
                    "parse_error_code='parse_timeout',lease_owner=NULL,lease_until=NULL,"
                    "expires_at=CASE WHEN expires_at>0 THEN MIN(expires_at,?) ELSE ? END,"
                    "updated=? WHERE id=? AND parse_status IN ('pending','processing')",
                    (now + SHARE_FAILED_TTL, now + SHARE_FAILED_TTL,
                     now, row["id"])).rowcount
                if changed:
                    _settle_quota_in_conn(conn, row["quota_reservation_id"], 0)
                    expired += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return expired


def _claim_share_parse(owner: str) -> Optional[dict]:
    """领取一条到期任务；进程崩溃后由其他 worker 接管过期租约。"""
    now = int(time.time())
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM shares WHERE status='ok' AND source_url IS NOT NULL "
                "AND COALESCE(expires_at,0)>? AND COALESCE(attempts,0)<? "
                "AND created>? AND ("
                "(parse_status='pending' AND COALESCE(next_attempt_at,0)<=?) OR "
                "(parse_status='processing' AND COALESCE(lease_until,0)<?)) "
                "ORDER BY COALESCE(next_attempt_at,created),created,"
                "COALESCE(attempts,0) LIMIT 1",
                (now, SHARE_PARSE_MAX_ATTEMPTS,
                 now - SHARE_PARSE_DEADLINE_SECONDS, now, now)).fetchone()
            if not row:
                conn.rollback()
                return None
            changed = conn.execute(
                "UPDATE shares SET parse_status='processing',lease_owner=?,lease_until=?,"
                "attempts=COALESCE(attempts,0)+1,updated=? WHERE id=? AND status='ok' "
                "AND source_url IS NOT NULL AND created>? AND ("
                "(parse_status='pending' AND COALESCE(next_attempt_at,0)<=?) OR "
                "(parse_status='processing' AND COALESCE(lease_until,0)<?))",
                (owner, min(now + SHARE_PARSE_LEASE_SECONDS,
                            int(row["created"] or now) + SHARE_PARSE_DEADLINE_SECONDS),
                 now, row["id"], now - SHARE_PARSE_DEADLINE_SECONDS, now, now)
            ).rowcount
            if changed != 1:
                conn.rollback()
                return None
            if row["quota_reservation_id"]:
                conn.execute(
                    "UPDATE quota_reservations SET lease_until=? "
                    "WHERE id=? AND status='pending'",
                    (now + max(QUOTA_RESERVATION_TTL,
                               SHARE_PARSE_DEADLINE_SECONDS),
                     row["quota_reservation_id"]))
            conn.commit()
            item = dict(row)
            item["lease_owner"] = owner
            item["attempts"] = int(row["attempts"] or 0) + 1
            return item
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _finish_share_parse_success(item: dict, data: dict) -> bool:
    item_id = str(data.get("item_id") or "")
    if not item_id:
        raise ApiError(502, "解析数据不完整")
    kind = str(data.get("kind") or "video")
    if kind not in ("video", "note"):
        raise ApiError(400, "不支持的作品类型")
    vid = ""
    video_payload = data.get("video") or {}
    play_url = (video_payload.get("url") or video_payload.get("direct_url") or "")
    match = re.search(r"[?&]video_id=([\w-]+)", play_url)
    if match:
        vid = match.group(1)
    if kind == "video" and not play_url and not video_payload.get("proxy_url"):
        raise ApiError(502, "视频播放地址缺失")
    if kind == "note" and not (data.get("images") or []):
        raise ApiError(502, "图集内容缺失")
    now = int(time.time())
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM shares WHERE id=? AND parse_status='processing' "
                "AND lease_owner=?", (item["id"], item["lease_owner"])).fetchone()
            if not current or current["status"] != "ok":
                conn.rollback()
                return False
            if conn.execute(
                    "SELECT 1 FROM blocked_share_items WHERE kind=? AND item_id=?",
                    (kind, item_id)).fetchone():
                if current["source_hash"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO blocked_share_sources(source_hash,created) "
                        "VALUES(?,?)", (current["source_hash"], now))
                conn.execute(
                    "UPDATE shares SET status='takedown',parse_status='failed',"
                    "source_url=NULL,parse_error_code='content_blocked',"
                    "lease_owner=NULL,lease_until=NULL,updated=? "
                    "WHERE id=? AND parse_status='processing' AND lease_owner=?",
                    (now, item["id"], item["lease_owner"]))
                _settle_quota_in_conn(
                    conn, current["quota_reservation_id"], 0)
                conn.commit()
                return False
            if int(current["created"] or now) <= (
                    now - SHARE_PARSE_DEADLINE_SECONDS):
                conn.execute(
                    "UPDATE shares SET parse_status='failed',source_url=NULL,"
                    "parse_error_code='parse_timeout',lease_owner=NULL,lease_until=NULL,"
                    "expires_at=CASE WHEN expires_at>0 THEN MIN(expires_at,?) ELSE ? END,"
                    "updated=? WHERE id=? AND parse_status='processing' AND lease_owner=?",
                    (now + SHARE_FAILED_TTL, now + SHARE_FAILED_TTL, now,
                     item["id"], item["lease_owner"]))
                _settle_quota_in_conn(
                    conn, current["quota_reservation_id"], 0)
                conn.commit()
                return False
            ttl = SHARE_TTL_USER if current["owner_user_id"] else SHARE_TTL_ANON
            changed = conn.execute(
                "UPDATE shares SET item_id=?,kind=?,vid=?,title=?,author=?,avatar=?,"
                "cover=?,payload=?,expires_at=?,refreshed_at=?,parse_status='ready',"
                "source_url=NULL,parse_error_code=NULL,next_attempt_at=0,"
                "lease_owner=NULL,lease_until=NULL,updated=?,ready_at=? "
                "WHERE id=? AND status='ok' AND parse_status='processing' AND lease_owner=?",
                (item_id, kind, vid, (data.get("title") or "")[:300],
                 (data.get("author") or "")[:100], data.get("avatar", ""),
                 data.get("cover", ""),
                 json.dumps(_share_storage_payload(data), ensure_ascii=False),
                 now + ttl, now, now, now, item["id"], item["lease_owner"])
            ).rowcount
            if changed != 1:
                conn.rollback()
                return False
            committed = _settle_quota_in_conn(
                conn, current["quota_reservation_id"], 1)
            if committed != 1:
                raise RuntimeError("share quota reservation is not pending")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return True


def _share_parse_retry_delay(attempt: int) -> int:
    base = (2, 5, 15, 45, 120, 300)[min(max(1, attempt), 6) - 1]
    return base + secrets.randbelow(max(1, base // 2 + 1))


def _finish_share_parse_failure(item: dict, code: str,
                                retryable: bool) -> str:
    """CAS 重试或终结任务；公开库只存稳定错误码，不存原始异常。"""
    now = int(time.time())
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM shares WHERE id=? AND parse_status='processing' "
                "AND lease_owner=?", (item["id"], item["lease_owner"])).fetchone()
            if not current:
                conn.rollback()
                return "lost"
            attempt = int(current["attempts"] or 0)
            before_deadline = int(current["created"] or now) > (
                now - SHARE_PARSE_DEADLINE_SECONDS)
            can_retry = (retryable and current["status"] == "ok"
                         and attempt < SHARE_PARSE_MAX_ATTEMPTS
                         and before_deadline and current["source_url"])
            if can_retry:
                delay = _share_parse_retry_delay(attempt)
                changed = conn.execute(
                    "UPDATE shares SET parse_status='pending',parse_error_code=?,"
                    "next_attempt_at=?,lease_owner=NULL,lease_until=NULL,updated=? "
                    "WHERE id=? AND status='ok' AND parse_status='processing' "
                    "AND lease_owner=?",
                    (code, now + delay, now, item["id"], item["lease_owner"])
                ).rowcount
                if changed == 1 and current["quota_reservation_id"]:
                    conn.execute(
                        "UPDATE quota_reservations SET lease_until=? "
                        "WHERE id=? AND status='pending'",
                        (now + max(QUOTA_RESERVATION_TTL,
                                   SHARE_PARSE_DEADLINE_SECONDS),
                         current["quota_reservation_id"]))
                outcome = "retry" if changed == 1 else "lost"
            else:
                final_code = ("parse_timeout" if retryable and
                              (attempt >= SHARE_PARSE_MAX_ATTEMPTS
                               or not before_deadline) else code)
                changed = conn.execute(
                    "UPDATE shares SET parse_status='failed',source_url=NULL,"
                    "parse_error_code=?,next_attempt_at=0,lease_owner=NULL,lease_until=NULL,"
                    "expires_at=CASE WHEN expires_at>0 THEN MIN(expires_at,?) ELSE ? END,"
                    "updated=? WHERE id=? AND parse_status='processing' AND lease_owner=?",
                    (final_code, now + SHARE_FAILED_TTL, now + SHARE_FAILED_TTL,
                     now, item["id"], item["lease_owner"])
                ).rowcount
                if changed == 1:
                    _settle_quota_in_conn(conn, current["quota_reservation_id"], 0)
                outcome = "failed" if changed == 1 else "lost"
            conn.commit()
            return outcome
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _run_claimed_share_parse(item: dict) -> None:
    try:
        data = _parse_cached(item["source_url"])
        _finish_share_parse_success(item, data)
    except ApiError as exc:
        if exc.status == 400:
            code, retryable = "unsupported_link", False
        elif exc.status == 404:
            code, retryable = "content_unavailable", False
        else:
            code = "upstream_unavailable"
            retryable = exc.status in (408, 425, 429, 500, 502, 503, 504)
        _finish_share_parse_failure(item, code, retryable)
    except Exception:
        _finish_share_parse_failure(item, "upstream_unavailable", True)


def _share_parse_worker_loop(worker_no: int) -> None:
    owner = f"{_share_parse_instance}:{worker_no}"
    while not _share_parse_stop.is_set():
        try:
            item = _claim_share_parse(owner)
        except Exception:
            _share_parse_stop.wait(1)
            continue
        if item is None:
            try:
                _share_parse_wakeup.get(timeout=1)
            except queue.Empty:
                pass
            continue
        try:
            _run_claimed_share_parse(item)
        except Exception:
            # 数据库瞬时故障时保留租约，到期由本进程或其他实例接管。
            _share_parse_stop.wait(0.5)


def _share_parse_heartbeat_once() -> None:
    prefix = _share_parse_instance + ":"
    _expire_share_parse_jobs()
    now = int(time.time())
    cutoff = now - SHARE_PARSE_DEADLINE_SECONDS
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE shares SET lease_until=MIN(?,created+?) "
                "WHERE parse_status='processing' AND created>? "
                "AND substr(lease_owner,1,?)=?",
                (now + SHARE_PARSE_LEASE_SECONDS, SHARE_PARSE_DEADLINE_SECONDS,
                 cutoff, len(prefix), prefix))
            conn.execute(
                "UPDATE quota_reservations SET lease_until=? WHERE status='pending' "
                "AND id IN (SELECT quota_reservation_id FROM shares "
                "WHERE parse_status='processing' AND created>? "
                "AND substr(lease_owner,1,?)=?)",
                (now + max(QUOTA_RESERVATION_TTL,
                           SHARE_PARSE_DEADLINE_SECONDS),
                 cutoff, len(prefix), prefix))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _share_parse_heartbeat_loop() -> None:
    while not _share_parse_stop.wait(SHARE_PARSE_HEARTBEAT_SECONDS):
        try:
            _share_parse_heartbeat_once()
        except Exception:
            pass


def _prepare_share_parse_jobs() -> None:
    """启动恢复：让过期租约重新排队，并保护仍有效任务的额度预占。"""
    now = int(time.time())
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE shares SET parse_status='ready' WHERE parse_status IS NULL")
            conn.execute(
                "UPDATE shares SET parse_status='pending',lease_owner=NULL,lease_until=NULL,"
                "next_attempt_at=MIN(COALESCE(next_attempt_at,?),?) "
                "WHERE parse_status='processing' AND COALESCE(lease_until,0)<?",
                (now, now, now))
            conn.execute(
                "UPDATE quota_reservations SET lease_until=? WHERE status='pending' "
                "AND id IN (SELECT quota_reservation_id FROM shares "
                "WHERE parse_status IN ('pending','processing'))",
                (now + max(QUOTA_RESERVATION_TTL,
                           SHARE_PARSE_DEADLINE_SECONDS),))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    _expire_share_parse_jobs()


def _start_share_parse_workers(prepared: bool = False) -> None:
    with _share_parse_workers_guard:
        _share_parse_threads[:] = [t for t in _share_parse_threads if t.is_alive()]
        if any(t.is_alive() for t in _share_parse_threads):
            return
        if not prepared:
            _prepare_share_parse_jobs()
        _share_parse_stop.clear()
        while True:
            try:
                _share_parse_wakeup.get_nowait()
            except queue.Empty:
                break
        _share_parse_threads.append(threading.Thread(
            target=_share_parse_heartbeat_loop,
            name="share-parse-heartbeat", daemon=False))
        for i in range(SHARE_PARSE_WORKERS):
            _share_parse_threads.append(threading.Thread(
                target=_share_parse_worker_loop, args=(i,),
                name=f"share-parse-worker-{i}", daemon=False))
        for thread in _share_parse_threads:
            thread.start()
        _wake_share_parse_workers()


def _stop_share_parse_workers() -> None:
    with _share_parse_workers_guard:
        _share_parse_stop.set()
        _wake_share_parse_workers()
        threads = list(_share_parse_threads)
    deadline = time.monotonic() + SHARE_PARSE_SHUTDOWN_TIMEOUT
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)
    with _share_parse_workers_guard:
        _share_parse_threads[:] = [
            t for t in _share_parse_threads if t.is_alive()]


class ShareEventBody(BaseModel):
    kind: str = Field(max_length=24)
    source: str = Field(default="", max_length=24)   # 播放线路
    stage: str = Field(default="", max_length=24)    # start / ok / error / timeout / giveup
    detail: str = Field(default="", max_length=120)  # media error code、readyState 等
    ms: int = 0                 # 从该线路开始到出结果的耗时
    next: str = Field(default="", max_length=24)


# 播放诊断事件：play_try/play_ok/play_fail 只写 share_events，不累加 shares 计数，
# 避免把「尝试次数」混进 plays（plays 仍只由 play 事件累加，代表一次成功起播）。
SHARE_EVENT_KINDS = ("view", "play", "download", "cta", "fallback",
                     "play_try", "play_ok", "play_fail")


@app.post("/api/share/{sid}/event")
def api_share_event(sid: str, body: ShareEventBody, request: Request):
    if body.kind not in SHARE_EVENT_KINDS:
        raise ApiError(422, "不支持的事件类型")
    outcome = _share_event(request, sid, body.kind, body.source, body.stage,
                           body.detail, body.ms, body.next)
    if outcome == "missing":
        raise ApiError(404, "分享页不存在或已失效")
    if outcome == "limited":
        raise ApiError(429, "事件上报过于频繁，请稍后再试",
                       {"Retry-After": "60"})
    if outcome == "error":
        raise ApiError(503, "事件暂时无法记录，请稍后重试")
    return {"ok": True, "duplicate": outcome == "duplicate"}


def _qr_bytes(sid: str, request: Request, kind: str, scale: int):
    try:
        import segno
    except ImportError:
        raise ApiError(501, "服务器未安装二维码依赖 segno")
    row = db_exec("SELECT assigned_origin FROM shares WHERE id=?", (sid,), "one")
    origin = (_normalize_origin_value(row["assigned_origin"] if row else "")
              or PUBLIC_ORIGIN or _share_origin(request))
    buf = io.BytesIO()
    segno.make(f"{origin}/s/{sid}", error="m").save(
        buf, kind=kind, scale=scale, border=2, dark="#111418", light="#ffffff")
    return buf.getvalue()


@app.get("/s/{sid}/qr.svg")
def share_qr(sid: str, request: Request):
    return Response(_qr_bytes(sid, request, "svg", 6), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/s/{sid}/qr.png")
def share_qr_png(sid: str, request: Request):
    """海报合成用：同源 PNG，画进 canvas 不会污染画布（SVG 在部分浏览器会）。"""
    return Response(_qr_bytes(sid, request, "png", 8), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/shares")
def api_my_shares(request: Request, limit: int = 50):
    u = current_user(request)
    if not u:
        raise ApiError(401, "请先登录后查看我的分享")
    rows = db_exec("SELECT * FROM shares WHERE owner_user_id=? ORDER BY created DESC LIMIT ?",
                   (u["id"], max(1, min(200, limit))), "all")
    origin = _share_origin(request)     # 复制出去的链接始终用当前可用域名
    return {"shares": [_share_view(dict(r), origin) for r in rows]}


@app.delete("/api/shares/{sid}")
def api_share_delete(sid: str, request: Request):
    u = current_user(request)
    manage_token = request.headers.get("X-Share-Manage-Token") or ""
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM shares WHERE id=?", (sid,)).fetchone()
            if not row:
                raise ApiError(404, "分享页不存在或已被删除")
            row_dict = dict(row)
            is_owner = bool(u and row["owner_user_id"] == u["id"])
            has_capability = _valid_share_manage_token(row_dict, manage_token)
            if not (is_owner or has_capability):
                raise ApiError(403, "没有权限删除该分享页")
            # claim 与删除都持有 BEGIN IMMEDIATE：attempts>0 说明后台已经实际占用过
            # 一次解析资源，此时删除只取消后续发布但不退款；从未领取的 pending 才退款。
            committed = 1 if int(row["attempts"] or 0) > 0 else 0
            _settle_quota_in_conn(
                conn, row["quota_reservation_id"], committed)
            conn.execute("DELETE FROM shares WHERE id=?", (sid,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return {"ok": True}


class ReportBody(BaseModel):
    sid: str = Field(min_length=3, max_length=32)
    reason: str = Field(min_length=1, max_length=1000)
    contact: str = Field(default="", max_length=200)


@app.post("/api/report")
def api_report(body: ReportBody, request: Request):
    """侵权/违规投诉入口（无需登录）。管理员在后台处理后可下架。"""
    sid = body.sid.strip()
    reason = body.reason.strip()
    contact = body.contact.strip()
    if not reason:
        raise ApiError(400, "请填写投诉理由")
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,32}", sid):
        raise ApiError(404, "分享页不存在或已失效")
    ip = _client_ip(request)
    if not _rate_ok(_report_hits, ip, 3600, REPORT_MAX_PER_HOUR):
        raise ApiError(429, "投诉提交过于频繁，请稍后再试",
                       {"Retry-After": "3600"})
    now = int(time.time())
    stored_ip = _privacy_hash("report-ip", ip)
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            share = conn.execute(
                "SELECT status,expires_at FROM shares WHERE id=?", (sid,)).fetchone()
            if (not share or share["status"] in ("dead", "takedown")
                    or (share["expires_at"] and int(share["expires_at"]) <= now)):
                raise ApiError(404, "分享页不存在或已失效")
            duplicate = conn.execute(
                "SELECT 1 FROM reports WHERE sid=? AND ip=? AND reason=? AND ts>=?",
                (sid, stored_ip, reason, now - 3600)).fetchone()
            if not duplicate:
                conn.execute(
                    "INSERT INTO reports(ts,sid,reason,contact,ip) VALUES(?,?,?,?,?)",
                    (now, sid, reason, contact, stored_ip))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return {"ok": True, "message": "已收到，我们会尽快处理"}


# ---- 微信 JS-SDK 分享卡片签名 ----
#
# 裸页面在微信里的分享卡片由微信自行抓取，样式朴素；接入 JS-SDK 才能精确控制
# 标题/描述/缩略图。需要「已认证服务号 + 已备案域名」，在后台填 AppID/AppSecret 启用。
# jsapi_ticket 全局唯一、7200s 有效且有调用频次上限 → **必须存 app_settings 表**，
# 存内存会导致多 worker 各自刷新互相顶掉。

def _wx_api(url: str) -> dict:
    """请求微信开放接口。**刻意不走代理池**——公众号要求服务器出口 IP 在白名单内，
    走代理会因 IP 不匹配而失败；且这里请求的是微信而非抖音，不涉及被抖音封的问题。"""
    req = urlreq.Request(url, headers={"User-Agent": "douyin-dl/1.0"})
    with urlreq.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


_wx_ticket_lock = threading.Lock()


def _wx_ticket():
    """合并并发刷新，并与后台凭据变更互斥，避免旧票据签署新 AppID。"""
    with _wx_ticket_lock:
        return _wx_ticket_locked()


def _wx_ticket_locked():
    """返回 (appid, jsapi_ticket)；未配置返回 (None, None)。带持久缓存。"""
    appid = app_setting("wx_appid").strip()
    secret = app_setting("wx_secret").strip()
    if not (appid and secret):
        return None, None
    now = time.time()
    cached = app_setting("wx_ticket")
    try:
        exp = float(app_setting("wx_ticket_exp") or 0)
    except ValueError:
        exp = 0
    if cached and exp > now + 60:
        return appid, cached
    tok = _wx_api("https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential"
                  f"&appid={urlparse.quote(appid)}&secret={urlparse.quote(secret)}")
    if not isinstance(tok, dict) or not isinstance(tok.get("access_token"), str) or not tok["access_token"]:
        raise ApiError(502, "分享卡片配置暂不可用，请稍后重试")
    tk = _wx_api("https://api.weixin.qq.com/cgi-bin/ticket/getticket?type=jsapi"
                 f"&access_token={urlparse.quote(tok['access_token'])}")
    if not isinstance(tk, dict) or not isinstance(tk.get("ticket"), str) or not tk["ticket"]:
        raise ApiError(502, "分享卡片配置暂不可用，请稍后重试")
    # 先校验有效期，再写票据，畸形响应不能留下部分缓存。
    ttl = int(tk.get("expires_in", 7200))
    if not 0 < ttl <= 7200:
        raise ApiError(502, "分享卡片配置暂不可用，请稍后重试")
    set_app_setting("wx_ticket", tk["ticket"])
    set_app_setting("wx_ticket_exp", now + ttl - 300)
    return appid, tk["ticket"]


@app.get("/api/wx/jssdk")
def wx_jssdk(request: Request, url: str = ""):
    """给分享页签名。未配置公众号时返回 enabled=false，前端静默降级。"""
    # 先校验 URL 归属，再取 ticket：避免外站请求也能触发微信接口调用（有频次上限）
    # 含后台主分享域名：反代头缺失（如 Cloudflare flexible SSL）导致 _origin 取错时仍能签名
    primary = _normalize_origin_value(app_setting("share_primary_domain", ""))
    allowed = set(SHARE_DOMAINS) | {_origin(request)}
    if primary:
        allowed.add(primary)
    page = (url or "").split("#")[0]
    try:
        parsed = urlparse.urlsplit(page)
        if (len(page) > 4096 or parsed.username or parsed.password
                or parsed.scheme not in ("http", "https") or not parsed.hostname):
            raise ValueError("invalid page URL")
        page_origin = _normalize_origin_value(
            f"{parsed.scheme}://{parsed.netloc}")
    except (TypeError, ValueError):
        page_origin = ""
    if not page_origin or page_origin not in allowed:
        raise ApiError(403, "该 URL 不属于本站，拒绝签名")
    try:
        appid, ticket = _wx_ticket()
    except Exception:
        # 公众号网络、鉴权或响应异常时保持页面可用，公开接口不透出上游原文。
        return {"enabled": False, "error": "分享卡片配置暂不可用，请稍后重试"}
    if not appid:
        return {"enabled": False}
    nonce = secrets.token_hex(8)
    ts = int(time.time())
    raw = (f"jsapi_ticket={ticket}&noncestr={nonce}&timestamp={ts}&url={page}")
    return {"enabled": True, "appId": appid, "timestamp": ts, "nonceStr": nonce,
            "signature": hashlib.sha1(raw.encode()).hexdigest()}


# ---------------------------------------------------------------- 主解析服务（保留内部配置名称兼容）
#
# 网页、批量 API 与分享 worker 优先通过主服务取数；抖音缺失信息由官方接口补全。
# 硬约束：
#   · 普通解析只传 workUrl，默认不传 taskType，不发起语音转文字
#   · 只有用户主动使用文案提取时才传 taskType=TEXT，并走 atc_jobs 持久队列
#   · 对方并发上限 5：主解析最多 3 个在途，后台队列最多 2 个在途
#   · 只存 URL 与文案元数据，不落地媒体字节；缓存随 DATA_RETENTION_DAYS 清理
#   · 出站刻意不走代理池（同微信 JS-SDK 先例：第三方 API 要求出口稳定，且非抖音无封 IP 风险）

ATC_DEFAULT_BASE = "https://api.anytocopy.com/vip/open-api/v1"
ATC_POLL_INTERVAL = 4          # 官方建议 3-5 秒
ATC_JOB_TIMEOUT = 300          # 单任务最长 5 分钟
ATC_INFLIGHT_MAX = 2           # 同时在轮询的任务数（对方并发上限 5，留余量给其网页端）
ATC_PRIMARY_INFLIGHT_MAX = 3   # 主解析占 3 席，与后台队列合计不超过 5
ATC_WORKERS = _clamped_env_int("ATC_WORKERS", 1, 1, ATC_INFLIGHT_MAX)
ATC_SUBMIT_LEASE_SECONDS = 60
ATC_POLL_LEASE_SECONDS = 45
ATC_SHUTDOWN_TIMEOUT = 35
ATC_WORK_URL = "https://www.douyin.com/video/{item_id}"   # 由 item_id 还原作品链接
SHARE_PLAY_SOURCES = ("atc", "proxy", "dy1", "dy2")
_atc_primary_slots = threading.BoundedSemaphore(ATC_PRIMARY_INFLIGHT_MAX)
_atc_stop = threading.Event()
_atc_threads: list[threading.Thread] = []
_atc_workers_guard = threading.Lock()
_atc_instance = "atc_" + secrets.token_urlsafe(9)


def _atc_cfg() -> dict:
    """读取运行时配置（app_settings，后台改即时生效）。"""
    key = app_setting("atc_api_key").strip()
    secret = app_setting("atc_api_secret").strip()
    try:
        daily = max(0, min(100, int(app_setting("atc_transcript_daily", "5"))))
    except ValueError:
        daily = 5
    try:
        # 实测 ATC 地址约 1 小时后 403（2026-08-07 实测），默认 1 小时并提前换线
        ttl = max(600, min(86400, int(app_setting("atc_url_ttl", "3600"))))
    except ValueError:
        ttl = 3600
    try:
        priority = json.loads(app_setting("share_play_priority", ""))
        if not (isinstance(priority, list)
                and sorted(priority) == sorted(SHARE_PLAY_SOURCES)):
            raise ValueError
    except (ValueError, TypeError):
        priority = list(SHARE_PLAY_SOURCES)
    return {
        "enabled": app_setting("atc_enabled", "1") == "1" and bool(key and secret),
        "key": key, "secret": secret,
        # 固定官方接口，避免后台误配或把 API 凭据发送到非预期主机。
        "base": ATC_DEFAULT_BASE,
        "play_enhance": app_setting("atc_play_enhance", "1") == "1",
        "transcript_enabled": app_setting("atc_transcript_enabled", "0") == "1",
        "transcript_daily": daily,
        "url_ttl": ttl,
        "play_priority": priority,
    }


class _ParserServiceError(ApiError):
    """内部重试分类；不给前端下发上游响应、凭据或服务商名称。"""
    def __init__(self, status: int, message: str, *, reason="failed", retryable=False):
        super().__init__(status, message, {"Retry-After": "5"} if reason == "busy" else None)
        self.reason, self.retryable = reason, retryable


ATC_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ATC_MAX_POLLS = 60


def _atc_request(method: str, path: str, params: dict, cfg: dict) -> dict:
    """调 ATC 开放 API；对外只抛不包含密钥/完整 URL 的稳定错误。"""
    url = cfg["base"] + path + "?" + urlparse.urlencode(params)
    req = urlreq.Request(url, method=method)
    req.add_header("X-API-Key", cfg["key"])
    req.add_header("X-API-Secret", cfg["secret"])
    req.add_header("User-Agent", pick_ua())
    try:
        with urlreq.urlopen(req, timeout=30) as resp:
            raw = resp.read(ATC_MAX_RESPONSE_BYTES + 1)
    except urlerr.HTTPError as exc:
        exc.close()
        if exc.code in (401, 403):
            raise _ParserServiceError(503, "视频解析服务暂时不可用，请稍后重试", reason="auth")
        if exc.code == 429:
            raise _ParserServiceError(503, "视频解析请求较多，请稍后重试",
                                      reason="busy", retryable=True)
        raise _ParserServiceError(502, "视频解析服务连接异常，请稍后重试",
                                  retryable=exc.code >= 500)
    except (urlerr.URLError, TimeoutError, OSError):
        raise _ParserServiceError(502, "视频解析服务连接异常，请稍后重试", retryable=True)
    if len(raw) > ATC_MAX_RESPONSE_BYTES:
        raise _ParserServiceError(502, "视频解析结果暂不可用，请稍后重试")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        raise _ParserServiceError(502, "视频解析结果暂不可用，请稍后重试")
    if not isinstance(payload, dict):
        raise _ParserServiceError(502, "视频解析结果暂不可用，请稍后重试")
    return payload


def _atc_rejected(resp: dict, action: str = "任务") -> ApiError:
    msg = str(resp.get("msg") or resp.get("message") or "")[:160]
    code = str(resp.get("code") or "")
    lowered = msg.lower()
    if code in ("401", "403") or any(
            token in lowered for token in ("api key", "secret", "验证失败", "鉴权")):
        return _ParserServiceError(503, "视频解析服务暂时不可用，请稍后重试", reason="auth")
    if code == "601" or any(token in lowered for token in ("会员", "权益", "余额", "额度")):
        return _ParserServiceError(503, "视频解析服务暂时不可用，请稍后重试", reason="entitlement")
    if code == "429" or "并发" in msg or "concurrent" in lowered:
        return _ParserServiceError(503, "视频解析请求较多，请稍后重试", reason="busy", retryable=True)
    return _ParserServiceError(502, f"视频解析{action}失败，请稍后重试")


def _atc_extract(work_url: str, include_text: bool = False) -> dict:
    """提交并轮询一个 ATC 任务。普通解析不传 taskType。"""
    cfg = _atc_cfg()
    if not (cfg["key"] and cfg["secret"]):
        raise ApiError(503, "视频解析服务暂时不可用，请稍后重试")
    if not cfg["enabled"]:
        raise ApiError(503, "视频解析服务暂时不可用，请稍后重试")
    if not _atc_primary_slots.acquire(timeout=10):
        raise ApiError(503, "视频解析任务较多，请稍后重试",
                       {"Retry-After": "5"})
    try:
        deadline = time.monotonic() + ATC_JOB_TIMEOUT
        params = {"workUrl": work_url}
        if include_text:
            params["taskType"] = "TEXT"
        submitted = _atc_request("POST", "/video/extract", params, cfg)
        if submitted.get("code") != 200 or not submitted.get("data"):
            raise _atc_rejected(submitted, "任务提交")
        created = submitted["data"]
        if _atc_result_complete(created, include_text=include_text):
            return created
        task_id = _atc_task_id(created)
        if not task_id:
            raise ApiError(502, "视频解析结果暂不可用，请稍后重试")

        poll_delay = ATC_POLL_INTERVAL
        for _ in range(ATC_MAX_POLLS):
            if time.monotonic() + poll_delay >= deadline:
                break
            time.sleep(poll_delay)
            poll_delay = ATC_POLL_INTERVAL
            try:
                queried = _atc_request(
                    "GET", "/video/query", {"taskId": task_id}, cfg)
                if queried.get("code") != 200:
                    raise _atc_rejected(queried, "任务查询")
            except _ParserServiceError as exc:
                if exc.retryable:
                    poll_delay = max(ATC_POLL_INTERVAL, int(exc.headers.get("Retry-After", 0)))
                    continue  # 只重查已有任务，不重复提交或重复计费。
                raise
            data = queried.get("data") or {}
            if not isinstance(data, dict):
                raise ApiError(502, "视频解析结果暂不可用，请稍后重试")
            if _atc_result_complete(data, include_text=include_text):
                return data
        raise ApiError(504, "视频解析超时，请稍后重试")
    finally:
        _atc_primary_slots.release()


def _atc_basic_result_ready(data: dict) -> bool:
    """Return whether a compatibility response contains actual media.

    Providers have returned the same fields both at the top level and inside
    ``data/result/video`` wrappers.  A profile URL, cover image, or author
    object is metadata. This detects media presence only; a WAITING or FAILURE
    payload can also have media. Task completion must use _atc_result_complete.
    """
    media_keys = {
        "videourl", "video_url", "videourllist", "video_url_list",
        "videourls", "video_urls", "imageurllist", "image_url_list",
        "imageurls", "image_urls", "downloadurl", "download_url",
    }
    nested_media_keys = {
        "url", "url_list", "urllist", "play_addr", "playaddr",
        "download_url", "downloadurl", "playurl", "play_url",
    }
    ignored = {
        "author", "user", "creator", "profile", "avatar", "cover",
        "coverurl", "cover_url", "thumbnail", "poster", "music",
    }

    def has_value(value):
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple, set)):
            return any(has_value(item) for item in value)
        return bool(value) if not isinstance(value, dict) else False

    def walk(node, depth=0):
        if depth > 12:
            return False
        if isinstance(node, dict):
            for key, value in node.items():
                normalized = str(key).replace("-", "_").lower()
                compact = normalized.replace("_", "")
                if normalized in media_keys or compact in media_keys:
                    if has_value(value):
                        return True
                if normalized not in ignored and isinstance(value, dict):
                    # Inside a video object, a plain ``url`` is media.  The
                    # same field under author/profile/cover was excluded above.
                    if (normalized in ("video", "media", "file", "result", "data",
                                       "payload", "item", "aweme", "aweme_detail")
                            and any(
                                has_value(value.get(k)) for k in nested_media_keys
                                if k in value)):
                        return True
                    if walk(value, depth + 1):
                        return True
                elif normalized not in ignored and isinstance(value, list):
                    if walk(value, depth + 1):
                        return True
        elif isinstance(node, (list, tuple)):
            return any(walk(value, depth + 1) for value in node)
        return False

    return walk(data)


_ATC_RESULT_WRAPPERS = (
    "data", "result", "payload", "item", "aweme", "aweme_detail")


def _atc_result_data(payload) -> dict:
    """将 ATC 历史上的多种响应包装归一为业务字段。

    只展开已知的 envelope，不展开 ``video``/``author`` 等业务
    对象，避免同名字段串线。内层结果覆盖外层运输元数据。
    """
    def normalize(node, depth=0):
        if depth > 12 or not isinstance(node, dict):
            return {}
        out = {
            str(key): value for key, value in node.items()
            if key not in _ATC_RESULT_WRAPPERS
        }
        for key in _ATC_RESULT_WRAPPERS:
            nested = node.get(key)
            if isinstance(nested, dict):
                out.update(normalize(nested, depth + 1))
            elif isinstance(nested, (list, tuple)):
                for entry in nested:
                    if isinstance(entry, dict):
                        candidate = normalize(entry, depth + 1)
                        if candidate:
                            out.update(candidate)
                            break
        return out

    return normalize(payload)


def _atc_task_id(payload) -> str:
    """Extract a task identifier from common nested provider envelopes."""
    keys = ("taskId", "taskID", "task_id", "taskid", "jobId", "job_id")

    def scalar(value):
        if value is None or isinstance(value, (dict, list, tuple, set, bool)):
            return ""
        text = str(value).strip()
        return text[:256] if text else ""

    def walk_explicit(node, depth=0):
        if depth > 12:
            return ""
        if isinstance(node, dict):
            for key in keys:
                if key in node:
                    found = scalar(node.get(key))
                    if found:
                        return found
            for key in _ATC_RESULT_WRAPPERS:
                value = node.get(key)
                found = walk_explicit(value, depth + 1)
                if found:
                    return found
        elif isinstance(node, (list, tuple)):
            for value in node:
                found = walk_explicit(value, depth + 1)
                if found:
                    return found
        return ""

    found = walk_explicit(payload)
    if found:
        return found
    # 一些旧版本直接返回 {"data":"task-id"}。只允许根值或
    # 已知 envelope 的标量作为兼容回退，不能把 code/message/status
    # 等任意叶子误当 taskId。
    if not isinstance(payload, (dict, list, tuple, set)):
        return scalar(payload)
    if isinstance(payload, dict):
        for key in _ATC_RESULT_WRAPPERS:
            value = payload.get(key)
            found = scalar(value)
            if found:
                return found
    return ""


def _atc_status_value(payload) -> str:
    """Extract and normalize a task status from nested provider envelopes."""
    keys = ("status", "taskStatus", "task_status", "state")

    def scalar(value):
        if value is None or isinstance(value, (dict, list, tuple, set, bool)):
            return ""
        text = str(value).strip()
        return text.upper()[:64] if text else ""

    def walk(node, depth=0):
        if depth > 12:
            return ""
        if isinstance(node, dict):
            for key in keys:
                if key in node:
                    found = scalar(node.get(key))
                    if found:
                        return found
            for key in _ATC_RESULT_WRAPPERS:
                value = node.get(key)
                found = walk(value, depth + 1)
                if found:
                    return found
        elif isinstance(node, (list, tuple)):
            for value in node:
                found = walk(value, depth + 1)
                if found:
                    return found
        return ""

    found = walk(payload)
    if found:
        return found
    if not isinstance(payload, (dict, list, tuple, set)):
        return scalar(payload)
    return ""


def _atc_result_complete(payload, *, include_text=False) -> bool:
    """以任务状态为准；新版等待/失败响应也含媒体，不能提前缓存。

    仅对没有状态的旧同步响应兼容媒体判定。作者等业务对象中的 status
    不能改变任务状态，转录始终等待明确的成功状态。
    """
    if not isinstance(payload, dict):
        return False
    status = _atc_status_value(payload)
    if status in ("FAILED", "FAILURE", "ERROR"):
        message = str(_atc_result_data(payload).get("errorMessage") or "")[:160]
        unavailable = any(x in message for x in ("不存在", "删除", "私密"))
        raise _ParserServiceError(404 if unavailable else 502,
            "作品无法解析，可能已失效、删除或设为私密" if unavailable
            else "视频解析失败，请稍后重试")
    return status in ("SUCCESS", "SUCCEEDED", "DONE") or (
        not status and not include_text and _atc_basic_result_ready(payload))


_MAX_MEDIA_DURATION_MS = 24 * 60 * 60 * 1000


def _atc_duration_ms(value, assume_ms: bool = False) -> int:
    """Normalize duration values returned by compatibility providers.

    Providers have historically returned seconds, milliseconds, ``MM:SS`` /
    ``HH:MM:SS`` strings, and ISO-8601 durations.  Keep this conversion in one
    place so malformed values cannot become a misleading ``0:00`` or an
    absurdly long media element.
    """
    if value is None or isinstance(value, bool) or isinstance(value, dict):
        return 0
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return 0
        iso = re.fullmatch(
            r"P(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?"
            r"(?:(\d+(?:\.\d+)?)S)?)", raw.upper())
        if iso and any(iso.groups()):
            seconds = (float(iso.group(1) or 0) * 3600
                       + float(iso.group(2) or 0) * 60
                       + float(iso.group(3) or 0))
            return _atc_duration_ms(seconds)
        clock = re.fullmatch(r"(?:(\d+):)?(\d{1,3}):(\d{2})(?:\.(\d+))?", raw)
        if clock:
            hours = int(clock.group(1) or 0)
            minutes = int(clock.group(2) or 0)
            seconds = int(clock.group(3) or 0)
            if minutes >= 60 or seconds >= 60:
                return 0
            fraction = float(f"0.{clock.group(4)}") if clock.group(4) else 0.0
            return _atc_duration_ms(hours * 3600 + minutes * 60
                                    + seconds + fraction)
        suffix = re.fullmatch(
            r"([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*(ms|s|m|h)?", raw, re.I)
        if not suffix:
            return 0
        number = float(suffix.group(1))
        unit = (suffix.group(2) or "").lower()
        if unit == "ms":
            assume_ms = True
        elif unit == "s":
            assume_ms = False
        elif unit == "m":
            number *= 60
            assume_ms = False
        elif unit == "h":
            number *= 3600
            assume_ms = False
        value = number
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(number) or number <= 0:
        return 0
    milliseconds = number if assume_ms or number >= 100000 else number * 1000
    if not math.isfinite(milliseconds) or milliseconds <= 0:
        return 0
    return min(_MAX_MEDIA_DURATION_MS, int(round(milliseconds)))


def _atc_payload_duration_ms(payload) -> int:
    """Find a duration in a nested provider response, preferring explicit units."""
    if not isinstance(payload, (dict, list, tuple)):
        return 0
    explicit_keys = {
        "durationms", "duration_ms", "durationmilliseconds", "duration_milliseconds",
    }
    generic_keys = {"duration", "length", "video_duration", "videoduration"}

    def walk(node, depth=0):
        if depth > 12:
            return 0
        if isinstance(node, dict):
            for key, value in node.items():
                normalized = str(key).replace("-", "_").lower()
                if normalized in explicit_keys:
                    parsed = _atc_duration_ms(value, assume_ms=True)
                    if parsed:
                        return parsed
            for key, value in node.items():
                normalized = str(key).replace("-", "_").lower()
                if normalized in generic_keys:
                    parsed = _atc_duration_ms(value)
                    if parsed:
                        return parsed
            for value in node.values():
                parsed = walk(value, depth + 1)
                if parsed:
                    return parsed
        elif isinstance(node, (list, tuple)):
            for value in node:
                parsed = walk(value, depth + 1)
                if parsed:
                    return parsed
        return 0

    return walk(payload)


def _atc_public_url(value) -> str:
    """返回可下发给浏览器的 HTTPS URL，拒绝凭据、自定义端口与异常长值。"""
    value = str(value or "").strip()
    if not value or len(value) > 4096:
        return ""
    try:
        parsed = urlparse.urlsplit(value)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443) or not parsed.hostname):
            return ""
    except (TypeError, ValueError):
        return ""
    return value


def _atc_public_urls(value) -> list[str]:
    """兼容开放 API 把 URL 集合返回为数组或单个字符串。"""
    out, seen = [], set()
    def visit(node):
        if len(out) >= 100:
            return
        if isinstance(node, str):
            url = _atc_public_url(node)
            if url and url not in seen:
                seen.add(url)
                out.append(url)
        elif isinstance(node, dict):
            for key in ("url_list", "url", "src", "download_url", "play_url",
                        "image_url", "imageUrl", "uri"):
                if key in node:
                    visit(node[key])
        elif isinstance(node, (list, tuple)):
            for item in node:
                visit(item)
                if len(out) >= 100:
                    break
    visit(value)
    return out


def _atc_item_id(work_url: str, data: dict) -> str:
    sources = [data]
    nested = _douyin_find_item(data)
    if isinstance(nested, dict) and nested is not data:
        sources.append(nested)
    for source in sources:
        for key in ("workId", "itemId", "awemeId", "aweme_id"):
            candidate = str(source.get(key) or "").strip()
            if re.fullmatch(r"[\w-]{8,40}", candidate):
                return candidate
    for value in (str(data.get("workUrl") or ""), str(nested.get("share_url") or "")
                  if isinstance(nested, dict) else "", work_url):
        match = re.search(r"/(?:video|note|slides|share/(?:video|note|slides))/(\d{8,30})",
                          value)
        if match:
            return match.group(1)
    # 短码不含作品 ID，用不可逆稳定标识做缓存/分享页主键。
    return "item_" + hashlib.sha256(work_url.encode()).hexdigest()[:24]


def _atc_result_to_parse(work_url: str, data: dict,
                         item_id_hint: str = "", kind_hint: str = "",
                         *, allow_partial: bool = False) -> dict:
    data = _atc_result_data(data if isinstance(data, dict) else {})
    platform_id = _atc_platform_for_url(work_url) or "other"
    native_item = _douyin_find_item(data) if isinstance(data, dict) else None
    native_item = native_item if isinstance(native_item, dict) else {}
    work_type = str(data.get("workType") or native_item.get("work_type")
                    or native_item.get("aweme_type") or kind_hint or "video").lower()
    image_urls = (_atc_public_urls(data.get("imageUrlList"))
                  + _atc_public_urls(data.get("images"))
                  + _atc_public_urls(data.get("image_urls")))
    video_obj = data.get("video") if isinstance(data.get("video"), dict) else {}
    if not video_obj and isinstance(native_item.get("video"), dict):
        video_obj = native_item.get("video")
    image_urls += _atc_public_urls(
        video_obj.get("imageUrlList") if isinstance(video_obj, dict) else None)
    video_urls = (_atc_public_urls(data.get("videoUrl"))
                  + _atc_public_urls(data.get("videoUrlList"))
                  + _atc_public_urls(video_obj.get("url"))
                  + _atc_public_urls(video_obj.get("play_addr"))
                  + _atc_public_urls(video_obj.get("download_addr"))
                  + _atc_public_urls(native_item.get("videoUrl"))
                  + _atc_public_urls(native_item.get("videoUrlList")))
    image_urls = list(dict.fromkeys(image_urls))
    video_urls = list(dict.fromkeys(video_urls))
    video_url = next(iter(dict.fromkeys(video_urls)), "")
    kind = "note" if work_type in ("image", "images", "note", "slides") else "video"
    if image_urls and not video_url:
        kind = "note"
    raw_item_id = _atc_item_id(work_url, data)
    if item_id_hint:
        item_id = item_id_hint[:40]
    elif platform_id == "douyin":
        item_id = raw_item_id[:40]
    else:
        namespace = platform_id
        if namespace == "other":
            host = (urlparse.urlsplit(work_url).hostname or "site").lower()
            namespace = "web" + hashlib.sha256(host.encode()).hexdigest()[:8]
        item_id = f"{namespace}_{raw_item_id}"[:40]
    title = str(data.get("title") or data.get("content") or data.get("video_description")
                or native_item.get("desc") or native_item.get("title")
                or "（无标题）").strip()
    title = title[:1000] or "（无标题）"
    base = _safe_name(title, item_id)

    author_data = data.get("author") or data.get("creator") or native_item.get("author") or {}
    if isinstance(author_data, dict):
        author = str(author_data.get("nickname") or author_data.get("name")
                     or author_data.get("display_name") or "")
        avatar = _atc_public_url(
            author_data.get("avatar") or author_data.get("avatarUrl")
            or next(iter(_atc_public_urls(author_data.get("avatar_larger")
                                         or author_data.get("avatar_thumb"))), ""))
        author_url = _atc_public_url(author_data.get("url")
                                     or author_data.get("homepage")
                                     or author_data.get("profile_url"))
    else:
        author, avatar, author_url = str(author_data or ""), "", ""
    author = str(data.get("authorName") or data.get("nickname")
                 or native_item.get("author_name") or author)[:100]
    avatar = _atc_public_url(data.get("avatar") or data.get("avatarUrl")) or avatar
    author_url = _atc_public_url(data.get("authorUrl")) or author_url

    raw_stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    if not raw_stats and isinstance(data.get("statistics"), dict):
        raw_stats = data["statistics"]
    if not raw_stats and isinstance(native_item.get("statistics"), dict):
        raw_stats = native_item["statistics"]
    def stat_value(*keys):
        for key in keys:
            if key in raw_stats and raw_stats[key] is not None:
                return raw_stats[key]
            if key in data and data[key] is not None:
                return data[key]
            if key in native_item and native_item[key] is not None:
                return native_item[key]
        return None
    stats = {
        # 显式判断 None，不能用 ``or`` 否则合法的 0 会被吞掉。
        "digg": stat_value("digg", "digg_count", "diggCount", "like_count", "likeCount"),
        "comment": stat_value("comment", "comment_count", "commentCount"),
        "collect": stat_value("collect", "collect_count", "collectCount", "favorite_count", "save_count"),
        "share": stat_value("share", "share_count", "shareCount"),
    }
    stats = {key: _douyin_number(value) for key, value in stats.items()}
    content = str(data.get("content") or "").strip()[:20000]
    tags = list(dict.fromkeys(re.findall(r"#\s*([^\s#]+)", content or title)))[:50]
    result = {
        "kind": kind, "item_id": item_id, "source": "parser", "title": title,
        "content": content,
        "platform": platform_id,
        "share_supported": platform_id == "douyin" or (platform_id == "tiktok" and kind == "video"),
        "author": author, "avatar": avatar, "author_url": author_url,
        "create_time": _douyin_number(data.get("create_time")), "stats": stats, "tags": tags,
        "music": None, "location": None, "base": base, "_link": work_url,
        "cover": _atc_public_url(
            data.get("cover") or data.get("coverUrl") or data.get("cover_image_url")
            or video_obj.get("cover") or video_obj.get("origin_cover")),
    }

    if kind == "note":
        if not image_urls and not allow_partial:
            raise ApiError(404, "暂未获取到可用的图集地址")
        result["images"] = [
            {"url": url, "filename": f"{base}_{index:02d}.jpeg"}
            for index, url in enumerate(image_urls, 1)]
    else:
        if not video_url and not allow_partial:
            raise ApiError(404, "暂未获取到可用的无水印视频地址")
        duration_value = (data.get("duration") if data.get("duration") is not None
                          else video_obj.get("duration")
                          if video_obj.get("duration") is not None
                          else native_item.get("duration"))
        result["duration_ms"] = (
            _atc_duration_ms(duration_value)
            or _atc_payload_duration_ms(data))
        filename = f"{base}.mp4"
        video = {
            "source": "parser", "url": video_url, "direct_url": video_url,
            "download_refresh_url": _video_download_refresh_url(item_id),
            "alt_url": "", "filename": filename, "media_available": bool(video_url),
            "width": (data.get("width") if data.get("width") is not None else
                      data.get("videoWidth") if data.get("videoWidth") is not None else
                      video_obj.get("width") if video_obj.get("width") is not None else
                      native_item.get("width")),
            "height": (data.get("height") if data.get("height") is not None else
                       data.get("videoHeight") if data.get("videoHeight") is not None else
                       video_obj.get("height") if video_obj.get("height") is not None else
                       native_item.get("height")),
        }
        # 同源流不接受客户端 URL；仅对缓存中的抖音白名单 CDN 开放。
        if _primary_media_allowed(video_url):
            video["proxy_url"] = _atc_video_proxy_url(item_id)
            video["download_url"] = _atc_video_download_url(item_id, filename)
        result["video"] = video

    # ATC 返回的 author/statistics 在不同版本有多种嵌套形式；写入同一作者缓存，
    # 使 /api/author 与抖音官方路径保持一致。缓存只保留展示和富化所需字段。
    author_sec = ""
    author_unique = ""
    author_signature = ""
    author_followers = author_likes = author_following = author_count = None
    if isinstance(author_data, dict):
        author_sec = str(author_data.get("sec_uid") or author_data.get("secUid") or "")[:200]
        author_unique = str(author_data.get("unique_id") or author_data.get("uniqueId")
                            or author_data.get("short_id") or author_data.get("shortId") or "")[:100]
        author_signature = str(author_data.get("signature") or "")[:500]
        author_followers = author_data.get("follower_count")
        author_likes = author_data.get("total_favorited")
        author_following = author_data.get("following_count")
        author_count = author_data.get("aweme_count")
    author_detail = {
        "item_id": item_id, "nickname": author, "author": author,
        "avatar": avatar, "author_url": author_url, "sec_uid": author_sec,
        "unique_id": author_unique, "signature": author_signature,
        "follower_count": _douyin_number(author_followers), "total_favorited": _douyin_number(author_likes),
        "following_count": _douyin_number(author_following), "aweme_count": _douyin_number(author_count),
        "enriched": False,
    }
    if not author_url and platform_id == "tiktok" and re.fullmatch(r"[\w.-]{1,100}", author_unique):
        author_url = f"https://www.tiktok.com/@{author_unique}"
        result["author_url"] = author_detail["author_url"] = author_url
    if not author_url and author_sec and platform_id == "douyin":
        author_url = f"https://www.douyin.com/user/{urlparse.quote(author_sec, safe='')}"
        result["author_url"] = author_detail["author_url"] = author_url
    if platform_id == "tiktok":
        # Public original link only; the saved input used for refresh stays in the backend.
        match = re.search(r"/@([\w.-]+)/video/(\d{8,30})", work_url)
        if match:
            result["original_url"] = f"https://www.tiktok.com/@{match[1]}/video/{match[2]}"
    result["author_detail"] = author_detail
    _author_cache[item_id] = (time.time(), author_detail)

    # 即便上游在基础模式意外附带了转录字段，也不在普通解析路径保存。
    cache_data = dict(data)
    cache_data.pop("textContent", None)
    cache_data.pop("audioUrl", None)
    _atc_save_result(item_id, cache_data, work_url=work_url)
    return result


def _merge_missing_fields(primary: dict, extra: dict) -> dict:
    """只补空字段；0 是合法统计值，已有有效信息不可被低精度接口覆盖。"""
    merged = dict(primary)
    for key, value in extra.items():
        old = merged.get(key)
        if isinstance(old, dict) and isinstance(value, dict):
            merged[key] = _merge_missing_fields(old, value)
        elif (old is None or old == "" or old in ("（无标题）", "(无标题)")
              or (key == "title" and str(old).strip() in ("暂无标题", "无标题"))):
            merged[key] = value
    return merged


def _result_has_media(result: dict) -> bool:
    if result.get("kind") == "note":
        return any(isinstance(image, dict) and (image.get("url") or image.get("proxy_url"))
                   for image in result.get("images") or [])
    video = result.get("video") or {}
    return bool(video.get("url") or video.get("direct_url"))


def _douyin_needs_supplement(result: dict) -> bool:
    if not _result_has_media(result):
        return True
    if any(not result.get(key) or result.get(key) in _EMPTY_TITLES
           for key in ("title", "author", "avatar", "author_url", "cover")):
        return True
    if any((result.get("stats") or {}).get(key) is None
           for key in ("digg", "comment", "collect", "share")):
        return True
    author = (_author_cache.get(result.get("item_id")) or (0, {}))[1]
    if not author.get("sec_uid") or not author.get("unique_id"):
        return True
    if result.get("kind") == "video":
        video = result.get("video") or {}
        return any(not (_douyin_number(value) or 0) for value in (
            result.get("duration_ms"), video.get("width"), video.get("height")))
    return False


def _complete_douyin_result(work_url: str, primary: dict) -> dict:
    """服务器端按需补作品详情；失败时已有媒体仍可用，禁止合并错作品。"""
    old_id = str(primary.get("item_id") or "")
    primary_author = dict((_author_cache.get(old_id) or (0, {}))[1])
    needs_detail = _douyin_needs_supplement(primary)
    # 图集需要真实作品 ID 才能生成受签名保护、可惰性刷新的图片端点。
    if not needs_detail and primary.get("kind") != "note":
        return primary
    _metadata_attempt(old_id)
    stage = "resolve"
    try:
        kind, item_id, canonical = _douyin_resolve_share_url(work_url)
        if re.fullmatch(r"\d{8,30}", old_id) and old_id != item_id:
            # 主服务返回了另一作品，不能把它的媒体、标题或作者混进当前结果。
            primary, primary_author = {}, {}
            return _parse_douyin_item_direct(kind, item_id, canonical)
        if primary.get("kind") == "note" and _result_has_media(primary):
            urls = [image["url"] for image in primary["images"] if image.get("url")]
            if urls and all(_douyin_public_url(url) for url in urls):
                _douyin_cache_note_media(item_id, urls)
                primary = dict(primary, item_id=item_id, source="douyin_direct", _link=canonical)
                primary = _douyin_note_result_with_media(primary, item_id)
        if needs_detail:
            stage = "metadata"
            extra = _parse_douyin_item_direct(kind, item_id, canonical, allow_metadata=True)
            extra_author = dict((_author_cache.get(item_id) or (0, {}))[1])
            merged = _merge_missing_fields(primary, extra)
            if _result_has_media(primary):
                # 媒体地址与缓存/下载端点必须来自同一路径，不能递归混合两套媒体。
                if primary.get("kind") == "video":
                    video = dict(primary["video"])
                    for key in ("width", "height"):
                        if not (_douyin_number(video.get(key)) or 0):
                            video[key] = (extra.get("video") or {}).get(key)
                    merged["video"] = video
                    if not (_douyin_number(primary.get("duration_ms")) or 0):
                        merged["duration_ms"] = extra.get("duration_ms")
                else:
                    merged["images"] = primary["images"]
                merged["source"] = primary["source"]
            else:
                merged.update({key: extra[key] for key in
                               ("kind", "item_id", "source", "video", "images", "duration_ms")
                               if key in extra})
                merged.pop("metadata_only", None)
            merged["_link"] = canonical
            primary = merged
            primary_author = _merge_missing_fields(primary_author, extra_author)
        primary_author["item_id"] = primary["item_id"]
        _author_cache[primary["item_id"]] = (time.time(), primary_author)
    except Exception:
        _record_metadata_failure(stage)
        # 补充接口受网络/风控影响时不丢弃主服务已成功的结果。
        if primary_author and old_id:
            _author_cache[old_id] = (time.time(), primary_author)
        if not _result_has_media(primary):
            raise ApiError(503, "暂未获取到作品内容，请稍后重试") from None
    if not _result_has_media(primary):
        raise ApiError(503, "暂未获取到可用的媒体地址，请稍后重试")
    if _metadata_missing(primary):
        _record_metadata_failure(stage)
    return primary


def _atc_parse_work_url(work_url: str, item_id_hint: str = "",
                        kind_hint: str = "") -> dict:
    """统一优先级入口；仅抖音可使用当前服务器的官方补充接口。"""
    douyin = _is_douyin_work_url(work_url)
    try:
        data = _atc_extract(work_url, include_text=False)
        primary = _atc_result_to_parse(
            work_url, data, item_id_hint, kind_hint, allow_partial=douyin)
    except ApiError:
        if not douyin:
            raise
        return _parse_douyin_share_direct(work_url)
    return _complete_douyin_result(work_url, primary) if douyin else primary


def _atc_cache_get(item_id: str) -> Optional[dict]:
    row = db_exec("SELECT * FROM atc_cache WHERE item_id=?", (item_id,), "one")
    return dict(row) if row else None


def _atc_url_fresh(row: Optional[dict], ttl: int) -> bool:
    """缓存里的 API 播放地址是否仍在有效期内（签名链接会过期）。"""
    if not (row and row.get("video_url") and row.get("url_fetched_at")):
        return False
    try:
        age = time.time() - float(row["url_fetched_at"])
        lifetime = float(ttl)
    except (TypeError, ValueError, OverflowError):
        return False
    # Future timestamps usually indicate a corrupt/clock-skewed cache.  Do not
    # advertise a URL as fresh indefinitely when ``age`` is negative.
    return 0 <= age < max(0.0, lifetime)


def _atc_enqueue(item_id: str, work_url: str = "", purpose: str = "play",
                 quota_reservation_id: str = "") -> bool:
    """入队一个 ATC 任务。幂等：同 item_id 有在途任务或缓存仍新鲜 → 不再入队。"""
    cfg = _atc_cfg()
    if not cfg["enabled"] or not item_id:
        return False
    if purpose == "transcript" and not cfg["transcript_enabled"]:
        return False
    cached = _atc_cache_get(item_id)
    if purpose == "play" and _atc_url_fresh(cached, cfg["url_ttl"]):
        return False
    if purpose == "transcript" and cached and cached.get("text_content"):
        return False
    now = int(time.time())
    saved_work_url = (work_url or (cached or {}).get("work_url") or "")
    if not saved_work_url and re.fullmatch(r"\d{1,30}", item_id):
        saved_work_url = ATC_WORK_URL.format(item_id=item_id)
    if not saved_work_url:
        return False
    # 检查与 INSERT 必须处在同一个写事务中。此前两次 db_exec 之间存在
    # 竞态：并发请求都能通过 pending 检查，重复创建任务并浪费 ATC 并发额度。
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                    "SELECT id FROM atc_jobs WHERE item_id=? AND purpose=? "
                    "AND status IN ('pending','submitting','submitted')",
                    (item_id, purpose)).fetchone():
                conn.rollback()
                return False
            # 防任务空转：近期已跑完一轮但仍没有新鲜地址（对方也取不到）
            # → 冷却期内不再入队。
            cooldown = conn.execute(
                "SELECT updated FROM atc_jobs WHERE item_id=? AND purpose=? "
                "AND status IN ('done','failed') ORDER BY updated DESC LIMIT 1",
                (item_id, purpose)).fetchone()
            cooldown_age = (now - int(cooldown[0] or 0)) if cooldown else -1
            if cooldown and 0 <= cooldown_age < cfg["url_ttl"]:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO atc_jobs(item_id,work_url,purpose,status,created,updated,quota_reservation_id) "
                "VALUES(?,?,?,'pending',?,?,?)",
                (item_id, saved_work_url[:1000], purpose, now, now, quota_reservation_id or None))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _atc_save_result(item_id: str, data: dict, work_url: str = "",
                     include_text: bool = False) -> None:
    """任务成功：upsert 缓存。

    普通播放任务只刷新媒体与基础元数据，即使上游意外附带 ``textContent`` 或
    ``audioUrl`` 也不得把文案写入缓存；只有用户明确发起的文案任务才允许新增
    这两个字段。已有文案在播放刷新时保留，避免被意外清空。
    """
    data = _atc_result_data(data if isinstance(data, dict) else {})
    now = int(time.time())
    old = _atc_cache_get(item_id) or {}
    incoming_text = str(
        data.get("textContent") or data.get("text_content") or ""
    ) if include_text else ""
    incoming_audio = next(iter(_atc_public_urls(
        data.get("audioUrl") or data.get("audio_url"))), "") if include_text else ""
    text_content = (incoming_text or str(old.get("text_content") or ""))[:20000]
    audio_url = (incoming_audio or str(old.get("audio_url") or ""))[:4096]
    saved_work_url = (work_url or data.get("workUrl")
                      or old.get("work_url") or "")[:1000]
    video_obj = data.get("video") if isinstance(data.get("video"), dict) else {}
    video_urls = (
        _atc_public_urls(data.get("videoUrl"))
        + _atc_public_urls(data.get("videoUrlList"))
        + _atc_public_urls(data.get("video_url"))
        + _atc_public_urls(data.get("video_urls"))
        + _atc_public_urls(video_obj.get("play_addr"))
        + _atc_public_urls(video_obj.get("download_addr"))
        + _atc_public_urls(video_obj.get("url")))
    fresh_video_url = next(iter(dict.fromkeys(video_urls)), "")
    video_url = fresh_video_url or old.get("video_url") or ""
    fetched = now if fresh_video_url else (old.get("url_fetched_at") or 0)
    db_exec(
        "INSERT INTO atc_cache(item_id,work_url,video_url,url_fetched_at,content,"
        "text_content,audio_url,duration,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(item_id) DO UPDATE SET work_url=?,video_url=?,url_fetched_at=?,"
        "content=?,text_content=?,audio_url=?,duration=?,updated=?",
        (item_id, saved_work_url, video_url, fetched,
         str(data.get("content") or data.get("title")
             or old.get("content") or "")[:2000],
         text_content,
         audio_url,
         (data.get("duration") if data.get("duration") is not None
          else video_obj.get("duration") if video_obj.get("duration") is not None
          else old.get("duration")),
         old.get("created") or now, now,
         saved_work_url, video_url, fetched,
         str(data.get("content") or data.get("title")
             or old.get("content") or "")[:2000],
         text_content,
         audio_url,
         (data.get("duration") if data.get("duration") is not None
          else video_obj.get("duration") if video_obj.get("duration") is not None
          else old.get("duration")), now))


def _atc_claim_pending(owner: str) -> Optional[dict]:
    """原子领取一条待提交任务；租约过期可被其他进程接管。"""
    now = int(time.time())
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE atc_jobs SET status='pending',lease_owner=NULL,"
                "lease_until=NULL,error=COALESCE(error,'提交租约超时，已恢复') "
                "WHERE status='submitting' AND COALESCE(lease_until,0)<?", (now,))
            inflight = conn.execute(
                "SELECT COUNT(*) FROM atc_jobs "
                "WHERE status IN ('submitting','submitted')").fetchone()[0]
            if int(inflight or 0) >= ATC_INFLIGHT_MAX:
                conn.rollback()
                return None
            row = conn.execute(
                "SELECT * FROM atc_jobs WHERE status='pending' "
                "AND (COALESCE(error,'')='' OR COALESCE(updated,0)<=?) "
                "ORDER BY created,id LIMIT 1",
                (now - max(5, ATC_POLL_INTERVAL),)).fetchone()
            if not row:
                conn.rollback()
                return None
            changed = conn.execute(
                "UPDATE atc_jobs SET status='submitting',lease_owner=?,lease_until=?,"
                "updated=? WHERE id=? AND status='pending'",
                (owner, now + ATC_SUBMIT_LEASE_SECONDS, now, row["id"])).rowcount
            if changed != 1:
                conn.rollback()
                return None
            conn.commit()
            job = dict(row)
            job.update({"status": "submitting", "lease_owner": owner,
                        "lease_until": now + ATC_SUBMIT_LEASE_SECONDS})
            return job
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _atc_claim_submitted(owner: str) -> Optional[dict]:
    """原子领取一条到期的轮询任务，状态保持 submitted。"""
    now = int(time.time())
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM atc_jobs WHERE status='submitted' "
                "AND COALESCE(updated,0)<=? AND COALESCE(lease_until,0)<? "
                "ORDER BY updated,id LIMIT 1",
                (now - max(5, ATC_POLL_INTERVAL), now)).fetchone()
            if not row:
                conn.rollback()
                return None
            changed = conn.execute(
                "UPDATE atc_jobs SET lease_owner=?,lease_until=? "
                "WHERE id=? AND status='submitted' AND COALESCE(lease_until,0)<?",
                (owner, now + ATC_POLL_LEASE_SECONDS, row["id"], now)).rowcount
            if changed != 1:
                conn.rollback()
                return None
            conn.commit()
            job = dict(row)
            job.update({"lease_owner": owner,
                        "lease_until": now + ATC_POLL_LEASE_SECONDS})
            return job
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _atc_claim_update(job: dict, status: str, *, task_id: str = "",
                      error: Optional[str] = None) -> bool:
    """只允许当前租约持有者推进任务，防止过期 worker 覆盖新结果。"""
    fields = ["status=?", "updated=?", "lease_owner=NULL", "lease_until=NULL"]
    values = [status, int(time.time())]
    if task_id:
        fields.append("task_id=?")
        values.append(task_id[:256])
    fields.append("error=?")
    values.append(str(error)[:300] if error else None)
    values.extend([job["id"], job["lease_owner"]])
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                f"UPDATE atc_jobs SET {','.join(fields)} WHERE id=? "
                "AND status IN ('submitting','submitted') AND lease_owner=?",
                tuple(values)).rowcount
            if changed and status in ("done", "failed"):
                row = conn.execute("SELECT quota_reservation_id FROM atc_jobs WHERE id=?",
                                   (job["id"],)).fetchone()
                _settle_quota_in_conn(conn, row[0], 1 if status == "done" else 0)
            conn.commit()
            return bool(changed)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _atc_store_job_result(job: dict, data: dict) -> None:
    if job["purpose"] == "transcript":
        _atc_save_result(job["item_id"], data, work_url=job["work_url"], include_text=True)
        return
    if job["purpose"] == "download":
        # 下载刷新只更新主媒体缓存，不拉官方补充、不覆盖分享页的信息快照。
        payload = _atc_result_data(data)
        video = payload.get("video") if isinstance(payload.get("video"), dict) else {}
        urls = (_atc_public_urls(payload.get("videoUrl"))
                + _atc_public_urls(payload.get("videoUrlList"))
                + _atc_public_urls(payload.get("video_url"))
                + _atc_public_urls(payload.get("video_urls"))
                + _atc_public_urls(video.get("url"))
                + _atc_public_urls(video.get("play_addr"))
                + _atc_public_urls(video.get("download_addr")))
        if not urls:
            raise ApiError(502, "暂未获取到可用的下载链接，请稍后重试")
        _atc_save_result(job["item_id"], {"videoUrl": urls[0]}, work_url=job["work_url"])
        return
    douyin = _is_douyin_work_url(job["work_url"])
    result = _atc_result_to_parse(job["work_url"], data, job["item_id"], allow_partial=douyin)
    if douyin:
        _complete_douyin_result(job["work_url"], result)


def _atc_try_job_fallback(job: dict) -> bool:
    """后台主媒体失败时也补充；保留来源供同源端点读取官方内存缓存。"""
    if job["purpose"] != "play" or not _is_douyin_work_url(job.get("work_url") or ""):
        return False
    try:
        result = _parse_douyin_share_direct(job["work_url"])
        if not _result_has_media(result):
            return False
        _atc_save_result(job["item_id"], {}, work_url=job["work_url"])
        return True
    except Exception:
        return False


def _atc_submit_claimed(job: dict, cfg: dict) -> None:
    now = int(time.time())
    if now - int(job["created"]) > ATC_JOB_TIMEOUT:
        _atc_claim_update(job, "failed", error="任务处理超时，请稍后重试")
        return
    completed_payload = False
    try:
        params = {"workUrl": job["work_url"]}
        if job["purpose"] == "transcript":
            params["taskType"] = "TEXT"
        resp = _atc_request("POST", "/video/extract", params, cfg)
        if resp.get("code") == 200 and resp.get("data"):
            created = resp["data"]
            if _atc_result_complete(created, include_text=job["purpose"] == "transcript"):
                completed_payload = True
                _atc_store_job_result(job, created)
                _atc_claim_update(job, "done", error=None)
                return
            task_id = _atc_task_id(created)
            if not task_id:
                raise _ParserServiceError(502, "视频解析结果暂不可用，请稍后重试")
            _atc_claim_update(job, "submitted", task_id=task_id, error=None)
            return
        raise _atc_rejected(resp, "任务提交")
    except Exception as exc:
        if isinstance(exc, _ParserServiceError) and exc.reason == "busy":
            # 明确拒绝受理才重新提交；连接中断不盲目重发，避免生成重复任务。
            _atc_claim_update(job, "pending", error="解析请求较多，排队重试中")
            return
        if not completed_payload and _atc_try_job_fallback(job):
            _atc_claim_update(job, "done", error=None)
            return
        _atc_claim_update(
            job, "failed",
            error=exc.message if isinstance(exc, ApiError) else "任务提交失败，请稍后重试")


def _atc_poll_claimed(job: dict, cfg: dict) -> None:
    now = int(time.time())
    completed_payload = False
    if now - int(job.get("created") or now) > ATC_JOB_TIMEOUT:
        _atc_claim_update(job, "failed", error="轮询超时")
        return
    try:
        resp = _atc_request(
            "GET", "/video/query", {"taskId": job["task_id"]}, cfg)
        if resp.get("code") != 200:
            raise _atc_rejected(resp, "任务查询")
        raw_data = resp.get("data") or {}
        if not isinstance(raw_data, dict):
            raise _ParserServiceError(502, "视频解析结果暂不可用，请稍后重试")
        if _atc_result_complete(raw_data, include_text=job["purpose"] == "transcript"):
            completed_payload = True
            _atc_store_job_result(job, raw_data)
            _atc_claim_update(job, "done", error=None)
        else:
            _atc_claim_update(job, "submitted", error=None)
    except Exception as exc:
        if completed_payload:
            _atc_claim_update(job, "failed", error="暂未获取到作品内容，请稍后重试")
            return
        if isinstance(exc, ApiError) and not getattr(exc, "retryable", False):
            if _atc_try_job_fallback(job):
                _atc_claim_update(job, "done", error=None)
                return
            _atc_claim_update(job, "failed", error=exc.message)
            return
        # 网络抖动可重查已有 taskId；鉴权、权益和明确失败不空转五分钟。
        _atc_claim_update(
            job, "submitted", error="任务查询暂不可用，稍后重试")


def _atc_worker_loop(worker_no: int) -> None:
    owner = f"{_atc_instance}:{worker_no}"
    while not _atc_stop.is_set():
        try:
            cfg = _atc_cfg()
            if not cfg["enabled"]:
                _atc_stop.wait(1)
                continue
            job = _atc_claim_pending(owner)
            if job:
                _atc_submit_claimed(job, cfg)
                continue
            job = _atc_claim_submitted(owner)
            if job:
                _atc_poll_claimed(job, cfg)
                continue
        except Exception:
            pass
        _atc_stop.wait(1)


def _atc_worker() -> None:
    """保留旧内部入口；新启停逻辑由 start/stop 统一管理。"""
    _atc_worker_loop(0)


def _prepare_atc_jobs() -> None:
    now = int(time.time())
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE atc_jobs SET status='pending',lease_owner=NULL,lease_until=NULL "
                "WHERE status='submitting' AND COALESCE(lease_until,0)<?", (now,))
            conn.execute(
                "UPDATE atc_jobs SET lease_owner=NULL,lease_until=NULL "
                "WHERE status='submitted' AND COALESCE(lease_until,0)<?", (now,))
            conn.execute(
                "UPDATE atc_jobs SET status='failed',error='轮询超时',updated=?,"
                "lease_owner=NULL,lease_until=NULL WHERE status IN "
                "('pending','submitting','submitted') AND created<?",
                (now, now - ATC_JOB_TIMEOUT))
            for row in conn.execute(
                    "SELECT j.quota_reservation_id,j.status FROM atc_jobs j "
                    "JOIN quota_reservations q ON q.id=j.quota_reservation_id "
                    "WHERE j.status IN ('done','failed') AND q.status='pending'").fetchall():
                _settle_quota_in_conn(conn, row[0], 1 if row[1] == "done" else 0)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _start_atc_workers(prepared: bool = False) -> None:
    with _atc_workers_guard:
        _atc_threads[:] = [thread for thread in _atc_threads if thread.is_alive()]
        if _atc_threads:
            return
        if not prepared:
            _prepare_atc_jobs()
        _atc_stop.clear()
        for index in range(ATC_WORKERS):
            _atc_threads.append(threading.Thread(
                target=_atc_worker_loop, args=(index,),
                name=f"atc-worker-{index}", daemon=False))
        for thread in _atc_threads:
            thread.start()


def _stop_atc_workers() -> None:
    with _atc_workers_guard:
        _atc_stop.set()
        threads = list(_atc_threads)
    deadline = time.monotonic() + ATC_SHUTDOWN_TIMEOUT
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)
    with _atc_workers_guard:
        _atc_threads[:] = [thread for thread in _atc_threads if thread.is_alive()]


def _atc_cleanup() -> int:
    """缓存按保留期清理；终态任务记录保留 7 天。由 _sweeper 调用。"""
    # 服务关闭或 worker 停止时，也必须释放超时文案的预留金额。
    _prepare_atc_jobs()
    now = int(time.time())
    n = db_exec("DELETE FROM atc_cache WHERE updated<?",
                (now - DATA_RETENTION_DAYS * 86400,), "rowcount") or 0
    n += db_exec("DELETE FROM atc_jobs WHERE status IN ('done','failed') AND updated<?",
                 (now - 7 * 86400,), "rowcount") or 0
    return n


def _public_parser_test_state() -> str:
    """历史测试结果也经过公开边界，旧数据库中的上游错误不得直接展示。"""
    try:
        stored = json.loads(app_setting("atc_test_state", "") or "{}")
    except (ValueError, TypeError):
        return ""
    if not isinstance(stored, dict) or not stored:
        return ""
    public = {key: stored[key] for key in
              ("state", "ms", "duration", "has_video", "has_text") if key in stored}
    if stored.get("error"):
        public["error"] = _public_error(stored["error"], "测试失败，请稍后重试")
    return json.dumps(public, ensure_ascii=False)


def _atc_status() -> dict:
    """后台状态面板数据（不含密钥本体）。"""
    cfg = _atc_cfg()
    today0 = _today() * 86400
    row = db_exec(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done, "
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed "
        "FROM atc_jobs WHERE created>=?", (today0,), "one")
    pending = db_exec(
        "SELECT COUNT(*) FROM atc_jobs "
        "WHERE status IN ('pending','submitting','submitted')", (), "one")[0]
    last_err = db_exec(
        "SELECT error FROM atc_jobs WHERE status='failed' AND error IS NOT NULL "
        "ORDER BY updated DESC LIMIT 1", (), "one")
    secret = cfg["secret"]
    return {
        "enabled": cfg["enabled"],
        "configured": bool(cfg["key"] and secret),
        "master_on": app_setting("atc_enabled", "1") == "1",
        "api_key": cfg["key"],
        "api_secret_masked": (secret[:3] + "****" + secret[-2:]) if len(secret) > 5 else "",
        "endpoint_managed": True,
        "play_enhance": cfg["play_enhance"],
        "transcript_enabled": cfg["transcript_enabled"],
        "transcript_daily": cfg["transcript_daily"],
        "url_ttl": cfg["url_ttl"],
        "play_priority": cfg["play_priority"],
        "queue_pending": pending,
        "today_total": int(row[0] or 0), "today_done": int(row[1] or 0),
        "today_failed": int(row[2] or 0),
        "last_error": _public_error(last_err[0]) if last_err and last_err[0] else "",
        "test": _public_parser_test_state(),
    }


# ---- 文案提取（注册用户专属，每日限额；缓存命中不扣次）----

def _atc_transcript_status(user_id: int) -> tuple[int, int, int]:
    """返回 (limit, used, remaining)。计数主体与网页解析配额隔离（atc: 前缀）。"""
    cfg = _atc_cfg()
    limit = cfg["transcript_daily"]
    row = db_exec("SELECT count FROM usage_daily WHERE day=? AND subject=?",
                  (_today(), f"atc:user:{user_id}"), "one")
    used = int(row[0]) if row else 0
    return limit, used, max(0, limit - used)


def _atc_transcript_reserve(user_id: int) -> dict:
    cfg = _atc_cfg()
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            reservation = _reserve_quota_in_conn(
                conn, _today(), [f"atc:user:{user_id}"], cfg["transcript_daily"],
                endpoint="atc_transcript")
            conn.commit()
            return reservation
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class AtcTranscriptBody(BaseModel):
    item_id: str = Field(default="", max_length=40)


@app.post("/api/atc/transcript", include_in_schema=False)
@app.post("/api/transcript", name="submit_transcript")
def api_atc_transcript(body: AtcTranscriptBody, request: Request):
    """提交文案提取。缓存命中秒回不扣次；否则扣一次额度并异步入队。"""
    cfg = _atc_cfg()
    if not (cfg["enabled"] and cfg["transcript_enabled"]):
        raise ApiError(404, "文案提取功能未开启")
    u = current_user(request)
    if not u:
        raise ApiError(401, "获取文案需要登录，注册后每天免费提取 "
                         f"{cfg['transcript_daily']} 次")
    item_id = (body.item_id or "").strip()[:40]
    if not item_id:
        raise ApiError(400, "缺少 item_id，请先解析作品")
    cached = _atc_cache_get(item_id)
    if cached and cached.get("text_content"):
        limit, used, remaining = _atc_transcript_status(u["id"])
        return {"state": "ready", "text": cached["text_content"],
                "audio_url": cached.get("audio_url") or "",
                "duration": cached.get("duration"), "cached": True,
                "remaining": remaining, "daily": limit}
    if db_exec("SELECT id FROM atc_jobs WHERE item_id=? AND purpose='transcript' "
               "AND status IN ('pending','submitting','submitted')", (item_id,), "one"):
        limit, used, remaining = _atc_transcript_status(u["id"])
        return {"state": "processing", "cached": False, "remaining": remaining, "daily": limit}
    reservation = _atc_transcript_reserve(u["id"])
    if not reservation["ok"]:
        raise (_quota_error(reservation["limit"], reservation) if reservation.get("insufficient_balance")
               else ApiError(429, f"今日文案提取次数已用完（每天 {reservation['limit']} 次）"))
    try:
        enqueued = _atc_enqueue(item_id, purpose="transcript", quota_reservation_id=reservation["id"])
    except Exception:
        release_quota(reservation)
        raise
    if not enqueued:
        # 竞态下可能已有同一文案任务，或任务仍在冷却期。不能把本次预占
        # 误结算成 processing，否则既没有新任务，额度也会永久扣除。
        active = db_exec(
            "SELECT id FROM atc_jobs WHERE item_id=? AND purpose='transcript' "
            "AND status IN ('pending','submitting','submitted')",
            (item_id,), "one")
        release_quota(reservation)
        if active:
            limit, used, remaining = _atc_transcript_status(u["id"])
            return {"state": "processing", "cached": False,
                    "remaining": remaining, "daily": limit}
        cached = _atc_cache_get(item_id)
        if cached and cached.get("text_content"):
            limit, used, remaining = _atc_transcript_status(u["id"])
            return {"state": "ready", "text": cached["text_content"], "cached": True,
                    "audio_url": cached.get("audio_url") or "", "duration": cached.get("duration"),
                    "remaining": remaining, "daily": limit}
        raise ApiError(503, "文案任务暂时无法提交，请稍后重试")
    # 由任务成功/失败终态原子结算；提交成功只预占，不提前收费。
    return {"state": "processing", "cached": False,
            "remaining": reservation["remaining"], "daily": reservation["limit"]}


@app.get("/api/atc/transcript", include_in_schema=False)
@app.get("/api/transcript", name="get_transcript")
def api_atc_transcript_get(item_id: str, request: Request):
    """轮询提取状态：ready / processing / none。"""
    cfg = _atc_cfg()
    if not (cfg["enabled"] and cfg["transcript_enabled"]):
        raise ApiError(404, "文案提取功能未开启")
    u = current_user(request)
    if not u:
        raise ApiError(401, "请先登录")
    limit, used, remaining = _atc_transcript_status(u["id"])
    item_id = (item_id or "").strip()[:40]
    cached = _atc_cache_get(item_id) if item_id else None
    if cached and cached.get("text_content"):
        return {"state": "ready", "text": cached["text_content"],
                "audio_url": cached.get("audio_url") or "",
                "duration": cached.get("duration"),
                "remaining": remaining, "daily": limit}
    if item_id and db_exec(
            "SELECT id FROM atc_jobs WHERE item_id=? AND purpose='transcript' "
            "AND status IN ('pending','submitting','submitted')",
            (item_id,), "one"):
        return {"state": "processing", "remaining": remaining, "daily": limit}
    failed = db_exec(
        "SELECT error FROM atc_jobs WHERE item_id=? AND status='failed' "
        "ORDER BY updated DESC LIMIT 1", (item_id,), "one") if item_id else None
    if failed:
        return {"state": "failed", "error": "提取失败，请稍后重试",
                "remaining": remaining, "daily": limit}
    return {"state": "none", "remaining": remaining, "daily": limit}


# ---------------------------------------------------------------- 公共 API

class ParseBody(BaseModel):
    text: str = Field(min_length=1, max_length=PARSE_TEXT_MAX)


def _quota_error(limit: int, reservation: Optional[dict] = None):
    if reservation and reservation.get("insufficient_balance"):
        return ApiError(402, "今日免费次数已用完，账户余额不足，请联系管理员充值后重试")
    daily = free_user_daily()
    account_hint = (f"注册登录后每天免费 {daily} 次，超出后可使用账户余额。" if daily
                    else "注册登录后可使用账户余额继续解析。")
    return ApiError(429, f"今日免费次数已用完（每天 {limit} 次）。{account_hint}")


@app.post("/api/parse")
def api_parse(body: ParseBody, request: Request):
    reservation = reserve_quota(request, 1, endpoint="parse")
    if not reservation["ok"]:
        raise _quota_error(reservation["limit"], reservation)
    try:
        data = _parse_cached(body.text)
    except Exception:
        release_quota(reservation)
        log_request(request, "web", body.text[:100], False)
        raise
    settle_quota(reservation, 1)
    log_request(request, "web", body.text[:100], True)
    return data


# ---------------------------------------------------------------- 开放 API v1（异步任务 + 计费）

def _extract_links(text: str) -> list:
    return _extract_supported_work_urls(text, 100)


API_JOB_LEASE_SECONDS = max(120, int(os.environ.get("API_JOB_LEASE_SECONDS", "600")))
API_JOB_HEARTBEAT_SECONDS = max(
    10, min(API_JOB_LEASE_SECONDS // 3,
            int(os.environ.get("API_JOB_HEARTBEAT_SECONDS", "30"))))
# 关闭服务不应把租约时长当作 join 超时；worker 会在下一次可中断等待时退出，
# 即使上游请求卡住，也只给进程一个有界的收尾窗口。
API_JOB_SHUTDOWN_TIMEOUT = _clamped_env_int(
    "API_JOB_SHUTDOWN_TIMEOUT", 35, 5, 120)
_JOB_TERMINAL = ("succeeded", "failed", "cancelled")
_job_stop = threading.Event()
_job_wakeup = queue.Queue(maxsize=1)
_job_threads: list[threading.Thread] = []
_job_workers_guard = threading.Lock()
_job_instance = "jw_" + secrets.token_urlsafe(9)


def _api_price_from_conn(conn) -> int:
    row = conn.execute(
        "SELECT v FROM app_settings WHERE k='api_price_cents'").fetchone()
    try:
        return max(0, int(row["v"] if row else "1"))
    except Exception:
        return 1


def _json_list(value) -> list:
    try:
        out = json.loads(value or "[]")
        return out if isinstance(out, list) else []
    except Exception:
        return []


def _job_item_result(row) -> Optional[dict]:
    item = dict(row)
    status = item.get("status")
    if status == "succeeded":
        try:
            data = json.loads(item.get("result") or "{}")
        except Exception:
            data = {}
        out = {"link": item.get("link") or "", "ok": True, "data": data}
        if item.get("error"):
            out["warning"] = _public_error(item["error"])
        return out
    if status in ("failed", "cancelled"):
        out = {"link": item.get("link") or "", "ok": False,
               "error": _public_error(item.get("error"), "解析失败，请稍后重试")}
        if status == "cancelled":
            out["code"] = "cancelled"
        return out
    return None


def _refresh_job_aggregate(conn, job_id: str, now: Optional[int] = None) -> None:
    """在调用者事务内，从 item 事实表重建 job 聚合；只在完成时写一次 results 快照。"""
    now = int(now or time.time())
    job = conn.execute("SELECT total FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        return
    agg = conn.execute(
        "SELECT "
        "SUM(CASE WHEN status IN ('succeeded','failed','cancelled') THEN 1 ELSE 0 END) done,"
        "SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) ok,"
        "COALESCE(SUM(CASE WHEN status='succeeded' THEN price_cents ELSE 0 END),0) cost,"
        "SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) running "
        "FROM job_items WHERE job_id=?", (job_id,)).fetchone()
    done = int(agg["done"] or 0)
    ok_n = int(agg["ok"] or 0)
    cost = int(agg["cost"] or 0)
    total = int(job["total"] or 0)
    if done >= total:
        status, finished = "done", now
        rows = conn.execute(
            "SELECT * FROM job_items WHERE job_id=? ORDER BY idx", (job_id,)).fetchall()
        results = [r for r in (_job_item_result(x) for x in rows) if r is not None]
        conn.execute(
            "UPDATE jobs SET status=?,done=?,ok=?,cost_cents=?,results=?,"
            "updated=?,finished=COALESCE(finished,?) WHERE id=?",
            (status, done, ok_n, cost, json.dumps(results, ensure_ascii=False),
             now, finished, job_id))
    else:
        status = "running" if int(agg["running"] or 0) or done else "pending"
        conn.execute(
            "UPDATE jobs SET status=?,done=?,ok=?,cost_cents=?,updated=?,finished=NULL "
            "WHERE id=?", (status, done, ok_n, cost, now, job_id))


def _claim_job_item(owner: str) -> Optional[dict]:
    """用数据库租约/CAS 领取一项；过期 running 可恢复，但绝不再次预扣。"""
    now = int(time.time())
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT ji.*,j.key,j.user_id FROM job_items ji "
                "JOIN jobs j ON j.id=ji.job_id "
                "WHERE ji.reserved=1 AND j.status IN ('pending','running') AND "
                "(ji.status IN ('pending','reserved') OR "
                "(ji.status='running' AND COALESCE(ji.lease_until,0)<?)) "
                # 持久性异常导致某项重复出租约时，先让未重试项继续前进，避免队首饥饿。
                "ORDER BY COALESCE(ji.attempts,0),j.created,ji.idx LIMIT 1",
                (now,)).fetchone()
            if not row:
                conn.rollback()
                return None
            n = conn.execute(
                "UPDATE job_items SET status='running',lease_owner=?,lease_until=?,"
                "attempts=COALESCE(attempts,0)+1,started=COALESCE(started,?) "
                "WHERE job_id=? AND idx=? AND reserved=1 AND "
                "(status IN ('pending','reserved') OR "
                "(status='running' AND COALESCE(lease_until,0)<?))",
                (owner, now + API_JOB_LEASE_SECONDS, now,
                 row["job_id"], row["idx"], now)).rowcount
            if n != 1:
                conn.rollback()
                return None
            conn.execute(
                "UPDATE jobs SET status='running',updated=? "
                "WHERE id=? AND status<>'done'", (now, row["job_id"]))
            conn.commit()
            item = dict(row)
            item["lease_owner"] = owner
            item["attempts"] = int(row["attempts"] or 0) + 1
            return item
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _release_job_lease(item: dict) -> None:
    """本进程遇到临时内部错误时立即让出租约，避免 heartbeat 把孤儿项永久续租。"""
    db_exec(
        "UPDATE job_items SET lease_until=0 WHERE job_id=? AND idx=? "
        "AND status='running' AND lease_owner=?",
        (item["job_id"], item["idx"], item["lease_owner"]))
    _wake_job_workers()


def _finish_job_item(item: dict, ok: bool, data: Optional[dict] = None,
                     error_message: str = "") -> bool:
    """CAS 完成 item，并在同一事务内扣 reserved/入 spent 或精确退款、写账本和日志。"""
    now = int(time.time())
    terminal = "succeeded" if ok else "failed"
    result_json = json.dumps(data or {}, ensure_ascii=False) if ok else None
    error_message = _public_error(error_message) if error_message else ""
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT ji.*,j.key,j.user_id FROM job_items ji "
                "JOIN jobs j ON j.id=ji.job_id "
                "WHERE ji.job_id=? AND ji.idx=? AND ji.status='running' "
                "AND ji.reserved=1 AND ji.lease_owner=?",
                (item["job_id"], item["idx"], item["lease_owner"])).fetchone()
            if not row:
                conn.rollback()       # 租约已被别的 worker 接管或已经结算
                return False
            changed = conn.execute(
                "UPDATE job_items SET status=?,reserved=0,result=?,error=?,finished=?,"
                "lease_owner=NULL,lease_until=NULL WHERE job_id=? AND idx=? "
                "AND status='running' AND reserved=1 AND lease_owner=?",
                (terminal, result_json, error_message or None, now,
                 row["job_id"], row["idx"], item["lease_owner"])).rowcount
            if changed != 1:
                conn.rollback()
                return False

            price = max(0, int(row["price_cents"] or 0))
            if ok:
                account_changed = conn.execute(
                    "UPDATE api_keys SET reserved_cents=reserved_cents-?,"
                    "spent_cents=spent_cents+?,calls=calls+1,last_used=? "
                    "WHERE key=? AND COALESCE(reserved_cents,0)>=?",
                    (price, price, now, row["key"], price)).rowcount
                event, balance_delta, reserved_delta = "charge", 0, -price
                spent_delta, calls_delta, reason = price, 1, "parse_succeeded"
            else:
                account_changed = conn.execute(
                    "UPDATE api_keys SET reserved_cents=reserved_cents-?,"
                    "balance_cents=balance_cents+? "
                    "WHERE key=? AND COALESCE(reserved_cents,0)>=?",
                    (price, price, row["key"], price)).rowcount
                event, balance_delta, reserved_delta = "refund", price, -price
                spent_delta, calls_delta, reason = 0, 0, "parse_failed"
            if account_changed != 1:
                raise RuntimeError("API 预授权账户不存在或 reserved_cents 对账失败")

            conn.execute(
                "INSERT INTO api_ledger("
                "ts,key,job_id,item_idx,event,balance_delta,reserved_delta,"
                "spent_delta,calls_delta,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, row["key"], row["job_id"], row["idx"], event,
                 balance_delta, reserved_delta, spent_delta, calls_delta, reason))
            conn.execute(
                "INSERT INTO api_logs("
                "ts,key,user_id,link,ok,cost_cents,job_id,item_idx"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (now, row["key"], row["user_id"], row["link"], 1 if ok else 0,
                 price if ok else 0, row["job_id"], row["idx"]))
            _refresh_job_aggregate(conn, row["job_id"], now)
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _run_claimed_job_item(item: dict) -> None:
    try:
        data = _parse_cached(item["link"])
    except ApiError as e:
        _finish_job_item(item, False, error_message=e.message)
    except Exception as e:
        _finish_job_item(
            item, False,
            error_message="解析暂时失败，请稍后重试")
    else:
        _finish_job_item(item, True, data=data)


def _wake_job_workers() -> None:
    try:
        _job_wakeup.put_nowait(True)
    except queue.Full:
        pass


def _job_worker_loop(worker_no: int) -> None:
    owner = f"{_job_instance}:{worker_no}"
    while not _job_stop.is_set():
        try:
            item = _claim_job_item(owner)
        except Exception:
            _job_stop.wait(1)
            continue
        if item is None:
            try:
                _job_wakeup.get(timeout=1)
            except queue.Empty:
                pass
            continue
        try:
            _run_claimed_job_item(item)
        except Exception:
            # 数据库瞬时故障时不改变预授权；释放租约后由本/下一进程重试。
            try:
                _release_job_lease(item)
            except Exception:
                pass
            _job_stop.wait(0.5)


def _job_heartbeat_loop() -> None:
    owner_prefix = _job_instance + ":"
    while not _job_stop.wait(API_JOB_HEARTBEAT_SECONDS):
        try:
            db_exec(
                "UPDATE job_items SET lease_until=? WHERE status='running' "
                "AND substr(lease_owner,1,?)=?",
                (int(time.time()) + API_JOB_LEASE_SECONDS,
                 len(owner_prefix), owner_prefix))
        except Exception:
            pass


def _recover_legacy_api_jobs() -> dict:
    """一次性终结旧 daemon 遗留任务；无法证明的至多一笔预扣按用户有利原则退款。"""
    now = int(time.time())
    recovered = refunded = 0
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            legacy = conn.execute(
                "SELECT j.* FROM jobs j WHERE j.status<>'done' AND NOT EXISTS "
                "(SELECT 1 FROM job_items ji WHERE ji.job_id=j.id) "
                "ORDER BY j.created").fetchall()
            current_price = _api_price_from_conn(conn)
            for job in legacy:
                links = [str(x) for x in _json_list(job["links"])]
                old_results = _json_list(job["results"])
                logs = conn.execute(
                    "SELECT * FROM api_logs WHERE job_id=? ORDER BY id",
                    (job["id"],)).fetchall()
                total = max(int(job["total"] or 0), len(links),
                            len(old_results), len(logs))
                positive_prices = [int(x["cost_cents"] or 0) for x in logs
                                   if int(x["cost_cents"] or 0) > 0]
                fallback_price = max(
                    [int(job["price_cents"] or 0), current_price, *positive_prices])
                built = []
                for idx in range(total):
                    prior = old_results[idx] if idx < len(old_results) else None
                    log = logs[idx] if idx < len(logs) else None
                    link = (links[idx] if idx < len(links) else
                            (prior.get("link", "") if isinstance(prior, dict) else
                             (log["link"] if log else "")))
                    if isinstance(prior, dict):
                        succeeded = bool(prior.get("ok"))
                        result = prior.get("data") if succeeded else None
                        err = "" if succeeded else str(prior.get("error") or "解析失败")
                    elif log is not None:
                        succeeded = bool(log["ok"])
                        result = {} if succeeded else None
                        err = ("旧任务已计费，但结果在重启前未完整落库"
                               if succeeded else "旧任务解析失败")
                    else:
                        succeeded, result = False, None
                        err = "服务升级时任务尚未完成，已取消并执行保守退款"
                    status = "succeeded" if succeeded else (
                        "failed" if log is not None or prior is not None else "cancelled")
                    item_price = (int(log["cost_cents"] or 0)
                                  if succeeded and log is not None else fallback_price)
                    conn.execute(
                        "INSERT OR IGNORE INTO job_items("
                        "job_id,idx,link,status,price_cents,reserved,result,error,"
                        "attempts,started,finished) VALUES(?,?,?,?,?,0,?,?,0,?,?)",
                        (job["id"], idx, link, status, item_price,
                         json.dumps(result or {}, ensure_ascii=False) if succeeded else None,
                         err or None, job["created"], now))
                    if log is not None and log["item_idx"] is None:
                        conn.execute(
                            "UPDATE api_logs SET item_idx=? WHERE id=? AND item_idx IS NULL",
                            (idx, log["id"]))
                    built.append(_job_item_result({
                        "link": link, "status": status,
                        "result": (json.dumps(result or {}, ensure_ascii=False)
                                   if succeeded else None),
                        "error": err or None,
                    }))

                # 旧执行器逐项串行，同一 job 在崩溃点至多有一笔“已扣余额但未结算”。
                # 旧 schema 没有证据能区分它是否发生，故只做一次、偏向用户的安全退款。
                key_row = conn.execute(
                    "SELECT 1 FROM api_keys WHERE key=?", (job["key"],)).fetchone()
                if key_row and fallback_price > 0:
                    conn.execute(
                        "UPDATE api_keys SET balance_cents=balance_cents+? WHERE key=?",
                        (fallback_price, job["key"]))
                    conn.execute(
                        "INSERT INTO api_ledger("
                        "ts,key,job_id,event,balance_delta,reason"
                        ") VALUES(?,?,?,?,?,?)",
                        (now, job["key"], job["id"], "legacy_safety_refund",
                         fallback_price, "legacy_daemon_state_ambiguous"))
                    refunded += fallback_price
                conn.execute(
                    "UPDATE jobs SET total=?,status='done',updated=?,finished=?,"
                    "results=? WHERE id=?",
                    (total, now, now,
                     json.dumps([x for x in built if x], ensure_ascii=False), job["id"]))
                _refresh_job_aggregate(conn, job["id"], now)
                recovered += 1
            conn.execute(
                "INSERT OR REPLACE INTO app_settings(k,v) "
                "VALUES('api_jobs_v2_legacy_recovered',?)", (str(now),))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return {"jobs": recovered, "refunded_cents": refunded}


def _reconcile_api_job_accounts() -> dict:
    """启动对账：item 是预授权事实源；差异只按“不让用户少余额”的方向修复并记账。"""
    now = int(time.time())
    repaired = 0
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            keys = conn.execute(
                "SELECT key,COALESCE(balance_cents,0) balance_cents,"
                "COALESCE(reserved_cents,0) reserved_cents FROM api_keys").fetchall()
            for key_row in keys:
                expected_row = conn.execute(
                    "SELECT COALESCE(SUM(ji.price_cents),0) n FROM job_items ji "
                    "JOIN jobs j ON j.id=ji.job_id WHERE j.key=? AND ji.reserved=1 "
                    "AND ji.status IN ('pending','reserved','running')",
                    (key_row["key"],)).fetchone()
                expected = int(expected_row["n"] or 0)
                actual = int(key_row["reserved_cents"] or 0)
                if actual > expected:
                    delta = actual - expected
                    conn.execute(
                        "UPDATE api_keys SET reserved_cents=?,balance_cents=balance_cents+? "
                        "WHERE key=?", (expected, delta, key_row["key"]))
                    conn.execute(
                        "INSERT INTO api_ledger("
                        "ts,key,event,balance_delta,reserved_delta,reason"
                        ") VALUES(?,?,?,?,?,?)",
                        (now, key_row["key"], "reconcile_refund",
                         delta, -delta, "orphan_reserved_surplus"))
                    repaired += 1
                elif actual < expected:
                    # 正常事务不可能走到这里；若磁盘/人工改库造成差额，补足 reserved
                    # 而不再扣 available，避免恢复过程让用户二次付费。
                    delta = expected - actual
                    conn.execute(
                        "UPDATE api_keys SET reserved_cents=? WHERE key=?",
                        (expected, key_row["key"]))
                    conn.execute(
                        "INSERT INTO api_ledger("
                        "ts,key,event,reserved_delta,reason) VALUES(?,?,?,?,?)",
                        (now, key_row["key"], "reconcile_reserve",
                         delta, "missing_reserve_repaired_user_favor"))
                    repaired += 1
            job_ids = conn.execute(
                "SELECT DISTINCT job_id FROM job_items").fetchall()
            for row in job_ids:
                _refresh_job_aggregate(conn, row["job_id"], now)
            conn.execute(
                "INSERT OR REPLACE INTO app_settings(k,v) "
                "VALUES('api_jobs_last_reconciled',?)", (str(now),))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return {"accounts_repaired": repaired}


def _prepare_api_jobs() -> None:
    """启动线程前完成会依赖旧 api_logs 的恢复与账户对账。"""
    _recover_legacy_api_jobs()
    _reconcile_api_job_accounts()


def _start_api_job_workers(prepared: bool = False) -> None:
    with _job_workers_guard:
        _job_threads[:] = [t for t in _job_threads if t.is_alive()]
        if any(t.is_alive() for t in _job_threads):
            return
        if not prepared:
            _prepare_api_jobs()
        _job_stop.clear()
        while True:
            try:
                _job_wakeup.get_nowait()
            except queue.Empty:
                break
        heartbeat = threading.Thread(
            target=_job_heartbeat_loop, name="api-job-heartbeat", daemon=False)
        _job_threads.append(heartbeat)
        for i in range(API_JOB_WORKERS):
            _job_threads.append(threading.Thread(
                target=_job_worker_loop, args=(i,),
                name=f"api-job-worker-{i}", daemon=False))
        for thread in _job_threads:
            thread.start()
        _wake_job_workers()


def _stop_api_job_workers() -> None:
    with _job_workers_guard:
        _job_stop.set()
        _wake_job_workers()
        threads = list(_job_threads)
    deadline = time.monotonic() + API_JOB_SHUTDOWN_TIMEOUT
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)
    with _job_workers_guard:
        _job_threads[:] = [t for t in _job_threads if t.is_alive()]


class JobBody(BaseModel):
    links: list[str] = Field(default_factory=list, max_length=100)
    text: str = Field(default="", max_length=BATCH_TEXT_MAX)


def _api_key_from(request: Request) -> str:
    # API Key 是长期计费凭据，只允许请求头；查询参数会泄露到 URL 历史和代理访问日志。
    return request.headers.get("X-API-Key") or ""


@app.post("/api/v1/jobs")
def api_v1_create_job(body: JobBody, request: Request):
    """原子预授权整批费用并持久化 item；Idempotency-Key 重放返回同一任务。"""
    key = _api_key_from(request)
    rec, err = api_key_check(key)
    if err:
        raise ApiError(401, err)
    sources = list(body.links or []) or [body.text]
    links, seen = [], set()
    for source in sources:
        for link in _extract_supported_work_urls(str(source), 100):
            if link in seen:
                continue
            seen.add(link)
            links.append(link)
            if len(links) >= 100:
                break
        if len(links) >= 100:
            break
    if not links:
        raise ApiError(400, "links 为空或没有受支持平台的公开作品链接")
    request_id = (request.headers.get("Idempotency-Key") or "").strip() or None
    if request_id and len(request_id) > 128:
        raise ApiError(400, "Idempotency-Key 最长 128 个字符")
    links_json = json.dumps(links, ensure_ascii=False)
    now = int(time.time())
    replay = None
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if request_id:
                replay = conn.execute(
                    "SELECT * FROM jobs WHERE key=? AND request_id=?",
                    (key, request_id)).fetchone()
                if replay:
                    if _json_list(replay["links"]) != links:
                        raise ApiError(409, "同一 Idempotency-Key 不能提交不同链接")
                    conn.commit()
                else:
                    replay = None
            if not replay:
                key_row = conn.execute(
                    "SELECT * FROM api_keys WHERE key=?", (key,)).fetchone()
                if (not key_row or not key_row["enabled"]
                        or key_row["deleted_at"] is not None):
                    raise ApiError(401, "无效或已禁用的 API Key")
                price = _api_price_from_conn(conn)
                total_cost = price * len(links)
                if int(key_row["balance_cents"] or 0) < total_cost:
                    raise ApiError(
                        402, f"余额不足（当前 {key_row['balance_cents']} 分，"
                        f"本任务需预授权 {total_cost} 分），请充值")
                job_id = "job_" + secrets.token_urlsafe(12)
                conn.execute(
                    "UPDATE api_keys SET balance_cents=balance_cents-?,"
                    "reserved_cents=COALESCE(reserved_cents,0)+? WHERE key=?",
                    (total_cost, total_cost, key))
                conn.execute(
                    "INSERT INTO jobs("
                    "id,key,user_id,status,total,done,ok,cost_cents,links,results,"
                    "created,price_cents,updated,request_id"
                    ") VALUES(?,?,?,?,?,0,0,0,?,'[]',?,?,?,?)",
                    (job_id, key, key_row["user_id"], "pending", len(links),
                     links_json, now, price, now, request_id))
                for idx, link in enumerate(links):
                    conn.execute(
                        "INSERT INTO job_items("
                        "job_id,idx,link,status,price_cents,reserved,attempts"
                        ") VALUES(?,?,?,'reserved',?,1,0)",
                        (job_id, idx, link, price))
                    conn.execute(
                        "INSERT INTO api_ledger("
                        "ts,key,job_id,item_idx,event,balance_delta,reserved_delta,reason"
                        ") VALUES(?,?,?,?,?,?,?,?)",
                        (now, key, job_id, idx, "reserve",
                         -price, price, "job_pre_authorized"))
                conn.commit()
                replay = conn.execute(
                    "SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    _wake_job_workers()
    j = dict(replay)
    return {"code": 0, "message": "accepted", "data": {
        "job_id": j["id"], "total": j["total"], "status": j["status"],
        "price_cents": j["price_cents"],
        "estimated_cost_cents": int(j["price_cents"] or 0) * int(j["total"] or 0),
        "query_url": f"/api/v1/jobs/{j['id']}"}}


@app.get("/api/v1/jobs/{job_id}")
def api_v1_get_job(job_id: str, request: Request):
    """查询任务结果。需带同一 API Key。"""
    rec, err = api_key_check(_api_key_from(request))
    if err:
        raise ApiError(401, err)
    with _db_lock:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id=? AND key=?",
                (job_id, rec["key"])).fetchone()
            if not row:
                raise ApiError(404, "任务不存在或无权访问")
            items = conn.execute(
                "SELECT * FROM job_items WHERE job_id=? ORDER BY idx",
                (job_id,)).fetchall()
        finally:
            conn.close()
    j = dict(row)
    if items:
        terminal = [x for x in items if x["status"] in _JOB_TERMINAL]
        done = len(terminal)
        ok_n = sum(1 for x in terminal if x["status"] == "succeeded")
        cost = sum(int(x["price_cents"] or 0) for x in terminal
                   if x["status"] == "succeeded")
        status = ("done" if done >= int(j["total"] or 0) else
                  ("running" if done or any(x["status"] == "running" for x in items)
                   else "pending"))
        results = [r for r in (_job_item_result(x) for x in items) if r is not None]
    else:                       # v1 已完成任务仍可按旧 results 快照查询
        done, ok_n, cost, status = j["done"], j["ok"], j["cost_cents"], j["status"]
        results = _json_list(j["results"])
        for result in results:
            if isinstance(result, dict):
                for key in ("error", "warning"):
                    if result.get(key):
                        result[key] = _public_error(result[key])
    return {"code": 0, "message": "ok", "data": {
        "job_id": j["id"], "status": status, "total": j["total"],
        "done": done, "ok": ok_n, "cost_cents": cost,
        "created": j["created"], "finished": j["finished"],
        "results": results}}


@app.get("/api/v1/balance")
def api_v1_balance(request: Request):
    """查询当前 Key 的余额与用量。"""
    rec, err = api_key_check(_api_key_from(request))
    if err:
        raise ApiError(401, err)
    return {"code": 0, "data": {
        "balance_cents": rec["balance_cents"], "spent_cents": rec["spent_cents"],
        "reserved_cents": int(rec.get("reserved_cents") or 0),
        "calls": rec["calls"], "price_cents": api_price_cents()}}


def _fetch_user_info(sec_uid: str) -> dict:
    """经代理拉取作者主页统计（免签名 reflow 接口）：粉丝数、获赞数、作品数等。"""
    safe_sec_uid = urlparse.quote(str(sec_uid or "")[:200], safe="")
    url = f"https://www.iesdouyin.com/web/api/v2/user/info/?sec_uid={safe_sec_uid}"
    # 浏览器无法跨域取（抖音接口无 CORS），只能服务器代拉 —— 走代理，不暴露服务器 IP
    resp, _ = open_url(url, headers={"Referer": "https://www.iesdouyin.com/"}, timeout=10)
    try:
        ui = (json.loads(resp.read(1024 * 1024).decode("utf-8", "ignore")) or {}).get("user_info") or {}
    finally:
        try:
            resp.close()
        except Exception:
            pass
    if not isinstance(ui, dict) or not ui:
        raise ApiError(503, "作者信息暂时无法补全，请稍后重试")
    return {
        "follower_count": _douyin_number(_douyin_first(ui, "follower_count", "mplatform_followers_count")),
        "total_favorited": _douyin_number(ui.get("total_favorited")),
        "following_count": _douyin_number(ui.get("following_count")),
        "aweme_count": _douyin_number(ui.get("aweme_count")),
        "douyin_id": ui.get("unique_id") or "",
        "signature": (ui.get("signature") or "").strip(),
    }


@app.get("/api/author")
def api_author(item_id: str):
    """作者结构化详情（供前端悬停浮层）。

    基础字段来自解析时缓存的分享页 author 对象；首次请求时再经服务端出站策略拉一次
    user/info 富化粉丝数/获赞数（分享页不给这两项），结果服务端缓存 10 分钟。
    注：抖音该接口无 CORS/JSONP，浏览器无法跨域直取，故由服务端代拉。
    """
    hit = _author_cache.get(item_id)
    if not hit:
        snapshot = _get_parse_snapshot(item_id)
        if snapshot and snapshot.get("author_detail"):
            return snapshot["author_detail"]
        raise ApiError(404, "作者信息不存在或已过期，请重新解析该视频")
    detail = hit[1]
    if detail.get("enriched") and time.time() - hit[0] < 600:
        _save_author_snapshot(item_id, detail)
        return detail
    sec = detail.get("sec_uid")
    if sec:
        try:
            merged = _merge_missing_fields(detail, _fetch_user_info(sec))
            for key in ("follower_count", "total_favorited", "following_count", "aweme_count"):
                merged[key] = _douyin_number(merged.get(key))
            merged["enriched"] = True
            _author_cache[item_id] = (time.time(), merged)
            _save_author_snapshot(item_id, merged)
            return merged
        except Exception:
            pass                         # 富化失败就返回基础字段，不影响头像浮层
    _save_author_snapshot(item_id, detail)
    return detail


class BatchBody(BaseModel):
    text: str = Field(min_length=1, max_length=BATCH_TEXT_MAX)


@app.post("/api/parse/batch")
def api_parse_batch(body: BatchBody, request: Request):
    """批量解析：每条链接算一次配额，超出今日免费额度的部分不解析。"""
    uniq = _extract_supported_work_urls(body.text, 50)
    if not uniq:
        raise ApiError(400, "未找到任何受支持平台的公开作品链接")

    uniq = uniq[:50]
    reservation = reserve_quota(
        request, len(uniq), partial=True, endpoint="parse_batch")
    if reservation["reserved"] <= 0:
        raise _quota_error(reservation["limit"], reservation)
    limit = reservation["limit"]
    process = uniq[:reservation["reserved"]]
    over = uniq[reservation["reserved"]:]   # 超额部分不解析

    out, spent = [], 0
    for l in process:
        try:
            out.append({"ok": True, "link": l, "data": _parse_cached(l)})
            spent += 1
            log_request(request, "web", l, True)
        except ApiError as e:
            out.append({"ok": False, "link": l, "error": e.message})
            log_request(request, "web", l, False)
        except Exception:
            out.append({"ok": False, "link": l, "error": "内部解析错误，请稍后重试"})
            log_request(request, "web", l, False)
    for l in over:
        out.append({"ok": False, "link": l,
                    "error": ("免费次数及账户余额不足，未解析" if reservation.get("insufficient_balance")
                              else f"今日免费次数不足未解析（每天 {limit} 次，登录后 {free_user_daily()} 次）")})
    settle_quota(reservation, spent)
    remaining = quota_status(request)[2]
    return {"count": len(out), "results": out,
            "quota": {"limit": limit, "remaining": remaining}}


class ExportBody(BaseModel):
    items: list[dict] = Field(max_length=EXPORT_ITEMS_MAX)


def _xlsx_safe_text(value, limit: int = 4096) -> str:
    """把外部文本强制为 Excel 纯文本，防止 =、+、-、@ 公式注入。"""
    text = str(value or "").replace("\x00", "")[:max(0, int(limit))]
    candidate = text.lstrip(" \t\r\n")
    if candidate.startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r")):
        return "'" + text
    return text


def _xlsx_safe_number(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value if math.isfinite(float(value)) else ""
    return _xlsx_safe_text(value, 64)


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
        d = d if isinstance(d, dict) else {}
        is_note = d.get("kind") == "note"
        st = d.get("stats") if isinstance(d.get("stats"), dict) else {}
        v = d.get("video") if isinstance(d.get("video"), dict) else {}
        ct = d.get("create_time")
        try:
            ct_value = int(ct or 0)
            cts = time.strftime("%Y-%m-%d %H:%M", time.localtime(ct_value)) \
                if ct_value > 0 else ""
        except (TypeError, ValueError, OverflowError, OSError):
            cts = ""
        images = d.get("images") if isinstance(d.get("images"), list) else []
        media = (" | ".join(
            str(im.get("url") or "") for im in images if isinstance(im, dict))
                 if is_note else v.get("url", ""))
        res = f"{v.get('width')}×{v.get('height')}" if v.get("width") else ""
        try:
            duration = float(d.get("duration_ms") or 0)
            dur = round(duration / 1000, 1) if math.isfinite(duration) and not is_note else ""
        except (TypeError, ValueError, OverflowError):
            dur = ""
        tags = d.get("tags") if isinstance(d.get("tags"), list) else []
        music = d.get("music") if isinstance(d.get("music"), dict) else {}
        ws.append([
            i, "图集" if is_note else "视频",
            _xlsx_safe_text(d.get("title", "")),
            _xlsx_safe_text(d.get("author", ""), 500),
            _xlsx_safe_text(d.get("item_id", ""), 128), dur,
            _xlsx_safe_text(res, 64),
            _xlsx_safe_number(st.get("digg")),
            _xlsx_safe_number(st.get("comment")),
            _xlsx_safe_number(st.get("collect")),
            _xlsx_safe_number(st.get("share")),
            cts, _xlsx_safe_text(" ".join("#" + str(t) for t in tags)),
            _xlsx_safe_text(music.get("title") or "", 500),
            _xlsx_safe_text(d.get("location") or "", 500),
            _xlsx_safe_text(media),
            _xlsx_safe_text(d.get("author_url", "")),
            _xlsx_safe_text(d.get("_link", "")),
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


def _close_upstream(resp) -> None:
    try:
        resp.close()
    except Exception:
        pass


def _open_video_upstream(vid: str, headers: dict, validator=None):
    """打开一个可信的视频响应；主线路异常时切到备用播放域名。"""
    no_proxy_error = None
    for upstream in (_play_api(vid), _play_api_alt(vid)):
        resp = None
        accepted = False
        try:
            resp, _ = open_url(
                upstream,
                headers=headers,
                retry_http_statuses=(408, 425, 429, 500, 502, 503, 504),
                ban_on_auth_error=False,
            )
            status = resp.status if hasattr(resp, "status") else resp.getcode()
            if status not in (200, 206):
                raise ValueError(f"unexpected video status {status}")

            content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0]
            content_type = content_type.strip().lower()
            if not content_type or not (
                content_type.startswith("video/")
                or content_type in {
                    "application/mp4",
                    "application/octet-stream",
                    "binary/octet-stream",
                }
            ):
                raise ValueError(f"unexpected video content type {content_type}")

            geturl = getattr(resp, "geturl", None)
            final_url = geturl() if callable(geturl) else ""
            if final_url and not _host_allowed(final_url):
                raise ValueError("video redirect left the Douyin media allowlist")
            if validator:
                validator(resp)

            accepted = True
            return resp
        except ApiError as exc:
            if exc.status == 503:
                no_proxy_error = exc
                break
        except urlerr.HTTPError as exc:
            _close_upstream(exc)
        except Exception:
            pass
        finally:
            if resp is not None and not accepted:
                _close_upstream(resp)

    if no_proxy_error:
        raise no_proxy_error
    raise ApiError(502, "视频下载线路暂时不可用，请稍后重试")


def _open_atc_video_upstream(item_id: str, headers: dict, validator=None):
    """先读主媒体缓存；缺失、过期或不可用时按已保存的抖音来源补充。"""
    try:
        return _open_primary_video_upstream(item_id, headers, validator)
    except ApiError as primary_error:
        if primary_error.status == 416:
            raise
        work_url = _saved_parse_source(item_id)
        if not work_url and re.fullmatch(r"\d{8,30}", item_id):
            work_url = _douyin_work_url("video", item_id)
        if not _is_douyin_work_url(work_url):
            raise
        _, official_id, _ = _douyin_resolve_share_url(work_url)
        return _open_douyin_video_upstream(official_id, headers, validator)


def _open_primary_video_upstream(item_id: str, headers: dict, validator=None):
    """只读取主服务缓存的 URL，限定抖音与 TikTok 媒体域名。"""
    cached = _atc_cache_get(item_id)
    if not _atc_url_fresh(cached, _atc_cfg()["url_ttl"]):
        raise ApiError(403, "媒体地址已过期，请重新解析或刷新页面")
    upstream = cached["video_url"]
    if not _primary_media_allowed(upstream):
        raise ApiError(502, "视频线路未通过安全校验")
    resp = None
    accepted = False
    try:
        headers = dict(headers)
        if _atc_platform_for_url(cached.get("work_url") or "") == "tiktok":
            headers["Referer"] = "https://www.tiktok.com/"
        resp, _ = open_url(
            upstream, headers=headers,
            retry_http_statuses=(408, 425, 429, 500, 502, 503, 504),
            ban_on_auth_error=False)
        status = resp.status if hasattr(resp, "status") else resp.getcode()
        if status not in (200, 206):
            raise ValueError(f"unexpected video status {status}")
        content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0]
        content_type = content_type.strip().lower()
        if not content_type or not (
                content_type.startswith("video/") or content_type in {
                    "application/mp4", "application/octet-stream",
                    "binary/octet-stream"}):
            raise ValueError(f"unexpected video content type {content_type}")
        geturl = getattr(resp, "geturl", None)
        final_url = geturl() if callable(geturl) else ""
        if final_url and not _primary_media_allowed(final_url):
            raise ValueError("video redirect left the media allowlist")
        if validator:
            validator(resp)
        accepted = True
        return resp
    except ApiError:
        raise
    except Exception:
        raise ApiError(502, "视频线路暂时不可用，请稍后重试")
    finally:
        if resp is not None and not accepted:
            _close_upstream(resp)


def _douyin_media_for_item(item_id: str, refresh: bool = False) -> Optional[dict]:
    """返回一条仍在有效期内的官方 CDN 地址；需要时重新打开官方网页。"""
    if not refresh:
        cached = _douyin_cached_media(item_id)
        if cached:
            return cached
    # 解析函数带按 item 锁，多个 Range/下载请求不会重复启动浏览器。
    try:
        result = _parse_douyin_item_direct("video", item_id)
    except Exception:
        return None
    cached = _douyin_cached_media(item_id)
    if cached:
        return cached
    direct = ((result.get("video") or {}).get("direct_url")
              or (result.get("video") or {}).get("url") or "")
    direct = _douyin_public_url(direct)
    if direct:
        return _douyin_cache_media(item_id, [direct])
    return None


def _invalidate_douyin_media(item_id: str) -> None:
    with _douyin_media_lock:
        _douyin_media_cache.pop(str(item_id), None)


def _invalidate_douyin_note_media(item_id: str) -> None:
    with _douyin_media_lock:
        _douyin_note_media_cache.pop(str(item_id), None)


def _douyin_note_media_for_item(item_id: str,
                                refresh: bool = False) -> Optional[dict]:
    """返回一组仍有效的官方图片地址，过期时惰性重抓。"""
    if not refresh:
        cached = _douyin_cached_note_media(item_id)
        if cached:
            return cached
    try:
        _parse_douyin_item_direct("note", item_id)
    except Exception:
        return None
    return _douyin_cached_note_media(item_id)


def _open_douyin_image_upstream(item_id: str, index: int):
    """只从 item_id 对应的短时内存缓存取图，失效时刷新一次。"""
    def attempt(record):
        urls = list((record or {}).get("urls") or [])
        if index < 1 or index > len(urls):
            return None
        upstream = _douyin_public_url(urls[index - 1])
        if not upstream:
            return None
        resp = None
        accepted = False
        try:
            kwargs = {
                "headers": {**CDN_HEADERS, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"},
                "retry_http_statuses": (408, 425, 429, 500, 502, 503, 504),
                "ban_on_auth_error": False,
            }
            pinned_proxy = _douyin_browser_proxy_for_item(item_id)
            if pinned_proxy:
                kwargs["proxy_override"] = pinned_proxy
            resp, _ = open_url(upstream, **kwargs)
            status = resp.status if hasattr(resp, "status") else resp.getcode()
            if status != 200:
                raise ValueError(f"unexpected image status {status}")
            content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0]
            content_type = content_type.strip().lower()
            if not content_type.startswith("image/"):
                raise ValueError("unexpected image content type")
            raw_length = str(resp.headers.get("Content-Length") or "")
            if raw_length.isdigit() and int(raw_length) > IMAGE_MAX_BYTES:
                raise ApiError(413, "图片文件过大")
            geturl = getattr(resp, "geturl", None)
            final_url = geturl() if callable(geturl) else ""
            if final_url and not _host_allowed(final_url):
                raise ValueError("image redirect left the Douyin media allowlist")
            accepted = True
            return resp, content_type
        except ApiError:
            raise
        except Exception:
            return None
        finally:
            if resp is not None and not accepted:
                _close_upstream(resp)

    record = _douyin_note_media_for_item(item_id)
    opened = attempt(record)
    if opened:
        return opened
    _invalidate_douyin_note_media(item_id)
    refreshed = _douyin_note_media_for_item(item_id, refresh=True)
    opened = attempt(refreshed)
    if opened:
        return opened
    raise ApiError(502, "抖音图片线路暂时不可用，请稍后重试")


def _open_douyin_video_upstream(item_id: str, headers: dict, validator=None):
    """打开官方网页生成的短时签名 CDN 地址；失效时只刷新一次。"""
    record = _douyin_media_for_item(item_id)
    if not record:
        raise ApiError(503, "抖音官方视频地址暂时不可用，请稍后重试")

    def attempt(candidate_record):
        for upstream in candidate_record.get("urls") or [candidate_record.get("url")]:
            if not upstream or not _douyin_public_url(upstream):
                continue
            resp = None
            accepted = False
            try:
                # CDN 签名可能绑定生成时的出口 IP；浏览器抓取时若使用了
                # 代理，媒体请求也固定到同一出口，避免首个 Range 直接 403。
                pinned_proxy = _douyin_browser_proxy_for_item(item_id)
                open_kwargs = {
                    "headers": headers,
                    "retry_http_statuses": (408, 425, 429, 500, 502, 503, 504),
                    "ban_on_auth_error": False,
                }
                if pinned_proxy:
                    open_kwargs["proxy_override"] = pinned_proxy
                resp, _ = open_url(upstream, **open_kwargs)
                status = resp.status if hasattr(resp, "status") else resp.getcode()
                if status not in (200, 206):
                    raise ValueError(f"unexpected video status {status}")
                content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0]
                content_type = content_type.strip().lower()
                if not content_type or not (
                        content_type.startswith("video/") or content_type in {
                            "application/mp4", "application/octet-stream",
                            "binary/octet-stream"}):
                    raise ValueError(f"unexpected video content type {content_type}")
                geturl = getattr(resp, "geturl", None)
                final_url = geturl() if callable(geturl) else ""
                if final_url and not _host_allowed(final_url):
                    raise ValueError("video redirect left the Douyin media allowlist")
                if validator:
                    validator(resp)
                accepted = True
                return resp
            except urlerr.HTTPError as exc:
                # Range 超出媒体末尾是一个确定的客户端请求错误，
                # 不是 CDN 签名失效；不应重抓页面或换线重试。
                if exc.code == 416:
                    content_range = str(exc.headers.get("Content-Range") or "")
                    headers_out = {"Accept-Ranges": "bytes"}
                    if re.fullmatch(r"bytes\s+\*/\d+", content_range, re.I):
                        headers_out["Content-Range"] = content_range
                    _close_upstream(exc)
                    raise ApiError(416, "请求范围超出媒体长度", headers_out)
                _close_upstream(exc)
            except ApiError as exc:
                # Chromium 抓取与媒体请求若使用了不同代理出口，CDN 可能
                # 返回鉴权/网关错误；清掉绑定后让下一轮重新选择出口。
                if pinned_proxy and exc.status in (401, 403, 502, 503):
                    _remember_douyin_browser_proxy(item_id, None)
                    continue
                raise
            except Exception:
                if resp is not None:
                    _close_upstream(resp)
            finally:
                if resp is not None and not accepted:
                    _close_upstream(resp)
        return None

    try:
        opened = attempt(record)
        if opened is not None:
            return opened
        # CDN 签名通常按请求方 IP/时间失效；重新跑官方页面取得新签名。
        _invalidate_douyin_media(item_id)
        refreshed = _douyin_media_for_item(item_id, refresh=True)
        if refreshed:
            opened = attempt(refreshed)
            if opened is not None:
                return opened
    except ApiError:
        raise
    raise ApiError(502, "抖音视频线路暂时不可用，请稍后重试")


@app.get("/api/douyin/image/{item_id}/{index}")
def api_douyin_image(item_id: str, index: int, request: Request,
                     exp: int = 0, sig: str = "", dl: str = "",
                     name: str = "image.jpeg"):
    """图集同源转发：客户端只能按作品 ID + 序号访问。"""
    if not re.fullmatch(r"\d{8,30}", str(item_id or "")):
        raise ApiError(400, "非法的抖音作品 ID")
    if index < 1 or index > 100:
        raise ApiError(404, "图片不存在")
    _require_media_token("douyin_image", f"{item_id}:{index}", exp, sig)
    lease = _media_lease(request, IMAGE_REQUESTS_PER_MIN)
    try:
        resp, content_type = _open_douyin_image_upstream(item_id, index)
    except Exception:
        _media_release(lease)
        raise

    def image_stream():
        sent = 0
        while True:
            block = resp.read(256 * 1024)
            if not block:
                break
            sent += len(block)
            if sent > IMAGE_MAX_BYTES:
                raise OSError("image exceeded configured byte limit")
            yield block

    headers = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    raw_length = str(resp.headers.get("Content-Length") or "")
    if raw_length.isdigit():
        headers["Content-Length"] = raw_length
    if dl:
        headers["Content-Disposition"] = _content_disposition(name or "image.jpeg")
    finalize = _media_finalizer(resp, lease)
    return _MediaStreamingResponse(
        image_stream(), finalize=finalize, media_type=content_type,
        headers=headers)

@app.get("/api/douyin/video/{item_id}")
def api_douyin_video(item_id: str, request: Request, exp: int = 0,
                     sig: str = "", dl: str = "", name: str = "video.mp4"):
    """抖音官方网页链路的同源 Range 流。

    客户端只提交作品 ID 和短期 HMAC，服务端从内存中的官方签名媒体记录取
    地址；绝不接受任意 URL，避免把该端点变成 SSRF。CDN 签名失效时由
    ``_open_douyin_video_upstream`` 惰性重抓官方网页并重试一次。
    """
    if not re.fullmatch(r"\d{8,30}", str(item_id or "")):
        raise ApiError(400, "非法的抖音作品 ID")
    _require_media_token("douyin_direct", item_id, exp, sig)
    range_header = request.headers.get("range", "")
    if not _valid_single_range(range_header):
        raise ApiError(416, "仅支持单段 bytes Range 请求")
    lease = _media_lease(request)
    extra = dict(CDN_HEADERS)
    if range_header:
        extra["Range"] = range_header
    opener = lambda outgoing, validator: _open_douyin_video_upstream(
        item_id, outgoing, validator=validator)
    try:
        resp = opener(
            extra, lambda candidate: _video_response_shape(
                candidate, range_header))
        status, start, end, total, expected = _video_response_shape(
            resp, range_header)
    except ApiError:
        _media_release(lease)
        raise
    except Exception:
        _media_release(lease)
        raise ApiError(502, "抖音视频线路暂时不可用，请稍后重试")

    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if expected is not None:
        headers["Content-Length"] = str(expected)
    if status == 206:
        headers["Content-Range"] = (
            f"bytes {start}-{end}/{total if total is not None else '*'}")
    if dl:
        headers["Content-Disposition"] = _content_disposition(name or "video.mp4")
    stream = _ResumableVideoStream(
        item_id, resp, extra, start, end, total, expected, opener=opener)
    scope = "download" if dl else "play"
    finalize = _media_finalizer(
        stream, lease, on_close=lambda: _traffic_add(scope, stream.sent))
    stream.set_on_close(finalize)
    return _MediaStreamingResponse(
        stream, finalize=finalize, status_code=status,
        media_type="video/mp4", headers=headers)


@app.get("/api/video/{vid}")
def api_video(vid: str, request: Request, exp: int = 0, sig: str = "",
              dl: str = "", name: str = "video.mp4"):
    if not re.fullmatch(r"[\w-]{8,120}", vid):
        raise ApiError(400, "非法的视频 ID")
    _require_media_token("video", vid, exp, sig)
    range_header = request.headers.get("range", "")
    if not _valid_single_range(range_header):
        raise ApiError(416, "仅支持单段 bytes Range 请求")
    lease = _media_lease(request)
    extra = dict(CDN_HEADERS)
    if range_header:
        extra["Range"] = range_header
    try:
        # 同源播放/下载线路：经代理，绝不直连暴露服务器 IP。
        # 主播放域名被风控、返回网关页或临时 5xx 时，自动切换备用域名。
        resp = _open_video_upstream(
            vid, extra,
            validator=lambda candidate: _video_response_shape(
                candidate, range_header))
        status, start, end, total, expected = _video_response_shape(
            resp, range_header)
    except ApiError:
        _media_release(lease)
        raise
    except Exception:
        _media_release(lease)
        raise ApiError(502, "视频下载线路暂时不可用，请稍后重试")

    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if expected is not None:
        headers["Content-Length"] = str(expected)
    if status == 206:
        headers["Content-Range"] = (
            f"bytes {start}-{end}/"
            f"{total if total is not None else '*'}")
    if dl:
        headers["Content-Disposition"] = _content_disposition(name or "video.mp4")
    stream = _ResumableVideoStream(
        vid, resp, extra, start, end, total, expected)
    # 流关闭时按实际转发字节计量（stream.sent 即已发给客户端的字节数）
    scope = "download" if dl else "play"
    finalize = _media_finalizer(
        stream, lease, on_close=lambda: _traffic_add(scope, stream.sent))
    stream.set_on_close(finalize)
    return _MediaStreamingResponse(
        stream,
        finalize=finalize, status_code=status,
        media_type="video/mp4", headers=headers)


class DownloadLinkBody(BaseModel):
    # 仅用于比较缓存是否已被其他请求更新；绝不把客户端 URL 作为请求目标。
    failed_url: str = Field(default="", max_length=4096)


def _request_download_link(item_id: str, failed_url: str) -> dict:
    """点击时复用新地址或合并刷新任务；轮询只查询同一个有界任务。"""
    cfg = _atc_cfg()
    now = int(time.time())
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                    "SELECT 1 FROM blocked_share_items WHERE kind='video' AND item_id=?",
                    (item_id,)).fetchone():
                raise ApiError(451, "该作品已被下架")
            row = conn.execute("SELECT * FROM atc_cache WHERE item_id=?", (item_id,)).fetchone()
            cached = dict(row) if row else {}
            job = conn.execute(
                "SELECT * FROM atc_jobs WHERE item_id=? AND purpose='download' ORDER BY id DESC LIMIT 1",
                (item_id,)).fetchone()
            age = now - int(job["updated"] or 0) if job else 60
            fresh = _atc_url_fresh(cached, cfg["url_ttl"])
            # 同一旧地址的并发报告直接复用已完成结果，即使上游仍返回相同 URL，
            # 也不能在一次点击的轮询过程中无限提交任务。
            just_done = bool(job and job["status"] == "done" and 0 <= age < 30
                             and int(cached.get("url_fetched_at") or 0) >= job["created"])
            if fresh and (not failed_url or failed_url != cached["video_url"] or just_done):
                result = {"status": "ready", "url": cached["video_url"],
                          "download_refresh_url": _video_download_refresh_url(item_id)}
                if _primary_media_allowed(cached["video_url"]):
                    result["proxy_url"] = _atc_video_proxy_url(item_id)
                    result["download_url"] = _atc_video_download_url(item_id)
                conn.commit()
                return result
            if job and job["status"] in ("pending", "submitting", "submitted"):
                if now - job["created"] > ATC_JOB_TIMEOUT:
                    conn.execute("UPDATE atc_jobs SET status='failed',error='下载链接更新超时',"
                                 "updated=?,lease_owner=NULL,lease_until=NULL WHERE id=?", (now, job["id"]))
                    conn.commit()
                    raise ApiError(504, "下载链接更新超时，请稍后再试")
                conn.commit()
                return {"status": "processing", "retry_after_ms": 2000}
            if job and 0 <= age < 30:
                raise ApiError(502, "暂时无法更新下载链接，请稍后重试", {"Retry-After": "30"})
            if not cfg["enabled"]:
                raise ApiError(503, "下载链接更新服务暂不可用，请稍后重试")
            source = _saved_source_in_conn(conn, item_id)
            links = _extract_supported_work_urls(source, 1)
            if not links:
                raise ApiError(404, "原分享链接已失效，请重新粘贴分享内容解析")
            pending = conn.execute(
                "SELECT COUNT(*) FROM atc_jobs WHERE purpose='download' "
                "AND status IN ('pending','submitting','submitted')").fetchone()[0]
            if pending >= 32:
                raise ApiError(503, "下载请求较多，请稍后重试", {"Retry-After": "5"})
            conn.execute(
                "INSERT INTO atc_jobs(item_id,work_url,purpose,status,created,updated) "
                "VALUES(?,?,'download','pending',?,?)", (item_id, links[0], now, now))
            conn.commit()
            return {"status": "processing", "retry_after_ms": 2000}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


@app.post("/api/media/video/{item_id}/download-link")
def api_video_download_link(item_id: str, body: DownloadLinkBody, request: Request,
                            exp: int = 0, sig: str = ""):
    if not re.fullmatch(r"[\w-]{8,40}", item_id):
        raise ApiError(400, "非法的作品 ID")
    _require_media_token("download_link", item_id, exp, sig)
    lease = _media_lease(request)
    try:
        result = _request_download_link(item_id, body.failed_url)
        return JSONResponse(result, status_code=202 if result["status"] == "processing" else 200,
                            headers={"Cache-Control": "private, no-store"})
    finally:
        _media_release(lease)


@app.get("/api/atc/video/{item_id}", include_in_schema=False)
@app.get("/api/media/video/{item_id}", name="stream_media")
def api_atc_video(item_id: str, request: Request, exp: int = 0, sig: str = "",
                  dl: str = "", name: str = "video.mp4"):
    """受签名保护的视频播放与下载接口；仅接受作品 ID。"""
    if not re.fullmatch(r"[\w-]{8,40}", item_id):
        raise ApiError(400, "非法的作品 ID")
    _require_media_token("atc_video", item_id, exp, sig)
    range_header = request.headers.get("range", "")
    if not _valid_single_range(range_header):
        raise ApiError(416, "仅支持单段 bytes Range 请求")
    lease = _media_lease(request)
    extra = dict(CDN_HEADERS)
    if range_header:
        extra["Range"] = range_header
    opener = lambda outgoing, validator: _open_atc_video_upstream(
        item_id, outgoing, validator=validator)
    try:
        resp = opener(
            extra, lambda candidate: _video_response_shape(
                candidate, range_header))
        status, start, end, total, expected = _video_response_shape(
            resp, range_header)
    except ApiError:
        _media_release(lease)
        raise
    except Exception:
        _media_release(lease)
        raise ApiError(502, "视频线路暂时不可用，请稍后重试")

    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if expected is not None:
        headers["Content-Length"] = str(expected)
    if status == 206:
        headers["Content-Range"] = (
            f"bytes {start}-{end}/{total if total is not None else '*'}")
    if dl:
        headers["Content-Disposition"] = _content_disposition(name or "video.mp4")
    stream = _ResumableVideoStream(
        item_id, resp, extra, start, end, total, expected, opener=opener)
    scope = "download" if dl else "play"
    finalize = _media_finalizer(
        stream, lease, on_close=lambda: _traffic_add(scope, stream.sent))
    stream.set_on_close(finalize)
    return _MediaStreamingResponse(
        stream, finalize=finalize, status_code=status,
        media_type="video/mp4", headers=headers)


# 注：图集打包 ZIP 需服务器同时拉取多张原图并在内存/磁盘压缩，
# 资源放大明显；保持前端逐张调用受限流保护的同源端点（downloadAll）。


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
def admin_login(body: LoginBody, request: Request):
    ip = _client_ip(request)
    if _admin_fail_count(ip) >= ADMIN_LOGIN_MAX_FAILS:
        raise ApiError(429, "登录失败次数过多，账号已临时锁定，请 15 分钟后再试")
    if not secrets.compare_digest(body.password, ADMIN_PASSWORD):
        _admin_record_fail(ip)
        left = ADMIN_LOGIN_MAX_FAILS - _admin_fail_count(ip)
        raise ApiError(403, f"密码错误，还可尝试 {left} 次" if left > 0
                       else "密码错误，账号已临时锁定，请 15 分钟后再试")
    with _rate_lock:
        _admin_fails.pop(ip, None)      # 成功登录 → 清零失败计数
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
        "captcha": dict(_captcha_stats),     # 滑块漏斗（内存计数，重启清零）
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


class ProxyBatchBody(BaseModel):
    action: str                            # delete / enable / disable / test
    ids: list[str]


@app.post("/api/admin/proxies/batch")
def admin_batch_proxy(body: ProxyBatchBody, request: Request):
    """批量操作选中的代理；managed 托管条目跳过增删启停（归 mihomo 面板管）。"""
    _require_admin(request)
    ids = set(body.ids)
    if not ids:
        raise ApiError(400, "未选择任何代理")
    if len(ids) > 500:
        raise ApiError(400, "单次批量操作最多 500 个")
    if body.action == "delete":
        return {"ok": True, "affected": proxy_mgr.remove_many(ids)}
    if body.action in ("enable", "disable"):
        return {"ok": True, "affected": proxy_mgr.set_enabled_many(ids, body.action == "enable")}
    if body.action == "test":
        selected = [p for p in proxy_mgr.proxies if p["id"] in ids]
        reach = proxy_mgr.settings.get("test_reach_douyin", True)
        results = _probe_all(selected, reach)
        ok = sum(1 for r in results if r and r.get("ok"))
        return {"results": results, "ok": ok, "total": len(results)}
    raise ApiError(400, "不支持的批量操作")


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


class KeyBody(BaseModel):
    key: str


@app.post("/api/admin/apikeys/revoke")
def admin_revoke_key(body: KeyBody, request: Request):
    _require_admin(request)
    if not revoke_api_key(body.key):
        raise ApiError(404, "API Key 不存在")
    return {"ok": True}


class RechargeBody(KeyBody):
    cents: int


@app.post("/api/admin/apikeys/recharge")
def admin_recharge_key(body: RechargeBody, request: Request):
    _require_admin(request)
    if not recharge_key(body.key, body.cents):
        raise ApiError(404, "API Key 不存在")
    return {"ok": True, "key": get_api_key(body.key)}


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
    rev_today = (one("SELECT COALESCE(SUM(cost_cents),0) FROM api_logs WHERE ts>=?", (day0,))
                 + one("SELECT COALESCE(SUM(spent_delta),0) FROM wallet_ledger WHERE ts>=?", (day0,)))
    rev_total = (one("SELECT COALESCE(SUM(cost_cents),0) FROM api_logs")
                 + one("SELECT COALESCE(SUM(spent_cents),0) FROM users"))
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
            "revenue": _series("SELECT ts/86400, COALESCE(SUM(cost),0) FROM "
                               "(SELECT ts,cost_cents AS cost FROM api_logs UNION ALL "
                               "SELECT ts,spent_delta AS cost FROM wallet_ledger) WHERE ts>=? GROUP BY 1"),
        },
    }


@app.get("/api/admin/users")
def admin_users(request: Request, limit: int = 100):
    _require_admin(request)
    rows = db_exec(
        "SELECT u.id,u.email,u.created_at,u.last_login,u.disabled,u.reg_ip,"
        "u.balance_cents,u.reserved_cents,u.spent_cents,u.wallet_version,"
        "MAX(0,? - COALESCE((SELECT count FROM usage_daily d "
        "WHERE d.day=? AND d.subject='user:'||u.id),0)) AS free_remaining,"
        "(SELECT COUNT(*) FROM request_logs r WHERE r.user_id=u.id AND r.ok=1) AS parses,"
        "(SELECT COALESCE(SUM(spent_cents),0) FROM api_keys k WHERE k.user_id=u.id) AS spent,"
        "(SELECT COUNT(*) FROM api_keys k WHERE k.user_id=u.id) AS keys "
        "FROM users u ORDER BY u.created_at DESC LIMIT ?",
        (free_user_daily(), _today(), max(1, min(limit, 500))), "all")
    return {"users": [dict(r) for r in rows]}


class WalletEditBody(BaseModel):
    mode: str
    cents: StrictInt = Field(ge=0, le=1000000000)
    expected_version: StrictInt = Field(ge=0)
    request_id: str = Field(min_length=8, max_length=80)
    note: str = Field(default="", max_length=200)


@app.get("/api/admin/users/{uid}/wallet")
def admin_user_wallet(uid: int, request: Request):
    _require_admin(request)
    row = db_exec("SELECT * FROM users WHERE id=?", (uid,), "one")
    if not row:
        raise ApiError(404, "用户不存在")
    logs = db_exec("SELECT ts,event,balance_delta,reserved_delta,spent_delta,note "
                   "FROM wallet_ledger WHERE user_id=? ORDER BY id DESC LIMIT 30", (uid,), "all")
    return {"user_id": uid, "email": row["email"], **_wallet_public(row),
            "ledger": [dict(r) for r in logs]}


@app.post("/api/admin/users/{uid}/wallet")
def admin_edit_wallet(uid: int, body: WalletEditBody, request: Request):
    _require_admin(request)
    if body.mode not in ("add", "set") or (body.mode == "add" and body.cents == 0):
        raise ApiError(400, "请输入有效的余额调整方式和金额")
    request_hash = hashlib.sha256(json.dumps(
        [body.mode, body.cents, body.expected_version, body.note], ensure_ascii=False).encode()).hexdigest()
    event_key = "admin:" + body.request_id
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if not row:
                raise ApiError(404, "用户不存在")
            previous = conn.execute("SELECT request_hash FROM wallet_ledger WHERE user_id=? AND event_key=?",
                                    (uid, event_key)).fetchone()
            if previous:
                if previous[0] != request_hash:
                    raise ApiError(409, "请刷新余额后重新提交")
                conn.commit()
                return {"ok": True, **_wallet_public(row)}
            if row["wallet_version"] != body.expected_version:
                raise ApiError(409, "账户余额已变化，请刷新后重试")
            delta = body.cents if body.mode == "add" else body.cents - row["balance_cents"]
            if row["balance_cents"] + delta > 1000000000:
                raise ApiError(400, "余额超过可设置上限")
            _wallet_change(conn, uid, "adjust", event_key, balance=delta,
                           note=body.note, request_hash=request_hash)
            updated = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            conn.commit()
            return {"ok": True, **_wallet_public(updated)}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class BillingPriceBody(BaseModel):
    parse_price_cents: StrictInt = Field(ge=0, le=100000)
    transcript_price_cents: StrictInt = Field(ge=0, le=100000)
    user_daily: Optional[StrictInt] = Field(default=None, ge=0, le=10000)


def _web_billing_status(user_id: Optional[int] = None) -> dict:
    with _db_lock:
        conn = _db()
        try:
            prices = {"parse_price_cents": _web_price_in_conn(conn),
                      "transcript_price_cents": _web_price_in_conn(conn, "transcript"),
                      "user_daily": _free_user_daily_in_conn(conn)}
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone() if user_id else None
            return {**prices, "wallet": _wallet_public(row) if row else None}
        finally:
            conn.close()


@app.get("/api/admin/billing-prices")
def admin_billing_prices(request: Request):
    _require_admin(request)
    return _web_billing_status()


@app.post("/api/admin/billing-prices")
def admin_save_billing_prices(body: BillingPriceBody, request: Request):
    _require_admin(request)
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for key, value in (("web_parse_price_cents", body.parse_price_cents),
                               ("transcript_price_cents", body.transcript_price_cents)):
                conn.execute("INSERT INTO app_settings(k,v) VALUES(?,?) "
                             "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, str(value)))
            if body.user_daily is not None:
                conn.execute("INSERT INTO app_settings(k,v) VALUES('free_user_daily',?) "
                             "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(body.user_daily),))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return {"ok": True, **_web_billing_status()}


class UserToggleBody(BaseModel):
    disabled: bool


@app.post("/api/admin/users/{uid}/toggle")
def admin_toggle_user(uid: int, body: UserToggleBody, request: Request):
    _require_admin(request)
    db_exec("UPDATE users SET disabled=? WHERE id=?", (1 if body.disabled else 0, uid))
    if body.disabled:
        db_exec("DELETE FROM user_sessions WHERE user_id=?", (uid,))
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


# ---- 功能检查：仅管理员可运行，不把存活、已配置或媒体成功当作元数据成功 ----

_function_check_lock = threading.RLock()
_function_check_job: dict = {}


def _check_item(ident: str, status: str, code: str, value=None) -> dict:
    row = {"id": ident, "status": status, "code": code}
    if value is not None:
        row["value"] = value
    return row


def _incomplete_share_samples() -> dict:
    rows = db_exec(
        "SELECT id,item_id,payload FROM shares WHERE status='ok' "
        "AND COALESCE(parse_status,'ready')='ready' AND (expires_at=0 OR expires_at>?) "
        "ORDER BY created DESC LIMIT 100", (int(time.time()),), "all")
    missing = []
    for row in rows:
        try:
            data = json.loads(row["payload"])
            fields = _metadata_missing(data)
        except (TypeError, ValueError, AttributeError):
            fields, data = ["snapshot"], {}
        if fields:
            missing.append({"sid": row["id"], "title": str(data.get("title") or "")[:300],
                            "missing": fields})
    return {"scanned": len(rows), "limit": 100, "items": missing}


def _function_check_environment() -> dict:
    binary = _douyin_browser_binary()
    checks = [_check_item("version", "pass", "version", FRONTEND_VERSION)]
    checks.append(_check_item("browser", "pending" if binary and _douyin_browser_enabled() else "fail",
                              "browser_found" if binary and _douyin_browser_enabled() else
                              "browser_disabled" if binary else "browser_missing"))
    cfg = _atc_cfg()
    configured = bool(cfg["enabled"] and cfg["key"] and cfg["secret"])
    checks.append(_check_item("parser", "pending" if configured else "warn",
                              "parser_configured" if configured else "parser_unconfigured"))
    available = len(proxy_mgr.candidates())
    checks.append(_check_item("proxy", "fail" if proxy_mgr.force_proxy and not available else "pass",
                              "proxy_required" if proxy_mgr.force_proxy and not available else
                              "proxy_strict" if proxy_mgr.force_proxy else "proxy_direct", available))
    try:
        samples = _incomplete_share_samples()
        checks.append(_check_item("database", "pass", "database_readable"))
    except Exception:
        samples = {"scanned": 0, "limit": 100, "items": []}
        checks.append(_check_item("database", "fail", "database_failed"))
    with _metadata_retry_lock:
        failure = dict(_metadata_last_failure)
    return {"version": APP_VERSION, "frontend_version": FRONTEND_VERSION,
            "checks": checks, "shares": samples, "last_metadata_failure": failure}


def _function_check_record(ident: str, status: str, code: str, value=None) -> None:
    with _function_check_lock:
        rows = _function_check_job.setdefault("checks", [])
        rows[:] = [row for row in rows if row["id"] != ident]
        rows.append(_check_item(ident, status, code, value))


def _check_error_code(exc: Exception) -> str:
    # 白名单分类；不把异常文本、上游响应和带签名地址传到管理页面。
    reason = getattr(exc, "reason", "")
    return {"auth": "parser_auth", "entitlement": "parser_entitlement",
            "busy": "parser_busy"}.get(reason, "request_failed")


def _function_browser_probe() -> bool:
    """沿用正式补全的 CDP 启动方式；不依赖会被子进程管道拖住的 dump-dom。"""
    binary = _douyin_browser_binary()
    if not binary or not _douyin_browser_enabled():
        return False
    with tempfile.TemporaryDirectory(prefix="douyin-check-") as profile:
        port = _free_local_port()
        proc = subprocess.Popen([
            binary, "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-gpu", "--disable-background-networking", "--disable-extensions",
            "--no-first-run", f"--user-data-dir={profile}",
            "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={port}", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        conn = None
        try:
            deadline = time.monotonic() + DOUYIN_BROWSER_START_TIMEOUT
            version = None
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    with urlreq.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=.5) as response:
                        version = json.loads(response.read(65536))
                    break
                except Exception:
                    time.sleep(.08)
            if not isinstance(version, dict) or not version.get("webSocketDebuggerUrl"):
                return False
            conn = _CDPConnection(version["webSocketDebuggerUrl"], timeout=2)
            target = conn.command("Target.createTarget", {"url": "about:blank"}, timeout=3)
            attached = conn.command("Target.attachToTarget", {
                "targetId": target["targetId"], "flatten": True}, timeout=3)
            result = conn.command("Runtime.evaluate", {
                "expression": "document.documentElement.tagName", "returnByValue": True,
            }, attached["sessionId"], timeout=3)
            return (result.get("result") or {}).get("value") == "HTML"
        finally:
            if conn is not None:
                conn.close()
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=2)


def _function_media_probe(data: dict) -> dict:
    if data.get("kind") != "video":
        return _check_item("media", "skipped", "video_only")
    source = data.get("source")
    opener = (_open_primary_video_upstream if source in ("atc", "parser")
              else _open_douyin_video_upstream if source in _LEGACY_DIRECT_SOURCES else None)
    if opener is None:
        return _check_item("media", "skipped", "media_not_supported")
    response = opener(str(data.get("item_id") or ""), dict(CDN_HEADERS, Range="bytes=0-0"),
                      validator=lambda r: _video_response_shape(r, "bytes=0-0"))
    try:
        if not response.read(1):
            raise ApiError(502, "媒体响应为空")
        return _check_item("media", "pass", "media_byte_ok")
    finally:
        _close_upstream(response)


def _function_check_url(text: str) -> str:
    links = _extract_supported_work_urls(text, 2)
    if len(links) != 1 or _atc_platform_for_url(links[0]) not in ("douyin", "tiktok"):
        raise ApiError(400, "请提供一条抖音或 TikTok 的公开作品链接")
    return links[0]


def _function_share_row(sid: str) -> dict:
    row = db_exec("SELECT * FROM shares WHERE id=?", (sid,), "one")
    if not row:
        raise ApiError(404, "分享页不存在")
    row = dict(row)
    if _share_state(row) != "ok":
        raise ApiError(409, "只可补齐有效且已完成解析的分享页")
    return row


def _function_repair_share(sid: str) -> dict:
    row = _function_share_row(sid)
    old = json.loads(row["payload"] or "{}")
    _require_share_item_allowed(old)
    fresh = _get_parse_snapshot(row["item_id"])
    if not fresh or _metadata_missing(fresh):
        work_url = ((_atc_cache_get(row["item_id"]) or {}).get("work_url")
                    or row.get("source_url") or "")
        if not work_url and re.fullmatch(r"\d{8,30}", row["item_id"]) and (
                old.get("platform") == "douyin" or old.get("source") in _LEGACY_DIRECT_SOURCES):
            work_url = _douyin_work_url(row["kind"], row["item_id"])
        work_url = _function_check_url(work_url)
        if _is_douyin_work_url(work_url):
            kind, real_id, canonical = _douyin_resolve_share_url(work_url)
            if row["item_id"] not in (real_id, "item_" + hashlib.sha256(work_url.encode()).hexdigest()[:24]):
                raise ApiError(409, "作品身份不一致，已停止信息补齐")
            fresh = _parse_douyin_item_direct(kind, real_id, canonical, allow_metadata=True, refresh=True)
            fresh["author_detail"] = _snapshot_author(fresh)
            fresh = dict(fresh, item_id=row["item_id"])
        else:
            fresh = _atc_parse_work_url(work_url)
        fresh["snapshot_at"] = int(time.time())
    # 网络调用期间分享页可能被下架；写入前再次验证状态和归属。
    row = _function_share_row(sid)
    old = json.loads(row["payload"] or "{}")
    _require_share_item_allowed(old)
    merged = _merge_metadata_snapshot(old, fresh)
    count = _backfill_share_metadata(merged, sid=sid)
    _save_parse_snapshot(merged)
    return {"updated": count, "missing": _metadata_missing(merged), "sid": sid}


def _function_check_worker(work_url: str, sid: str) -> None:
    try:
        if sid:
            _function_check_record("repair", "running", "repair_running")
            result = _function_repair_share(sid)
            _function_check_record("repair", "warn" if result["missing"] else "pass",
                                   "repair_partial" if result["missing"] else "repair_ok", result)
            return
        _function_check_record("browser", "running", "browser_running")
        try:
            ok = _function_browser_probe()
            code = ("browser_ok" if ok else "browser_missing" if not _douyin_browser_binary()
                    else "browser_disabled" if not _douyin_browser_enabled() else "browser_failed")
            _function_check_record("browser", "pass" if ok else "fail", code)
        except Exception:
            _function_check_record("browser", "fail", "browser_failed")
        primary, native = None, None
        _function_check_record("parser", "running", "parser_running")
        try:
            raw = _atc_extract(work_url, include_text=False)
            primary = _atc_result_to_parse(work_url, raw, allow_partial=_is_douyin_work_url(work_url))
            _function_check_record("parser", "pass", "parser_ok")
        except Exception as exc:
            _function_check_record("parser", "fail", _check_error_code(exc))
        if _is_douyin_work_url(work_url):
            _function_check_record("official", "running", "official_running")
            try:
                kind, real_id, canonical = _douyin_resolve_share_url(work_url)
                native = _parse_douyin_item_direct(kind, real_id, canonical, allow_metadata=True, refresh=True)
                _function_check_record("official", "warn" if _metadata_missing(native) else "pass",
                                       "metadata_partial" if _metadata_missing(native) else "official_ok",
                                       _metadata_missing(native))
            except Exception:
                _record_metadata_failure("metadata")
                _function_check_record("official", "fail", "official_failed")
        else:
            _function_check_record("official", "skipped", "tiktok_primary")
        data = (_complete_douyin_result(work_url, primary) if primary and _is_douyin_work_url(work_url)
                else primary or native)
        if not data:
            _function_check_record("metadata", "fail", "metadata_unavailable")
            _function_check_record("media", "skipped", "no_result")
            _function_check_record("snapshot", "skipped", "no_result")
            return
        missing = _metadata_missing(data)
        summary = {k: data.get(k) for k in ("item_id", "title", "author", "stats")}
        summary["missing"] = missing
        _function_check_record("metadata", "warn" if missing else "pass",
                               "metadata_partial" if missing else "metadata_ok", summary)
        _function_check_record("media", "running", "media_running")
        try:
            result = _function_media_probe(data)
            _function_check_record(result["id"], result["status"], result["code"])
        except Exception:
            _function_check_record("media", "fail", "media_failed")
        _function_check_record("snapshot", "running", "snapshot_running")
        _remember_parse_result(work_url, data, time.time())
        saved = _get_parse_snapshot(data["item_id"])
        if not saved or any(saved.get(k) != data.get(k) for k in ("title", "author", "stats")):
            raise ApiError(500, "信息快照校验失败")
        _function_check_record("snapshot", "pass", "snapshot_ok")
    except Exception:
        _function_check_record("repair" if sid else "result", "fail", "repair_failed" if sid else "check_failed")
    finally:
        with _function_check_lock:
            # 异常中止的阶段不能永远显示“正在检查”。
            for row in _function_check_job.get("checks", []):
                if row["status"] == "running":
                    row.update(status="fail", code="check_failed")
            _function_check_job.update(state="done", finished_at=int(time.time()))


def _start_function_check(work_url: str = "", sid: str = "") -> dict:
    with _function_check_lock:
        if _function_check_job.get("state") == "running":
            raise ApiError(409, "已有功能检查正在进行，请等待完成")
        if time.time() - _function_check_job.get("finished_at", 0) < 10:
            raise ApiError(429, "请等待 10 秒后再次检查", {"Retry-After": "10"})
        _function_check_job.clear()
        _function_check_job.update(id=secrets.token_hex(8), state="running", mode="repair" if sid else "probe",
                                   started_at=int(time.time()), checks=[])
        try:
            threading.Thread(target=_function_check_worker, args=(work_url, sid), daemon=True).start()
        except Exception:
            _function_check_job.update(state="done", finished_at=int(time.time()))
            raise ApiError(503, "暂时无法启动功能检查") from None
        return dict(_function_check_job)


class FunctionCheckBody(BaseModel):
    text: str = ""


@app.get("/api/admin/checks")
def admin_function_checks(request: Request):
    _require_admin(request)
    status = _function_check_environment()
    with _function_check_lock:
        status["job"] = json.loads(json.dumps(_function_check_job))
    return status


@app.post("/api/admin/checks/run", status_code=202)
def admin_function_check_run(body: FunctionCheckBody, request: Request):
    _require_admin(request)
    if len(body.text) > PARSE_TEXT_MAX:
        raise ApiError(413, "解析文本超过上限")
    return _start_function_check(work_url=_function_check_url(body.text))


@app.post("/api/admin/checks/shares/{sid}/repair", status_code=202)
def admin_function_check_repair(sid: str, request: Request):
    _require_admin(request)
    _function_share_row(sid)
    return _start_function_check(sid=sid)


# ---- 视频解析服务（配置 / 测试 / 播放优先级）----

def _parser_test_error(exc: Exception) -> str:
    """管理员可诊断配置问题，但仍不展示原始响应或凭据。"""
    reason = getattr(exc, "reason", "")
    if reason == "auth":
        return "解析服务鉴权失败，请核对后台密钥是否有效"
    if reason == "entitlement":
        return "解析服务账号权益不足，请检查会员状态与可用额度"
    return exc.message if isinstance(exc, ApiError) else "测试任务查询失败，请稍后重试"


def _parser_test_success(data: dict, ms) -> dict:
    normalized = _atc_result_data(data)
    video = normalized.get("video") if isinstance(normalized.get("video"), dict) else {}
    return {"state": "success", "ms": ms, "duration": normalized.get("duration"),
            "has_video": bool(normalized.get("videoUrl") or normalized.get("videoUrlList")
                              or video.get("url")),
            "has_text": bool(normalized.get("textContent"))}


@app.get("/api/admin/atc", include_in_schema=False)
@app.get("/api/admin/parser", name="get_parser_settings")
def admin_atc_get(request: Request):
    _require_admin(request)
    status = _atc_status()
    # 面板刷新也遵守查询间隔及有界截止时间。
    try:
        test = json.loads(app_setting("atc_test_state", "") or "{}")
    except ValueError:
        test = {}
    if not isinstance(test, dict):
        test = {}
    if test.get("state") == "submitted" and test.get("task_id"):
        now = int(time.time())
        test.setdefault("created", now)
        if now - int(test.get("updated") or 0) < max(5, ATC_POLL_INTERVAL):
            return status
        cfg = _atc_cfg()
        test["updated"] = now
        try:
            if now - int(test["created"]) >= ATC_JOB_TIMEOUT:
                raise ApiError(504, "测试任务超时，请稍后重试")
            resp = _atc_request("GET", "/video/query", {"taskId": test["task_id"]}, cfg)
            if resp.get("code") != 200:
                raise _atc_rejected(resp, "测试任务查询")
            data = resp.get("data") or {}
            if not isinstance(data, dict):
                raise ApiError(502, "测试任务结果暂不可用，请稍后重试")
            if _atc_result_complete(data):
                test = _parser_test_success(data, test.get("ms"))
            else:
                test.pop("error", None)
        except Exception as exc:
            if not getattr(exc, "retryable", False):
                test = {"state": "failed"}
            test["error"] = _parser_test_error(exc)
        set_app_setting("atc_test_state", json.dumps(test, ensure_ascii=False))
    status["test"] = _public_parser_test_state()
    return status


class AtcSettingsBody(BaseModel):
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    base_url: Optional[str] = None
    enabled: Optional[bool] = None
    play_enhance: Optional[bool] = None
    transcript_enabled: Optional[bool] = None
    transcript_daily: Optional[int] = None
    url_ttl: Optional[int] = None
    play_priority: Optional[list] = None


@app.post("/api/admin/atc", include_in_schema=False)
@app.post("/api/admin/parser", name="set_parser_settings")
def admin_atc_set(body: AtcSettingsBody, request: Request):
    _require_admin(request)
    if body.api_key is not None:
        set_app_setting("atc_api_key", body.api_key.strip()[:100])
    if body.api_secret is not None:      # 空串 = 清除；前端不回传打码值
        set_app_setting("atc_api_secret", body.api_secret.strip()[:100])
    if body.base_url is not None:
        base = body.base_url.strip().rstrip("/")
        if base and base != ATC_DEFAULT_BASE:
            raise ApiError(400, "视频解析接口由系统管理，无法修改")
        set_app_setting("atc_base_url", ATC_DEFAULT_BASE)
    if body.enabled is not None:
        set_app_setting("atc_enabled", "1" if body.enabled else "0")
    if body.play_enhance is not None:
        set_app_setting("atc_play_enhance", "1" if body.play_enhance else "0")
    if body.transcript_enabled is not None:
        set_app_setting("atc_transcript_enabled", "1" if body.transcript_enabled else "0")
    if body.transcript_daily is not None:
        set_app_setting("atc_transcript_daily",
                        str(max(0, min(100, int(body.transcript_daily)))))
    if body.url_ttl is not None:
        set_app_setting("atc_url_ttl", str(max(600, min(86400, int(body.url_ttl)))))
    if body.play_priority is not None:
        if not (isinstance(body.play_priority, list)
                and sorted(body.play_priority) == sorted(SHARE_PLAY_SOURCES)):
            raise ApiError(400, "请选择全部四条播放线路，每条仅保留一次")
        set_app_setting("share_play_priority", json.dumps(body.play_priority))
    return _atc_status()


class AtcTestBody(BaseModel):
    work_url: str = ""


@app.post("/api/admin/atc/test", include_in_schema=False)
@app.post("/api/admin/parser/test", name="test_parser")
def admin_atc_test(body: AtcTestBody, request: Request):
    """测试普通解析；保存任务 ID，由后台面板有界轮询推进。"""
    _require_admin(request)
    cfg = _atc_cfg()
    if not (cfg["key"] and cfg["secret"]):
        raise ApiError(400, "请先保存 API Key 与 Secret")
    work_url = body.work_url.strip() or "https://v.douyin.com/uc_Eukb0zUM/"
    t0 = time.time()
    try:
        resp = _atc_request("POST", "/video/extract", {"workUrl": work_url}, cfg)
        if resp.get("code") != 200 or not resp.get("data"):
            raise _atc_rejected(resp, "测试任务提交")
        ms = int((time.time() - t0) * 1000)
        created = resp["data"]
        if _atc_result_complete(created):
            state = _parser_test_success(created, ms)
        else:
            task_id = _atc_task_id(created)
            if not task_id:
                raise ApiError(502, "任务提交响应缺少 taskId")
            state = {"state": "submitted", "task_id": task_id, "ms": ms,
                     "created": int(t0), "updated": int(time.time())}
    except Exception as exc:
        detail = _parser_test_error(exc)
        set_app_setting("atc_test_state", json.dumps(
            {"state": "failed", "error": detail}, ensure_ascii=False))
        raise ApiError(exc.status if isinstance(exc, ApiError) else 502, detail)
    set_app_setting("atc_test_state", json.dumps(state, ensure_ascii=False))
    return {"ok": True, **{k: v for k, v in state.items() if k not in ("created", "updated")}}


# ---- 分享页管理（含侵权下架）----

@app.get("/api/admin/shares")
def admin_shares(request: Request, limit: int = 100, q: str = ""):
    _require_admin(request)
    like = f"%{q}%"
    rows = db_exec(
        "SELECT id,item_id,kind,title,author,status,parse_status,parse_error_code,"
        "views,plays,downloads,cta_clicks,"
        "expires_at,created,owner_user_id,owner_ip FROM shares "
        "WHERE (?='' OR title LIKE ? OR author LIKE ? OR id=?) "
        "ORDER BY created DESC LIMIT ?",
        (q, like, like, q, max(1, min(500, limit))), "all")
    tot = db_exec("SELECT COUNT(*) c, COALESCE(SUM(views),0) v, COALESCE(SUM(plays),0) p, "
                  "COALESCE(SUM(cta_clicks),0) k FROM shares", (), "one")
    return {"shares": [dict(r) for r in rows],
            "total": tot["c"], "views": tot["v"], "plays": tot["p"], "cta": tot["k"]}


@app.post("/api/admin/shares/{sid}/takedown")
def admin_takedown(sid: str, request: Request):
    _require_admin(request)
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            target = conn.execute(
                "SELECT * FROM shares WHERE id=?", (sid,)).fetchone()
            if not target:
                raise ApiError(404, "分享页不存在")
            now = int(time.time())
            if target["source_hash"]:
                conn.execute(
                    "INSERT OR IGNORE INTO blocked_share_sources(source_hash,created) "
                    "VALUES(?,?)", (target["source_hash"], now))
            if target["item_id"]:
                conn.execute(
                    "INSERT OR IGNORE INTO blocked_share_items(kind,item_id,created) "
                    "VALUES(?,?,?)",
                    (target["kind"] or "video", target["item_id"], now))

            targets = {target["id"]: target}
            if target["source_hash"]:
                for row in conn.execute(
                        "SELECT * FROM shares WHERE source_hash=?",
                        (target["source_hash"],)).fetchall():
                    targets[row["id"]] = row
            if target["item_id"]:
                for row in conn.execute(
                        "SELECT * FROM shares WHERE kind=? AND item_id=?",
                        (target["kind"] or "video", target["item_id"])).fetchall():
                    targets[row["id"]] = row
            for row in targets.values():
                conn.execute(
                    "UPDATE shares SET status='takedown',parse_status=CASE "
                    "WHEN parse_status IN ('pending','processing') THEN 'failed' "
                    "ELSE parse_status END,source_url=NULL,parse_error_code=CASE "
                    "WHEN parse_status IN ('pending','processing') THEN 'content_blocked' "
                    "ELSE parse_error_code END,lease_owner=NULL,lease_until=NULL,updated=? "
                    "WHERE id=?", (now, row["id"]))
                # 平台处置不向用户计费；旧 worker 的 lease CAS 会因此失效。
                _admin_refund_quota_in_conn(
                    conn, row["quota_reservation_id"])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return {"ok": True}


@app.delete("/api/admin/shares/{sid}")
def admin_del_share(sid: str, request: Request):
    _require_admin(request)
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT quota_reservation_id FROM shares WHERE id=?", (sid,)).fetchone()
            if row:
                _admin_refund_quota_in_conn(
                    conn, row["quota_reservation_id"])
                conn.execute("DELETE FROM shares WHERE id=?", (sid,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return {"ok": True}


@app.get("/api/admin/play-stats")
def admin_play_stats(request: Request, hours: int = 24, limit: int = 60):
    """播放诊断看板：看清「微信内哪些视频能播、走的哪条线路、失败在哪一步」。

    所有环境（含微信）播放链路均为 dy1 → dy2 → proxy，抖音直连优先、同源代理兜底。
    每条线路的 try/ok/fail 都会上报，带宽分析需要按微信内/外分别观察。"""
    _require_admin(request)
    hours = max(1, min(24 * 30, hours))
    limit = max(1, min(300, limit))
    since = int(time.time()) - hours * 3600

    # 按 微信内/外 × 线路 汇总成功率。排除 giveup —— 它是「三条都挂了」的汇总事件，
    # 没有 source，混进来会多出一行空线路并把失败数重复计一遍。
    rows = db_exec(
        "SELECT wechat, COALESCE(source,'') source, kind, COUNT(*) n, "
        "CAST(AVG(ms) AS INTEGER) avg_ms FROM share_events "
        "WHERE ts>=? AND kind IN ('play_ok','play_fail') AND COALESCE(stage,'')<>'giveup' "
        "GROUP BY wechat, source, kind", (since,), "all")
    agg: dict = {}
    for r in rows:
        k = (r["wechat"], r["source"])
        cur = agg.setdefault(k, {"wechat": r["wechat"], "source": r["source"],
                                 "ok": 0, "fail": 0, "ok_ms": 0})
        if r["kind"] == "play_ok":
            cur["ok"] = r["n"]
            cur["ok_ms"] = r["avg_ms"] or 0
        else:
            cur["fail"] = r["n"]
    lines = sorted(agg.values(), key=lambda x: (-x["wechat"], x["source"]))
    for x in lines:
        t = x["ok"] + x["fail"]
        x["total"] = t
        x["rate"] = round(x["ok"] * 100.0 / t, 1) if t else 0.0

    # 彻底放弃（三条线路全挂）的次数，微信内外分开
    gv = db_exec("SELECT wechat, COUNT(*) n FROM share_events "
                 "WHERE ts>=? AND kind='play_fail' AND stage='giveup' "
                 "GROUP BY wechat", (since,), "all")
    giveup = {("wechat" if r["wechat"] else "other"): r["n"] for r in gv}

    # 按作品维度：微信内失败最多的分享页（就是「哪些视频播不了」）。
    # 同样排除 giveup 汇总事件，并单列 giveup 次数 —— 那才是「彻底放不出来」的次数。
    bad = db_exec(
        "SELECT e.sid, COALESCE(s.title,'(已删除)') title, COALESCE(s.kind,'') vkind, "
        "SUM(CASE WHEN e.kind='play_ok' THEN 1 ELSE 0 END) ok, "
        "SUM(CASE WHEN e.kind='play_fail' AND COALESCE(e.stage,'')<>'giveup' THEN 1 ELSE 0 END) fail, "
        "SUM(CASE WHEN COALESCE(e.stage,'')='giveup' THEN 1 ELSE 0 END) giveup "
        "FROM share_events e LEFT JOIN shares s ON s.id=e.sid "
        "WHERE e.ts>=? AND e.wechat=1 AND e.kind IN ('play_ok','play_fail') "
        "GROUP BY e.sid HAVING fail>0 ORDER BY giveup DESC, fail DESC, ok ASC LIMIT ?",
        (since, limit), "all")

    # 播放尝试全量日志：每条线路的 尝试(play_try)/成功/失败 明细，带粗粒度 UA
    # 定位是哪个机型/内核播不了。play_try 为 v1.11 新增，旧数据只有 ok/fail。
    log = db_exec(
        "SELECT ts,sid,kind,COALESCE(source,'') source,COALESCE(stage,'') stage,"
        "COALESCE(detail,'') detail,ms,wechat,ua FROM share_events "
        "WHERE ts>=? AND kind IN ('play_try','play_ok','play_fail') "
        "ORDER BY ts DESC LIMIT ?",
        (since, limit), "all")

    return {"hours": hours, "lines": lines, "giveup": giveup,
            "bad": [dict(r) for r in bad], "log": [dict(r) for r in log]}


@app.get("/api/admin/play-logs")
def admin_play_logs(request: Request, page: int = 1, size: int = 20,
                    result: str = "", wechat: str = "", sid: str = ""):
    """播放请求日志（服务端分页）：记录每次播放的浏览器环境、线路、成败，
    以及失败后将重试的下一条线路（next_src）。支撑「微信内是否播放成功」的逐条核查。
    只存粗粒度环境与线路名，不存完整媒体签名地址（隐私红线）。"""
    _require_admin(request)
    page = max(1, int(page))
    size = max(1, min(100, int(size)))
    where = ["e.kind IN ('play_try','play_ok','play_fail')"]
    params: list = []
    if result in ("try", "ok", "fail"):
        where.append("e.kind=?")
        params.append("play_" + result)
    if wechat in ("0", "1"):
        where.append("e.wechat=?")
        params.append(int(wechat))
    if sid.strip():
        where.append("e.sid=?")
        params.append(sid.strip()[:20])
    cond = " AND ".join(where)
    total = db_exec(f"SELECT COUNT(*) FROM share_events e WHERE {cond}",
                    tuple(params), "one")[0]
    rows = db_exec(
        "SELECT e.ts,e.sid,COALESCE(s.title,'(已删除)') title,e.kind,"
        "COALESCE(e.source,'') source,COALESCE(e.stage,'') stage,"
        "COALESCE(e.detail,'') detail,COALESCE(e.next_src,'') next_src,"
        "e.ms,e.wechat,e.ua FROM share_events e "
        "LEFT JOIN shares s ON s.id=e.sid "
        f"WHERE {cond} ORDER BY e.ts DESC, e.id DESC LIMIT ? OFFSET ?",
        (*params, size, (page - 1) * size), "all")
    return {"rows": [dict(r) for r in rows], "total": total,
            "page": page, "size": size,
            "pages": max(1, (total + size - 1) // size)}


@app.get("/api/admin/traffic-stats")
def admin_traffic_stats(request: Request, days: int = 30):
    """转发流量统计：两个同源视频路由的按天字节/次数聚合。

    scope=play 是播放兜底线路、scope=download 是视频下载；
    浏览器直连抖音 CDN 的流量不经过本服务器，无法也不需要统计。"""
    _require_admin(request)
    days = max(1, min(365, days))
    _flush_media_traffic()          # 先把内存里的增量落库，保证读到最新
    since = _today() - days + 1
    rows = db_exec("SELECT day,scope,requests,bytes FROM media_traffic "
                   "WHERE day>=? ORDER BY day DESC", (since,), "all")
    daily: dict = {}
    for r in rows:
        d = daily.setdefault(r["day"], {
            "day": r["day"],
            "date": time.strftime("%Y-%m-%d", time.gmtime(r["day"] * 86400)),
            "play_requests": 0, "play_bytes": 0,
            "download_requests": 0, "download_bytes": 0})
        if r["scope"] == "download":
            d["download_requests"] += r["requests"] or 0
            d["download_bytes"] += r["bytes"] or 0
        else:
            d["play_requests"] += r["requests"] or 0
            d["play_bytes"] += r["bytes"] or 0
    out = sorted(daily.values(), key=lambda x: -x["day"])
    totals = {k: sum(d[k] for d in out) for k in
              ("play_requests", "play_bytes", "download_requests", "download_bytes")}
    return {"days": days, "daily": out, "totals": totals}


@app.get("/api/admin/reports")
def admin_reports(request: Request, limit: int = 100):
    _require_admin(request)
    rows = db_exec("SELECT * FROM reports ORDER BY ts DESC LIMIT ?",
                   (max(1, min(500, limit)),), "all")
    return {"reports": [dict(r) for r in rows]}


@app.post("/api/admin/reports/{rid}/handle")
def admin_handle_report(rid: int, request: Request):
    _require_admin(request)
    db_exec("UPDATE reports SET handled=1 WHERE id=?", (rid,))
    return {"ok": True}


@app.get("/api/admin/share-config")
def admin_share_config(request: Request):
    _require_admin(request)
    off = _domains_off()
    return {
        "primary_domain": app_setting("share_primary_domain", ""),
        "domains": [{"url": d, "enabled": d not in off} for d in SHARE_DOMAINS],
        "env_hint": "主分享域名在此填写即时生效（无需重启）；额外的备用域名池仍通过环境变量 "
                    "SHARE_DOMAINS 配置（逗号分隔，重启生效）。",
        "wx": {"appid": app_setting("wx_appid"),
               "configured": bool(app_setting("wx_appid") and app_setting("wx_secret"))},
    }


class ShareConfigBody(BaseModel):
    wx_appid: Optional[str] = None
    wx_secret: Optional[str] = None
    toggle_domain: Optional[str] = None
    primary_domain: Optional[str] = None


@app.post("/api/admin/share-config")
def admin_set_share_config(body: ShareConfigBody, request: Request):
    _require_admin(request)
    if body.primary_domain is not None:
        d = body.primary_domain.strip().rstrip("/")
        if d and not d.startswith(("http://", "https://")):
            d = "https://" + d                 # 容错：只填了域名就补 https
        normalized = _normalize_origin_value(d)
        if d and not normalized:
            raise ApiError(422, "主分享域名必须是合法的 http(s) origin，不能含路径或账号信息")
        d = normalized
        set_app_setting("share_primary_domain", d)
    with _wx_ticket_lock:
        credentials_changed = False
        if body.wx_appid is not None:
            appid = body.wx_appid.strip()
            credentials_changed = appid != app_setting("wx_appid")
            set_app_setting("wx_appid", appid)
        if body.wx_secret is not None and body.wx_secret.strip():
            secret = body.wx_secret.strip()
            credentials_changed |= secret != app_setting("wx_secret")
            set_app_setting("wx_secret", secret)
        if credentials_changed:
            set_app_setting("wx_ticket", "")
            set_app_setting("wx_ticket_exp", 0)
    if body.toggle_domain:
        off = _domains_off()
        off.symmetric_difference_update({body.toggle_domain})
        set_app_setting("share_domains_off", json.dumps(sorted(off)))
    return admin_share_config(request)


# ---- 内置 mihomo（机场加速）----

@app.get("/api/admin/mihomo")
def admin_mihomo(request: Request):
    _require_admin(request)
    return mihomo_mgr.status()


class MihomoBody(BaseModel):
    sub_url: Optional[str] = None        # 填/改订阅 URL（空串=停用并清除）
    action: Optional[str] = None         # start / stop / restart


@app.post("/api/admin/mihomo")
def admin_set_mihomo(body: MihomoBody, request: Request):
    _require_admin(request)
    if body.sub_url is not None:
        set_app_setting("mihomo_sub_url", body.sub_url.strip())
        mihomo_mgr.reload()               # 重建配置并按有无订阅启停
    elif body.action == "stop":
        set_app_setting("mihomo_sub_url", "")
        mihomo_mgr.reload()
    elif body.action in ("start", "restart"):
        mihomo_mgr.reload()
    return mihomo_mgr.status()


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
    return create_api_key(u["id"], body.name)


@app.post("/api/keys/revoke")
def user_revoke_key(body: KeyBody, request: Request):
    u = current_user(request)
    if not u:
        raise ApiError(401, "请先登录")
    if not revoke_api_key(body.key, u["id"]):
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
    if not _douyin_browser_binary() or not _douyin_browser_enabled():
        print("⚠ 官方元数据补全缺少可用浏览器；请在管理后台的功能检查中查看依赖与修复提示。", flush=True)
    # 崩溃遗留的网页配额先退款；API 作业先恢复/对账，再清理过期明细。
    # cleanup 放在 legacy recovery 后，避免提前删掉旧 api_logs 导致无法重建已结算项。
    _prepare_share_parse_jobs()
    _refund_stale_quota_reservations()
    _prepare_api_jobs()
    _prepare_atc_jobs()
    _cleanup_retained_data(force=True)
    # 所有可能阻断 startup 的迁移/清理完成后才启动非 daemon worker，避免半启动悬挂。
    _start_share_parse_workers(prepared=True)
    _start_api_job_workers(prepared=True)
    _start_atc_workers(prepared=True)
    threading.Thread(target=_health_loop, daemon=True).start()
    threading.Thread(target=mihomo_mgr.supervise, daemon=True).start()


@app.on_event("shutdown")
def _stop_mihomo():
    _stop_share_parse_workers()
    _stop_api_job_workers()
    _stop_atc_workers()
    # 内存里的转发流量计数落库，重启不丢
    try:
        _flush_media_traffic()
    except Exception:
        pass
    # 关服务时杀掉内置 mihomo 子进程，避免留下孤儿进程占用端口
    try:
        mihomo_mgr.stop()
    except Exception:
        pass


# ---------------------------------------------------------------- 页面 + SEO

def _origin(request: Request) -> str:
    """返回不可由任意 Host 头污染的站点 origin。生产环境应显式配置 PUBLIC_ORIGIN。"""
    if PUBLIC_ORIGIN:
        return PUBLIC_ORIGIN

    configured = set(SHARE_DOMAINS)
    primary = _normalize_origin_value(app_setting("share_primary_domain", ""))
    if primary:
        configured.add(primary)
    if TRUST_PROXY:
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
        forwarded = _normalize_origin_value(f"{proto}://{host}")
        # 只有已配置的公开 origin 才能由可信反代头选中，避免反代原样透传 Host。
        if forwarded and forwarded in configured:
            return forwarded

    server = request.scope.get("server") or ("127.0.0.1", 80)
    hostname = str(server[0] or "127.0.0.1")
    try:
        port = int(server[1])
    except (TypeError, ValueError, IndexError):
        port = 80
    scheme = str(request.scope.get("scheme") or "http").lower()
    if scheme not in ("http", "https"):
        scheme = "http"
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    return (_normalize_origin_value(
        f"{scheme}://{rendered_host}{f':{port}' if port != default_port else ''}")
        or "http://127.0.0.1")


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


# 落地页 SEO 覆盖：键为路径，内容与该页可见文案保持一致（FAQ 与页面对应）
_LANDING_SEO = {
    "/transcript": {
        "meta": {
            "zh": {
                "title": "抖音文案提取 · 视频语音转文字 — 抖音无水印下载器",
                "desc": "把抖音视频里的语音自动转成文字：注册用户每天免费提取，标题、正文、口播全文一次拿全，适合素材收集与内容分析。开源可审查、不保存媒体文件。",
                "kw": "抖音文案提取,视频转文字,抖音语音转文字,口播文案提取,视频文案提取,douyin transcript,抖音字幕提取",
                "site": "抖音无水印下载器",
                "ogt": "抖音文案提取 · 视频语音一键转文字",
                "ogd": "粘贴抖音链接，自动把视频语音转成完整文字。注册用户每天免费提取，不保存媒体文件。",
                "locale": "zh_CN",
            },
            "en": {
                "title": "Douyin Transcript Extractor — Speech to Text, Free Daily",
                "desc": "Turn the speech in any Douyin video into text: title, caption and full transcript in one go. Free daily quota for signed-in users. Open source, no media-file storage.",
                "kw": "douyin transcript,video to text,douyin speech to text,extract video caption,douyin subtitle extractor",
                "site": "Douyin Downloader",
                "ogt": "Douyin Transcript Extractor — Speech to Text",
                "ogd": "Paste a Douyin link and get the full transcript of its speech. Free daily quota, no media-file storage.",
                "locale": "en_US",
            },
        },
        "ld": {
            "zh": {
                "app_desc": "抖音视频文案提取工具：粘贴链接即可把视频语音转成文字，同时获得标题与正文。注册用户每天免费提取，同一视频只计一次；本站不保存媒体文件。",
                "features": ["抖音视频语音转文字", "标题与正文提取", "音频试听", "注册用户每日免费", "结果可复制", "不保存媒体文件"],
                "faq": [
                    ("什么是抖音文案提取？", "把视频里的语音自动转成文字，同时保留作品的标题与正文，适合收集口播文案、做内容分析。语音转文字是需要主动开启的异步任务，通常 1–3 分钟完成。"),
                    ("文案提取收费吗？", "注册用户每天有免费提取次数（默认 5 次，以页面显示为准）。同一视频全站只提取一次，再次打开命中缓存不重复扣次。"),
                    ("我的链接会发给第三方吗？", "公开作品链接会提交给已配置的第三方内容解析服务，以获取作品信息和媒体地址；抖音缺失信息由服务器通过官方接口补全。普通解析默认不请求语音文案；只有你主动打开「获取文案」时才额外请求语音转文字。本站只保存处理结果元数据，不保存视频文件。"),
                    ("提取要等多久？", "通常 1–3 分钟，取决于视频时长，短视频更快。提交后可以离开页面，回来后重新打开开关即可查看结果。"),
                ],
                "howto": ("如何提取抖音视频文案", [
                    ("粘贴并解析", "把抖音分享链接粘贴到本站输入框，点击解析。"),
                    ("打开「获取文案」开关", "登录后在解析结果卡上打开「获取文案（语音转文字）」开关。未登录时点开关会引导你先登录。"),
                    ("等待并复制", "提取通常 1–3 分钟，完成后可一键复制全文或试听音频。")]),
            },
            "en": {
                "app_desc": "A Douyin transcript extractor: paste a link to turn a video's speech into text, together with its title and caption. Signed-in users get a free daily quota; only one extraction per video site-wide. No media files are stored.",
                "features": ["Douyin speech to text", "Title & caption extraction", "Audio preview", "Free daily quota", "One-click copy", "No media-file storage"],
                "faq": [
                    ("What is Douyin transcript extraction?", "It turns a video's speech into text and keeps the post's title and caption — built for collecting scripts and content analysis. Speech-to-text is an opt-in async job, usually done in 1–3 minutes."),
                    ("Is transcript extraction free?", "Signed-in users get a free daily quota (5/day by default, as shown on the page). Each video is extracted only once site-wide; reopening a cached result costs nothing."),
                    ("Is my link sent to a third party?", "Public post links are sent to the configured third-party content parsing service for metadata and media URLs. Missing Douyin information is supplemented through official interfaces on the server. Basic parsing does not request a speech transcript. Speech-to-text is requested only when you actively turn on Transcript. Only result metadata is kept — never the video file."),
                    ("How long does it take?", "Usually 1–3 minutes depending on video length; short clips are faster. You can leave after submitting — reopen the toggle later to see the result."),
                ],
                "howto": ("How to extract the transcript of a Douyin video", [
                    ("Paste and parse", "Paste the Douyin share link into the input box and click Parse."),
                    ("Turn on the transcript toggle", "After signing in, switch on “Transcript (speech to text)” on the result card. Signed-out users are guided to sign in first."),
                    ("Wait and copy", "Extraction usually takes 1–3 minutes. When done, copy the full text or listen to the audio.")]),
            },
        },
    },
}


def _seo_head(lang: str, origin: str, path = "/") -> str:
    """按语言生成整段 SEO 头（title/description/OG/Twitter/hreflang/JSON-LD）。"""
    zh = lang == "zh"
    base = f"{origin}{path}"
    canon = base if zh else f"{base}?lang=en"
    meta = {
        "zh": {
            "title": "多平台无水印下载器 · 支持抖音、小红书、B站、TikTok 等 50+ 平台",
            "desc": "免费的多平台视频与图集解析工具：支持抖音、小红书、快手、B站、微博、视频号、Twitter/X、TikTok、YouTube 等 50+ 平台。粘贴公开作品链接即可获取无水印原片、图集和作品信息；普通解析默认不获取语音文案。开源可审查、不保存媒体文件、无广告。",
            "kw": "多平台视频下载,无水印下载器,抖音下载,小红书视频下载,快手视频下载,B站视频下载,微博视频下载,TikTok downloader,YouTube downloader,视频号下载,图集下载",
            "site": "多平台无水印下载器",
            "ogt": "50+ 平台无水印下载 · 一个输入框统一解析",
            "ogd": "支持抖音、小红书、快手、B站、微博、TikTok、YouTube 等 50+ 平台；粘贴链接即可预览视频、图集与无水印原片。开源、无广告、不保存媒体文件。",
            "locale": "zh_CN",
        },
        "en": {
            "title": "Multi-platform Video Downloader — 50+ Platforms, No Watermark",
            "desc": "Parse public posts from 50+ platforms including Douyin, Xiaohongshu, Kuaishou, Bilibili, Weibo, TikTok and YouTube. Preview original videos and galleries with no media-file storage; transcript extraction is off by default.",
            "kw": "multi platform video downloader,no watermark downloader,douyin downloader,xiaohongshu downloader,kuaishou downloader,bilibili downloader,weibo downloader,tiktok downloader,youtube downloader,photo gallery downloader",
            "site": "Multi-platform Video Downloader",
            "ogt": "No-watermark downloads from 50+ content platforms",
            "ogd": "Paste one public post link to parse videos, galleries and post details from Douyin, Xiaohongshu, Kuaishou, Bilibili, Weibo, TikTok, YouTube and more.",
            "locale": "en_US",
        },
    }[lang]

    ld = {
        "zh": {
            "app_desc": "支持 50+ 内容平台的视频与图集解析工具：粘贴抖音、小红书、快手、B站、微博、视频号、Twitter/X、TikTok、YouTube 等平台的公开作品链接，即可预览无水印原片、图集与作品信息。基础解析不要求登录源平台，普通解析默认不获取语音文案，站点不保存媒体文件。",
            "features": ["50+ 内容平台统一解析", "抖音、小红书、快手、B站视频解析", "微博、视频号、Twitter/X 作品解析", "TikTok 与 YouTube 视频解析", "视频与图集预览", "批量解析与 Excel 导出", "抖音作品分享页", "可选语音文案提取", "媒体文件不落地", "开发者 API"],
            "faq": [
                ("这个多平台下载器会处理和保留哪些数据？", "前端代码开源可审查，本站不保存视频或图片文件。浏览器使用 30 天随机第一方匿名 ID；免费额度、防滥用和播放诊断会处理用途化网络/匿名 ID 摘要、粗粒度浏览器信息及事件。相关明细及 API 任务结果的保留期最多设为 30 天，到期后由每 5 分钟运行的任务删除。站内账号可选，注册会保存邮箱与加盐密码哈希。公开作品链接会提交给已配置的第三方内容解析服务，抖音缺失信息由服务器通过官方接口补全；普通解析默认不获取语音文案，只有用户主动打开「获取文案」时才请求语音转文字。媒体直连时媒体源会收到请求方网络与浏览器信息；安全的同源视频线路仅对已验证媒体域名流式转发。"),
                ("怎么把抖音视频分享到微信？发出去是卡片还是链接？", "解析后点「生成分享页」得到一条链接。想让好友收到带封面标题的卡片，要在微信里打开这个页面，再点右上角 ··· →「发送给朋友」，这样转发出去才是卡片。若只是复制链接粘贴到聊天窗口，微信不会把网址展开成卡片，会显示为一条普通网址（这是微信的机制，对任何网站都一样）。两种方式好友点开都能直接观看无水印原片，无需安装抖音 App、不用复制口令跳转。"),
                ("分享给朋友后，对方需要装抖音 App 吗？链接会过期吗？", "不需要装任何 App，用微信内置浏览器点开就能看。分享页匿名有效期 7 天、登录后 30 天；页面保存作品文案、互动数据和已获取的作者资料等信息，不存储任何视频文件，版权仍归原作者。你也可以生成带二维码的分享海报，长按保存后发朋友圈。"),
                ("需要登录或安装软件吗？", "无需登录源平台账号或安装软件。基础解析无需注册本站账号；API 控制台等账号功能需要登录。"),
                ("解析得到的视频有水印吗？", "本站优先展示可获取的无水印原片，也不会加入本站自己的二次水印；实际可提取内容以源平台和作品类型为准。"),
                ("支持图集（图片作品）下载吗？", "支持。图集作品会自动识别，可逐张下载原图，也可批量下载。"),
                ("有没有 API 可以批量调用？", "有。登录后可在 API 控制台生成密钥，通过异步接口批量提交链接并查询结果，按次计费。"),
                ("怎么提取抖音视频的文案（语音转文字）？", "解析后打开结果卡上的「获取文案（语音转文字）」开关即可自动提取，通常 1–3 分钟完成，可复制全文或试听音频。该功能需要登录，注册用户每天有免费提取次数；同一视频全站只提取一次，命中缓存不重复扣次。"),
            ],
            "howto": ("如何解析多平台无水印视频与图集", [
                ("复制公开作品链接", "在受支持的内容平台中打开分享功能，复制作品链接或整段分享文案。"),
                ("粘贴并解析", "把链接粘贴到输入框；多条链接会自动拆分并批量处理。"),
                ("预览或保存原片", "解析完成后预览视频或图集，并按当前平台可用的方式保存原片；抖音作品还可生成分享页。")]),
        },
        "en": {
            "app_desc": "A video and gallery parser for 50+ content platforms. Paste a public post link from Douyin, Xiaohongshu, Kuaishou, Bilibili, Weibo, WeChat Channels, Twitter/X, TikTok, YouTube and more to preview the original media and post details. Basic parsing requires no source-platform login, transcript extraction is off by default, and the site stores no media files.",
            "features": ["Unified parsing for 50+ platforms", "Douyin, Xiaohongshu, Kuaishou and Bilibili", "Weibo, WeChat Channels and Twitter/X", "TikTok and YouTube", "Video and gallery preview", "Batch parsing and Excel export", "Douyin share pages", "Optional speech transcript", "No media-file storage", "Developer API"],
            "faq": [
                ("What data does this downloader process and retain?", "The front end is open source and auditable, and the service stores no video or image files. A random first-party anonymous ID lasts 30 days. Purpose-specific network and anonymous-ID digests, coarse browser details, quota or diagnostic events, API jobs and results have a configurable 1–30 day retention period; expired records are removed by a cleanup task that runs every five minutes. A site account is optional and stores an email address and salted password hash. Public post links are sent to the configured third-party content parsing service for metadata and media URLs. Missing Douyin information is supplemented through official interfaces on the server. Normal parsing does not request a speech transcript; speech-to-text is requested only when the user actively turns on Transcript. Direct media requests disclose browser network information to the media host, video downloads prefer direct original-media requests and try the site's proxy if those fail."),
                ("How do I share a Douyin video to WeChat? Does it show as a card or a plain link?", "Create a share page after parsing. Pasting its URL into a chat produces a plain link. To send a card with a cover and title, open the page inside WeChat and forward it from the top-right menu. Either form opens without the Douyin app."),
                ("Do my friends need the Douyin app? Do share links expire?", "No app is needed — the page opens right in WeChat's built-in browser. Share pages last 7 days anonymously and 30 days when signed in. The page stores the post’s metadata, including its caption, engagement counts and available author details; no video files are stored and copyright stays with the original creator. You can also generate a poster with a QR code to save and post to Moments."),
                ("Do I need to log in or install anything?", "No Douyin login, app, or extension is required. Basic parsing needs no site account; account features such as the API console require sign-in."),
                ("Do downloaded videos have a watermark?", "No. You get the original video with no watermark, and we never add our own."),
                ("Can I download photo galleries (image posts)?", "Yes. Image posts are detected automatically; download each original image or batch-download them."),
                ("Is there an API for bulk use?", "Yes. After signing in you can create an API key in the console, submit links in bulk via the async API and poll for results, billed per request."),
                ("How do I extract the transcript of a Douyin video?", "After parsing, switch on the transcript toggle on the result card — extraction usually takes 1–3 minutes and the full text can be copied. Sign-in is required, with a free daily quota; each video is extracted only once site-wide."),
            ],
            "howto": ("How to parse videos and galleries from supported platforms", [
                ("Copy a public post link", "Use Share on a supported content platform and copy the post link or the complete share text."),
                ("Paste and parse", "Paste into the input box. Multiple links are detected and processed as a batch automatically."),
                ("Preview or save the original", "Preview the video or gallery and use the option available for that platform to save the original. Douyin posts can also become share pages.")]),
        },
    }[lang]

    # 落地页（如 /transcript）用独立文案覆盖默认首页 SEO
    landing = _LANDING_SEO.get(path)
    if landing:
        meta = landing["meta"][lang]
        ld = landing["ld"][lang]

    org_id = f"{origin}/#org"
    site_id = f"{origin}/#website"
    graph = [
        {"@type": "Organization", "@id": org_id, "name": meta["site"],
         "url": f"{origin}/", "logo": f"{origin}{_frontend_asset('/og.png')}"},
        {"@type": "WebSite", "@id": site_id, "name": meta["site"],
         "url": f"{origin}/", "publisher": {"@id": org_id},
         "inLanguage": ["zh-CN", "en"]},
        {"@type": "WebApplication", "name": meta["site"], "url": f"{origin}/",
         "applicationCategory": "MultimediaApplication", "operatingSystem": "All",
         "isPartOf": {"@id": site_id}, "publisher": {"@id": org_id},
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
<meta name="theme-color" content="#0E1013" media="(prefers-color-scheme:dark)">
<meta name="theme-color" content="#FFFFFF" media="(prefers-color-scheme:light)">
<meta name="author" content="{esc(meta["site"])}">
<meta name="application-name" content="{esc(meta["site"])}">
<meta name="apple-mobile-web-app-title" content="{esc(meta["site"])}">
<meta name="apple-mobile-web-app-capable" content="yes">
<link rel="preconnect" href="https://aweme.snssdk.com" crossorigin>
<link rel="dns-prefetch" href="https://aweme.snssdk.com">
<link rel="canonical" href="{canon}">
<link rel="alternate" hreflang="zh-CN" href="{base}">
<link rel="alternate" hreflang="en" href="{base}?lang=en">
<link rel="alternate" hreflang="x-default" href="{base}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="{esc(meta["site"])}">
<meta property="og:title" content="{esc(meta["ogt"])}">
<meta property="og:description" content="{esc(meta["ogd"])}">
<meta property="og:url" content="{canon}">
<meta property="og:image" content="{origin}{_frontend_asset('/og.png')}">
<meta property="og:image:type" content="image/png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="{esc(meta["site"])}">
<meta property="og:locale" content="{meta["locale"]}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{esc(meta["ogt"])}">
<meta name="twitter:description" content="{esc(meta["ogd"])}">
<meta name="twitter:image" content="{origin}{_frontend_asset('/og.png')}">
<script type="application/ld+json">{jsonld}</script>
<script>window.__LANG={lang!r};window.__ORIGIN={origin!r};</script>'''


# 自包含 HTML 的 JS/CSS 随页面更新；静态资源以构建时间戳区分缓存版本。
# 在进程启动时固定，同一版本的每次请求不随机破坏缓存。
_FRONTEND_MTIME = max(path.stat().st_mtime for path in
                      [Path(__file__), *Path("static").rglob("*")] if path.is_file())
FRONTEND_VERSION = f"{APP_VERSION}-" + time.strftime("%Y%m%d%H%M%S", time.gmtime(_FRONTEND_MTIME))


def _frontend_asset(path: str) -> str:
    return f"{path}?v={FRONTEND_VERSION}"


def _frontend_template(name: str) -> str:
    html = (Path("static") / name).read_text("utf-8")
    html = html.replace("{{FRONTEND_VERSION}}", FRONTEND_VERSION)
    if name in ("index.html", "share.html"):
        catalog = json.dumps(UI_MESSAGES, ensure_ascii=False).replace("<", "\\u003c")
        script = Path("static/ui-i18n.js").read_text("utf-8")
        html = html.replace("<head>", f"<head>\n<script>globalThis.__UI_MESSAGES={catalog};\n{script}</script>", 1)
    html = html.replace("<head>", '<head>\n'
                        f'<meta name="app-version" content="{APP_VERSION}">\n'
                        f'<meta name="frontend-version" content="{FRONTEND_VERSION}">', 1)
    # 只处理受控模板中的本地资源，不修改分享快照、外链或业务 API 参数。
    return re.sub(r'''((?:src|href)=["'])(/(?:platform-logos/[\w-]+\.svg|og\.(?:png|svg)))(["'])''',
                  lambda match: match[1] + _frontend_asset(match[2]) + match[3], html)


def _frontend_response(html: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(html, status_code=status_code, headers={
        "Cache-Control": "private, no-store", "Pragma": "no-cache",
        "X-App-Version": APP_VERSION, "X-Frontend-Version": FRONTEND_VERSION,
    })


_PLATFORM_LOGO_NAMES = frozenset({
    "xiaohongshu", "kuaishou", "bilibili", "pinduoduo", "twitter",
    "toutiao", "shipinhao", "weibo", "tiktok", "youtube",
})


@app.get("/platform-logos/{name}.svg", include_in_schema=False)
def platform_logo(name: str, v: str = ""):
    """Serve the small, allow-listed platform marks used by the home page."""
    if name not in _PLATFORM_LOGO_NAMES:
        return Response(status_code=404)
    return FileResponse(
        Path("static/platform-logos") / f"{name}.svg",
        media_type="image/svg+xml",
        headers={
            "Cache-Control": ("public, max-age=31536000, immutable"
                              if v == FRONTEND_VERSION else "no-cache"),
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    log_pageview(request)
    lang = _pick_lang(request)
    origin = _origin(request)
    html = _frontend_template("index.html")
    html = (html.replace("{{HTMLLANG}}", SUPPORTED_LANGS[lang])
                .replace("{{SEO_HEAD}}", _seo_head(lang, origin))
                .replace("{{ORIGIN}}", origin))
    resp = _frontend_response(html)
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["Pragma"] = "no-cache"
    resp.set_cookie("lang", lang, max_age=31536000, samesite="lax")
    return resp


def _share_head(view: Optional[dict], origin: str, lang: str = "zh") -> str:
    """分享页的 per-share 头信息。**一律 noindex** —— 不收录他人作品内容。"""
    def esc(s):
        return (str(s or "").replace("&", "&amp;").replace('"', "&quot;")
                .replace("<", "&lt;").replace(">", "&gt;"))

    if view and view["state"] in ("pending", "processing"):
        url = esc(view.get("url") or f"{origin}/s/{view.get('sid', '')}")
        return f'''<title>{_ui_text('视频正在准备中', lang)} · {_ui_text('分享页', lang)}</title>
<meta name="description" content="{_ui_text('链接已创建，内容正在后台获取，完成后页面会自动更新。', lang)}">
<meta name="robots" content="noindex,nofollow">
<meta name="theme-color" content="#0E1013">
<meta property="og:type" content="website">
<meta property="og:title" content="{_ui_text('视频正在准备中', lang)}">
<meta property="og:description" content="{_ui_text('内容获取完成后，打开该链接即可播放。', lang)}">
<meta property="og:image" content="{esc(origin)}{_frontend_asset('/og.png')}">
<meta property="og:image:type" content="image/png">
<meta property="og:url" content="{url}">'''
    if not view or view["state"] != "ok":
        return (f"<title>{_ui_text('内容不可用', lang)} · {_ui_text('分享页', lang)}</title>\n"
                '<meta name="robots" content="noindex,nofollow">\n'
                '<meta name="theme-color" content="#0E1013">')
    # 卡片大标题 = 抖音文案原文（与抖音里一模一样）；作者放进描述行。
    # 微信抓取网页 meta 生成卡片：title/og:title→标题，og:image→缩略图，description→摘要。
    # 卡片底部的"来源/抬头"由微信按域名自动填（域名或其绑定的公众号名称），网页无法自定义。
    platform = "TikTok" if (view.get("data") or {}).get("platform") == "tiktok" else ("Douyin" if lang == "en" else "抖音")
    title = (view["title"] or platform + " video")[:60]
    author = view["author"] or _ui_text("视频创作者", lang)
    desc = (f"{platform} video by @{author} · Watch without the app" if lang == "en"
            else f"@{author} 的 {platform} 作品 · 点开即可观看，无需安装 App")
    # 卡片图：抖音封面转成无签名 JPEG（webp 微信缩略图支持不稳定、签名 14 天过期）；
    # 兜底用 og.png 而非 og.svg —— 微信不渲染 SVG，会退化成无图的纯链接。
    cover = _card_cover(view["cover"]) or _atc_public_url(view["cover"]) or f"{origin}{_frontend_asset('/og.png')}"
    return f'''<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<meta name="robots" content="noindex,nofollow">
<meta name="theme-color" content="#0E1013">
<meta property="og:type" content="video.other">
<meta property="og:site_name" content="@{esc(author)}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:image" content="{esc(cover)}">
<meta property="og:image:type" content="image/jpeg">
<meta property="og:image:alt" content="{esc(title)}">
<meta property="og:url" content="{esc(view['url'])}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{esc(title)}">
<meta name="twitter:description" content="{esc(desc)}">
<meta name="twitter:image" content="{esc(cover)}">'''


@app.get("/s/{sid}", response_class=HTMLResponse)
def share_page(sid: str, request: Request):
    """分享页：服务端渲染，微信内可直接打开与播放。"""
    origin = _origin(request)
    lang = _pick_lang(request)
    row = db_exec("SELECT * FROM shares WHERE id=?", (sid,), "one")
    view = None
    if row:
        row = dict(row)
        # 页面只读已保存的快照；媒体端点在播放/下载时处理地址过期。
        view = _share_view(row, origin)
        # pending 页面完成后会自动 reload；只在终态记录一次，避免单次访问被算成 2 次。
        if view["state"] not in ("pending", "processing"):
            _share_event(request, sid, "view")

    html = _frontend_template("share.html")
    # 注入 <script> 前把 < 转义成 <，防止标题里的 </script> 打断脚本
    payload = json.dumps(view or {"state": "notfound", "sid": sid},
                         ensure_ascii=False).replace("<", "\\u003c")
    html = (html.replace("{{HTMLLANG}}", SUPPORTED_LANGS[lang])
                .replace("{{LANG}}", lang)
                .replace("{{SHARE_HEAD}}", _share_head(view, origin, lang))
                .replace("{{ORIGIN}}", origin)
                .replace("{{WECHAT}}", "true" if _is_wechat(request) else "false")
                .replace("{{SHARE_DATA}}", payload))
    resp = _frontend_response(html, status_code=200 if view else 404)
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Vary"] = "User-Agent, Accept-Language, Cookie"
    resp.set_cookie("lang", lang, max_age=31536000, samesite="lax", secure=COOKIE_SECURE)
    return resp


@app.get("/api-docs", response_class=HTMLResponse)
def api_docs(request: Request):
    log_pageview(request)
    lang = _pick_lang(request)
    origin = _origin(request)
    html = _frontend_template("api-docs.html")
    html = (html.replace("{{HTMLLANG}}", SUPPORTED_LANGS[lang])
                .replace("{{SEO_HEAD}}", _seo_head(lang, origin, "/api-docs"))
                .replace("{{ORIGIN}}", origin))
    resp = _frontend_response(html)
    resp.set_cookie("lang", lang, max_age=31536000, samesite="lax")
    return resp


@app.get("/transcript", response_class=HTMLResponse)
def transcript_page(request: Request):
    """文案提取落地页（SEO）：功能本体在首页结果卡，本页负责被搜到。"""
    log_pageview(request)
    lang = _pick_lang(request)
    origin = _origin(request)
    html = _frontend_template("transcript.html")
    html = (html.replace("{{HTMLLANG}}", SUPPORTED_LANGS[lang])
                .replace("{{SEO_HEAD}}", _seo_head(lang, origin, "/transcript"))
                .replace("{{ORIGIN}}", origin))
    resp = _frontend_response(html)
    resp.set_cookie("lang", lang, max_age=31536000, samesite="lax")
    return resp


@app.get("/api/quota")
def api_quota(request: Request):
    """前端查询今日剩余免费次数（含文案提取额度，供结果卡开关展示）。"""
    limit, used, remaining = quota_status(request)
    u = current_user(request)
    cfg = _atc_cfg()
    atc_on = cfg["enabled"] and cfg["transcript_enabled"]
    if u and atc_on:
        atc_limit, atc_used, atc_remaining = _atc_transcript_status(u["id"])
    else:
        # 匿名也返回真实每日上限（前端提示"登录后每天 N 次"要用），剩余恒 0
        atc_limit, atc_remaining = (cfg["transcript_daily"] if atc_on else 0), 0
    return {"limit": limit, "used": used, "remaining": remaining,
            "billing": _web_billing_status(u["id"] if u else None),
            "user_daily": limit if u else free_user_daily(),
            "user": {"email": u["email"]} if u else None,
            "transcript": {"enabled": atc_on,
                    "daily": atc_limit, "remaining": atc_remaining}}


# ---------------------------------------------------------------- 用户鉴权 API

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# 滑块漏斗观测（内存计数，重启清零；只看失败率趋势，不落库、不含个人标识）
_captcha_stats = {"load": 0, "ok": 0, "fail": 0}


@app.get("/api/auth/math")
def auth_math(request: Request):
    if not _captcha_rate_ok(_client_ip(request)):
        raise ApiError(429, "操作过于频繁，请稍后再试")
    return _make_math_challenge(request)


class MathAnswerBody(BaseModel):
    cid: str = Field(default="", max_length=128)
    answer: str = Field(default="", max_length=16)


@app.post("/api/auth/math/verify")
def auth_math_verify(body: MathAnswerBody, request: Request):
    if not _captcha_rate_ok(_client_ip(request)):
        raise ApiError(429, "操作过于频繁，请稍后再试")
    return {"ok": True, "math_token": _verify_math_challenge(body.cid, body.answer, request)}


@app.get("/api/auth/captcha")
def auth_captcha(request: Request):
    if not _captcha_rate_ok(_client_ip(request)):        # 防验证码 CPU-DoS
        raise ApiError(429, "操作过于频繁，请稍后再试")
    _require_math_grant(request)
    _captcha_stats["load"] += 1
    return make_captcha(request)


class CaptchaBody(BaseModel):
    cid: str = Field(default="", max_length=128)
    x: float = -1
    trajectory: list[dict] = Field(default_factory=list, max_length=256)
    nonce: str = Field(default="", max_length=128)


@app.post("/api/auth/captcha/verify")
def auth_captcha_verify(body: CaptchaBody, request: Request):
    """滑块校验独立成步。通过后返回一次性通行令牌，注册/登录必须携带它。"""
    ok, err = verify_captcha(body.cid, body.x, body.trajectory, body.nonce, request)
    _captcha_stats["ok" if ok else "fail"] += 1
    if not ok:
        raise ApiError(400, err)
    return {"ok": True, "pass_token": issue_pass(request)}


@app.get("/api/auth/me")
def auth_me(request: Request):
    u = current_user(request)
    if not u:
        return {"user": None}
    return {"user": {"email": u["email"], "id": u["id"], "created_at": u["created_at"]},
            "billing": _web_billing_status(u["id"])}


class RegisterBody(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)
    pass_token: str = Field(default="", max_length=512)  # 滑块通过后签发的一次性令牌
    hp: str = Field(default="", max_length=200)           # 蜜罐字段，正常用户为空


def _do_auth_guard(request: Request, body: RegisterBody):
    if body.hp:                                     # 蜜罐命中 → 机器人
        raise ApiError(400, "验证失败")
    if not _auth_rate_ok(_client_ip(request)):
        raise ApiError(429, "操作过于频繁，请一小时后再试")
    if not consume_pass(body.pass_token, request):  # 必须先过滑块拿到令牌
        raise ApiError(400, "请先完成滑块验证（验证已失效，请重试）")


def _issue_session(uid: int, request: Request) -> JSONResponse:
    tok = _new_user_session(uid, request.cookies.get("sess", ""))
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
                  (email, salt, h, int(time.time()), int(time.time()), ""))
    return _issue_session(uid, request)


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
    return _issue_session(row["id"], request)


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    tok = request.cookies.get("sess", "")
    _delete_user_session(tok)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("sess")
    return resp


@app.get("/api-console")
def api_console():
    return _frontend_response(_frontend_template("api-console.html"))


@app.get("/admin_d")
def admin_page():
    return _frontend_response(_frontend_template("admin.html"))


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots(request: Request):
    # /s/ 是用户生成的他人作品分享页，一律不收录（详见 docs/分享页功能规划.md §3.1）
    o = _origin(request)
    rules = "Allow: /\nDisallow: /admin_d\nDisallow: /api/\nDisallow: /s/\n"
    # 生成式引擎（GEO）：显式放行主流 AI 抓取器，规则同普通爬虫
    ai_bots = ["GPTBot", "OAI-SearchBot", "ChatGPT-User", "PerplexityBot",
               "ClaudeBot", "Claude-Web", "Google-Extended", "Applebot-Extended", "CCBot"]
    blocks = [f"User-agent: *\n{rules}"] + [f"User-agent: {b}\n{rules}" for b in ai_bots]
    return ("\n".join(blocks) + f"\nLLM: {o}/llms.txt\nSitemap: {o}/sitemap.xml\n")


@app.get("/llms.txt", response_class=PlainTextResponse)
def llms_txt(request: Request):
    # 面向大模型/生成式引擎的站点说明（llmstxt.org 约定），帮助其准确引用本站
    o = _origin(request)
    return f"""# 多平台无水印下载器（Multi-platform Video Downloader）

> 免费、开源的多平台视频与图集解析工具。支持抖音、小红书、快手、B站、多多视频、Twitter/X、今日头条、视频号、微博、TikTok、YouTube 等 50+ 平台。粘贴公开作品链接即可预览无水印原片、图集和作品信息；普通解析默认不获取语音文案。抖音作品另可生成微信友好的分享页。本站不落地保存媒体文件，基础解析无需账号，站内账号与开发者 API 可选，无广告。

## 核心特性
- **50+ 平台统一解析**：一个输入框识别抖音、小红书、快手、B站、微博、视频号、Twitter/X、TikTok、YouTube 等公开作品链接。
- 抖音无水印原片：通过隔离 Chromium 捕获抖音官方 detail 数据和短时 CDN 地址；其他平台的能力以兼容适配器、源平台和作品类型为准。
- 图集（图片作品）下载：自动识别多图作品，可逐张或批量下载原图。
- **一键生成分享页**：把抖音作品变成一个网页，发给朋友点开即看。
- **分享到微信显示为卡片**：在微信内打开分享页，点右上角 ··· 转发，好友收到带封面标题的卡片（直接粘贴网址则是纯链接，这是微信机制）。
- **免 App 观看**：接收方无需安装抖音、无需登录，微信内置浏览器直接播放。
- **分享海报**：前端合成带二维码的海报图，长按保存后可发朋友圈；链接被拦截时的传播兜底。
- 在线预览：下载前可直接在网页中预览播放。
- **按平台解析**：公开作品链接会提交给配置的第三方内容解析服务；抖音缺失的信息由服务器通过官方接口补全。普通解析默认不请求语音文案。
- **文案提取（语音转文字）**：解析后主动打开「获取文案」开关，才会额外请求语音转文字；注册用户每天免费提取，同一视频全站只计一次。
- 可靠媒体链路：视频与抖音图片均有受签名/限流保护的同源流式转发，本站不落地、不留存媒体。
- 开源可审查、数据最小化：不保存媒体文件；免费额度和诊断只处理必要的用途化摘要、粗粒度环境与事件，保留期最多设为 30 天，到期后由每 5 分钟运行的任务删除；站内账号可选。
- 开发者 API：登录后于控制台生成密钥，异步批量提交链接、轮询结果，按次计费。

## 使用方式
1. 在受支持的内容平台点「分享 → 复制链接」，得到分享文案或作品链接。
2. 打开 {o}/ ，把链接粘贴进输入框并解析。
3. 在线预览视频或图集，按页面提供的当前平台方式保存原片；抖音作品还可点「生成分享页」发给微信好友。

## 常见问答
- 会处理和保留哪些数据？——不保存视频或图片文件。公开作品链接会提交给配置的第三方内容解析服务；抖音缺失的信息由服务器通过官方接口补全。普通解析默认不请求语音文案。浏览器使用 30 天随机匿名 ID；免费额度、防滥用和播放诊断会处理用途化网络/匿名 ID 摘要、粗粒度浏览器信息与事件。相关明细及 API 任务结果的保留期最多设为 30 天，到期后由每 5 分钟运行的任务删除。站内账号可选并保存邮箱与加盐密码哈希；媒体直连时，媒体源会收到请求方网络与浏览器信息。
- 怎么把抖音视频分享到微信？——解析后生成分享页。想发出带封面标题的卡片，需在微信里打开该页面，点右上角 ··· →「发送给朋友」；直接复制链接粘贴进聊天窗口不会展开成卡片，只显示为一条网址（微信机制，对所有网站一致）。两种方式好友点开都能直接观看无水印原片，无需装抖音 App。
- 对方需要装抖音 App 吗？会过期吗？——不需要装 App，微信内直接看；分享页匿名 7 天、登录后 30 天有效，保存文案、互动统计及作者公开资料等元数据，不存储视频文件。
- 需要登录或装软件吗？——无需登录源平台或安装软件；基础解析无需本站账号，API 控制台等账号功能需要登录。
- 下载的视频有水印吗？——没有，是无水印原片，也不加本站二次水印。
- 支持图集吗？——支持，自动识别并可批量下载原图。
- 有批量 API 吗？——有，登录后在 API 控制台生成密钥调用。

## 相关链接
- 首页（下载 + 生成分享页）：{o}/
- 文案提取（语音转文字）：{o}/transcript
- API 文档：{o}/api-docs
- API 控制台：{o}/api-console
"""


@app.get("/sitemap.xml")
def sitemap(request: Request):
    o = _origin(request)

    def entry(path, pri):
        return (f'  <url><loc>{o}{path}</loc>'
                f'<xhtml:link rel="alternate" hreflang="zh-CN" href="{o}{path}"/>'
                f'<xhtml:link rel="alternate" hreflang="en" href="{o}{path}?lang=en"/>'
                f'<lastmod>{_BUILD_DATE}</lastmod>'
                f'<changefreq>daily</changefreq><priority>{pri}</priority></url>\n')

    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
           'xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
           + entry("/", "1.0") + entry("/api-docs", "0.7")
           + entry("/transcript", "0.8")
           + '</urlset>\n')
    return Response(xml, media_type="application/xml")


@app.get("/og.svg")
def og_image(v: str = ""):
    # SVG 是可维护源；static/og.png 由 tools/render_og.swift 从同一文件生成。
    return FileResponse("static/og.svg", media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"
                                 if v == FRONTEND_VERSION else "no-cache"})


@app.get("/og.png")
def og_png(v: str = ""):
    # 社交/微信卡片首选位图：微信、多数抓取器不渲染 SVG，PNG 才能出图（og.svg 保留兜底）
    return FileResponse("static/og.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"
                                 if v == FRONTEND_VERSION else "no-cache"})


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": APP_VERSION, "proxies": len(proxy_mgr.proxies),
            "enabled": sum(p["enabled"] for p in proxy_mgr.proxies)}
