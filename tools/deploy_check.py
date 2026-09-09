#!/usr/bin/env python3
"""只读部署预检；不导入 server、不迁移数据库、不调用解析服务或修改余额。"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
from urllib import request


REQUIRED_COLUMNS = {
    'users': {'balance_cents', 'reserved_cents', 'spent_cents', 'wallet_version'},
    'user_sessions': {'token_hash', 'user_id', 'expires_at'},
    'wallet_ledger': {'user_id', 'event_key', 'balance_delta', 'reserved_delta', 'spent_delta'},
    'quota_reservations': {'user_id', 'free_units', 'price_cents', 'status'},
    'shares': {'source_url', 'payload', 'parse_status', 'assigned_origin', 'expires_at'},
    'parse_snapshots': {'source_url', 'canonical_url', 'payload', 'expires_at'},
    'api_keys': {'balance_cents', 'reserved_cents', 'spent_cents'},
    'job_items': {'lease_owner', 'lease_until', 'price_cents', 'reserved', 'status'},
    'atc_cache': {'work_url', 'video_url', 'url_fetched_at'},
    'atc_jobs': {'purpose', 'lease_owner', 'lease_until', 'quota_reservation_id'},
    'app_settings': {'k', 'v'},
}


def check_database(data_dir):
    checks = []
    path = Path(data_dir) / 'app.db'
    if not path.is_file():
        return [{'id': 'database', 'status': 'FAIL', 'detail': '数据库不存在；请核对实际 DATA_DIR'}]
    try:
        # mode=ro 避免错误路径生成空库；query_only 再禁止写语句。
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10) as conn:
            conn.execute('PRAGMA query_only=ON')
            healthy = conn.execute('PRAGMA quick_check').fetchall() == [('ok',)]
            checks.append({'id': 'database_integrity', 'status': 'PASS' if healthy else 'FAIL',
                           'detail': 'SQLite quick_check'})
            schema_ok = True
            for table, expected in REQUIRED_COLUMNS.items():
                actual = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
                missing = sorted(expected - actual)
                schema_ok &= not missing
                checks.append({'id': 'schema_' + table, 'status': 'FAIL' if missing else 'PASS',
                               'detail': '缺少列：' + ', '.join(missing) if missing else '必要列齐全'})
            if schema_ok:
                invalid = sum(conn.execute(
                    f'SELECT COUNT(*) FROM {table} WHERE balance_cents<0 OR reserved_cents<0 OR spent_cents<0'
                ).fetchone()[0] for table in ('users', 'api_keys'))
                checks.append({'id': 'balances', 'status': 'FAIL' if invalid else 'PASS',
                               'detail': f'负余额/预留/累计扣费异常记录：{invalid}；不替代逐笔对账'})
        private = not bool(path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))
        checks.append({'id': 'database_permissions', 'status': 'PASS' if private else 'WARN',
                       'detail': '数据库应仅允许运行账号访问'})
    except (OSError, sqlite3.Error):
        checks.append({'id': 'database_read', 'status': 'FAIL', 'detail': '数据库不可读取，请检查权限与锁'})
    return checks


def check_http(base_url, expected_version):
    checks = []
    for path, label in (('/healthz', 'running_version'), ('/', 'frontend_version')):
        try:
            req = request.Request(base_url.rstrip('/') + path, headers={'Cache-Control': 'no-cache'})
            with request.urlopen(req, timeout=15) as response:
                payload = response.read(2 * 1024 * 1024)
                if path == '/healthz':
                    body = json.loads(payload)
                    version = body.get('version') if isinstance(body, dict) else None
                    ok = isinstance(body, dict) and body.get('ok') is True and version == expected_version
                else:
                    version = response.headers.get('X-App-Version')
                    ok = (version == expected_version
                          and 'no-store' in response.headers.get('Cache-Control', '')
                          and b'{{FRONTEND_VERSION}}' not in payload)
                checks.append({'id': label, 'status': 'PASS' if ok else 'FAIL',
                               'detail': f'期望 {expected_version}，实际 {version or "无版本标识"}'})
        except Exception:
            checks.append({'id': label, 'status': 'FAIL', 'detail': '请求失败或响应格式异常'})
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:3344')
    parser.add_argument('--data-dir', help='可选；必须是运行进程实际使用的数据目录')
    parser.add_argument('--expected-version')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    expected = args.expected_version or re.search(
        r'^APP_VERSION = "([^"]+)"', (root / 'server.py').read_text(), re.M).group(1)
    checks = check_http(args.base_url, expected)
    if args.data_dir:
        checks.extend(check_database(args.data_dir))
        stable = bool(os.environ.get('APP_SECRET') or os.environ.get('CAPTCHA_SECRET')
                      or (Path(args.data_dir) / '.app-secret').is_file())
        checks.append({'id': 'signing_key', 'status': 'PASS' if stable else 'WARN',
                       'detail': '仅检查密钥来源存在；还须确认服务使用同一密钥且已备份'})
        binary = (os.environ.get('DOUYIN_BROWSER_BIN') or shutil.which('chromium')
                  or shutil.which('google-chrome-stable') or shutil.which('google-chrome'))
        available = bool(binary and os.path.isfile(binary) and os.access(binary, os.X_OK))
        checks.append({'id': 'browser_binary', 'status': 'WARN',
                       'detail': '已找到浏览器；仍需后台检查真实启动' if available else '未找到浏览器，请在运行环境核对'})
    print(json.dumps({'expected_version': expected, 'checks': checks,
                      'scope': '只读预检；不代表主服务鉴权、完整下载、微信或计费实测通过'},
                     ensure_ascii=False, indent=2))
    return int(any(item['status'] == 'FAIL' for item in checks))


if __name__ == '__main__':
    raise SystemExit(main())
