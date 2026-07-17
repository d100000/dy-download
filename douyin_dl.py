#!/usr/bin/env python3
"""抖音无水印下载器 — 免登录、免签名，仅依赖 Python 标准库。

用法:
    python3 douyin_dl.py "分享文案或短链" [输出目录]

示例:
    python3 douyin_dl.py "3.89 复制打开抖音... https://v.douyin.com/xxxx/ ..."
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1"
)


def http_get(url: str, follow: bool = True) -> tuple[int, str, bytes]:
    """返回 (状态码, 最终URL/Location, 响应体)。follow=False 时不跟随重定向。"""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    req = urllib.request.Request(url, headers={"User-Agent": UA})
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.status, resp.url, resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return e.code, e.headers.get("Location", ""), b""
        raise


def extract_short_link(text: str) -> str:
    m = re.search(r"https://v\.douyin\.com/[\w-]+/?", text)
    if not m:
        sys.exit("错误：文案中未找到 v.douyin.com 短链")
    return m.group(0)


def resolve_item(short_link: str) -> tuple[str, str]:
    """解析短链，返回 (类型 video/note, 作品ID)。"""
    _, location, _ = http_get(short_link, follow=False)
    m = re.search(r"/share/(video|note)/(\d+)", location)
    if not m:
        sys.exit(f"错误：链接已失效或类型不支持（跳转到: {location[:80]}）")
    return m.group(1), m.group(2)


def fetch_router_data(kind: str, item_id: str) -> dict:
    _, _, body = http_get(f"https://www.iesdouyin.com/share/{kind}/{item_id}/")
    m = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", body.decode("utf-8"), re.S)
    if not m:
        sys.exit("错误：分享页中未找到 _ROUTER_DATA（页面结构可能已变更）")
    return json.loads(m.group(1))


def find_key(obj, key):
    """递归查找 JSON 中所有名为 key 的值（容错页面结构变动）。"""
    if isinstance(obj, dict):
        if key in obj:
            yield obj[key]
        for v in obj.values():
            yield from find_key(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from find_key(v, key)


def safe_filename(desc: str, fallback: str) -> str:
    name = re.sub(r"#\S+", "", desc).strip()          # 去话题标签
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_")
    return (name or fallback)[:60]


def download(url: str, dest: Path) -> None:
    _, _, body = http_get(url)
    if len(body) < 10 * 1024:
        sys.exit("错误：下载内容过小，可能被风控拦截，请稍后重试")
    dest.write_bytes(body)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)

    short = extract_short_link(sys.argv[1])
    print(f"短链: {short}")
    kind, item_id = resolve_item(short)
    print(f"作品: {kind} / {item_id}")

    data = fetch_router_data(kind, item_id)
    items = next((i for i in find_key(data, "item_list") if i), None)
    if not items:
        sys.exit("错误：视频不存在、已删除或为私密作品")
    item = items[0]

    desc = item.get("desc", "")
    author = next(find_key(item, "nickname"), "")
    print(f"标题: {desc}\n作者: {author}")
    base = safe_filename(desc, item_id)

    if kind == "note":  # 图集
        images = item.get("images") or []
        if not images:
            sys.exit("错误：图集中未找到图片")
        for idx, img in enumerate(images, 1):
            dest = out_dir / f"{base}_{idx:02d}.jpeg"
            download(img["url_list"][0], dest)
            print(f"已保存: {dest.name}")
        print(f"完成：共 {len(images)} 张图片 → {out_dir}")
        return

    play_url = next(find_key(item, "play_addr"), {}).get("url_list", [None])[0]
    if not play_url:
        sys.exit("错误：未找到播放地址")
    play_url = play_url.replace("/playwm/", "/play/")  # 去水印
    dest = out_dir / f"{base}.mp4"
    print("下载中（无水印）...")
    download(play_url, dest)
    size_mb = dest.stat().st_size / 1024 / 1024
    print(f"完成: {dest}（{size_mb:.1f} MB）")


if __name__ == "__main__":
    main()
