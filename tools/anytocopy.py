#!/usr/bin/env python3
"""AnyToCopy 开放 API 本地调试工具（纯标准库，无第三方依赖）。

用途：配置 API 密钥 → 提交视频提取任务 → 轮询查询结果（含无水印 videoUrl）。
密钥保存在 data/anytocopy.json（权限 0600，data/ 已在 .gitignore 中，不会进仓库）。

用法：
  python3 tools/anytocopy.py config                     # 交互式配置 API Key / Secret
  python3 tools/anytocopy.py extract "<作品链接或分享文案>"   # 默认只提取媒体，不获取文案
  python3 tools/anytocopy.py extract --with-text "<链接>"   # 主动请求语音转文字
  python3 tools/anytocopy.py extract --no-wait "<链接>"     # 只提交，打印 taskId 后退出
  python3 tools/anytocopy.py query <taskId>             # 按 taskId 查询一次
  python3 tools/anytocopy.py show                       # 查看当前已保存的配置（Secret 打码）
"""

import getpass
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib import error as urlerr
from urllib import parse as urlparse
from urllib import request as urlreq

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "data" / "anytocopy.json"
DEFAULT_BASE = "https://api.anytocopy.com/vip/open-api/v1"
POLL_INTERVAL = 4      # 官方建议 3-5 秒
POLL_MAX_TIMES = 60    # 官方建议最多轮询 60 次


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text("utf-8"))
        except (OSError, ValueError):
            pass
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    CONFIG_FILE.chmod(0o600)


def cmd_config() -> int:
    cfg = load_config()
    print("配置 AnyToCopy API 密钥（直接回车保留原值）")
    old_key = cfg.get("api_key", "")
    api_key = input(f"API Key{f' [已保存: {old_key[:6]}...]' if old_key else ''}: ").strip() or old_key
    old_secret = cfg.get("api_secret", "")
    api_secret = getpass.getpass(
        f"API Secret{' [已保存: ****]' if old_secret else ''}（输入不显示）: "
    ).strip() or old_secret
    if not api_key or not api_secret:
        print("错误：API Key 与 Secret 都不能为空", file=sys.stderr)
        return 1
    save_config({"api_key": api_key, "api_secret": api_secret})
    print(f"已保存到 {CONFIG_FILE}（权限 0600）")
    return 0


def cmd_show() -> int:
    cfg = load_config()
    if not cfg:
        print("尚未配置，请先运行: python3 tools/anytocopy.py config")
        return 1
    secret = cfg.get("api_secret", "")
    masked = (secret[:3] + "****" + secret[-2:]) if len(secret) > 5 else "****"
    print(f"API Key : {cfg.get('api_key', '')}")
    print(f"Secret  : {masked}")
    print(f"Base URL: {DEFAULT_BASE}（固定官方接口）")
    return 0


def _request(method: str, path: str, params: dict, cfg: dict) -> dict:
    url = DEFAULT_BASE + path + "?" + urlparse.urlencode(params)
    req = urlreq.Request(url, method=method)
    req.add_header("X-API-Key", cfg["api_key"])
    req.add_header("X-API-Secret", cfg["api_secret"])
    req.add_header("User-Agent", "anytocopy-local-test/1.0")
    try:
        with urlreq.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urlerr.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        raise SystemExit(f"HTTP {e.code}: {body or e.reason}")
    except urlerr.URLError as e:
        raise SystemExit(f"网络错误: {e.reason}")
    try:
        return json.loads(body)
    except ValueError:
        raise SystemExit(f"响应不是 JSON: {body[:500]}")


def extract_url(text: str) -> str:
    """从整段分享文案中抠出第一个 http(s) 链接，抠不到就按原文返回。"""
    m = re.search(r"https?://[^\s，。\"'<>]+", text)
    return m.group(0) if m else text.strip()


def require_config() -> dict:
    cfg = load_config()
    if not cfg.get("api_key") or not cfg.get("api_secret"):
        raise SystemExit("尚未配置密钥，请先运行: python3 tools/anytocopy.py config")
    return cfg


def cmd_extract(args: list) -> int:
    wait = "--no-wait" not in args
    include_text = "--with-text" in args
    args = [value for value in args
            if value not in ("--no-wait", "--with-text")]
    if not args:
        print("用法: python3 tools/anytocopy.py extract [--no-wait] [--with-text] "
              "\"<作品链接或分享文案>\"", file=sys.stderr)
        return 1
    cfg = require_config()
    work_url = extract_url(args[0])
    print(f"提交任务: {work_url}")
    params = {"workUrl": work_url}
    if include_text:
        params["taskType"] = "TEXT"
    resp = _request("POST", "/video/extract", params, cfg)
    print(f"提交响应: {json.dumps(resp, ensure_ascii=False)}")
    if resp.get("code") != 200 or not resp.get("data"):
        return 1
    task_id = resp["data"]
    print(f"taskId = {task_id}")
    if not wait:
        print(f"稍后可用: python3 tools/anytocopy.py query {task_id}")
        return 0
    for i in range(1, POLL_MAX_TIMES + 1):
        time.sleep(POLL_INTERVAL)
        resp = _request("GET", "/video/query", {"taskId": task_id}, cfg)
        data = resp.get("data") or {}
        status = data.get("status", "?")
        print(f"[{i}/{POLL_MAX_TIMES}] status={status} {data.get('errorMessage', '')}")
        if status in ("SUCCESS", "FAILED", "FAILURE"):
            print(json.dumps(data, ensure_ascii=False, indent=2))
            if status == "SUCCESS" and data.get("videoUrl"):
                print(f"\n无水印视频地址:\n{data['videoUrl']}")
            return 0 if status == "SUCCESS" else 1
    print("轮询超时（任务仍在处理），稍后可用 query 子命令再查", file=sys.stderr)
    return 2


def cmd_query(args: list) -> int:
    if not args:
        print("用法: python3 tools/anytocopy.py query <taskId>", file=sys.stderr)
        return 1
    cfg = require_config()
    resp = _request("GET", "/video/query", {"taskId": args[0]}, cfg)
    print(json.dumps(resp, ensure_ascii=False, indent=2))
    data = resp.get("data") or {}
    if data.get("status") == "SUCCESS" and data.get("videoUrl"):
        print(f"\n无水印视频地址:\n{data['videoUrl']}")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "config":
        return cmd_config()
    if cmd == "show":
        return cmd_show()
    if cmd == "extract":
        return cmd_extract(args)
    if cmd == "query":
        return cmd_query(args)
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
