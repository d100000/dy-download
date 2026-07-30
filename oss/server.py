#!/usr/bin/env python3
"""抖音无水印下载器 · 开源基础版 (Douyin Downloader · Open-Source Edition)

免登录，粘贴分享链接即可在线预览并下载无水印视频 / 图集。
视频与图片优先由用户浏览器直连抖音 CDN；直连受限时由服务器流式转发，
全程不落地、不存储内容。

启动:  uvicorn server:app --host 0.0.0.0 --port 8000 --no-access-log
可选:  环境变量 PROXY=socks5://user:pass@host:port 让服务器解析走代理（防封 IP）

> 这是最小可用的开源版。完整版（管理后台、代理池、用户体系、异步计费 API、
> 数据分析）不在本仓库开源，如需请联系作者，见 README。
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from urllib import error as urlerr
from urllib import parse as urlparse
from urllib import request as urlreq

from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, Response, StreamingResponse)
from pydantic import BaseModel

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1")
CDN_HEADERS = {"Referer": "https://www.douyin.com/"}
PROXY = os.environ.get("PROXY", "").strip()
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
try:
    DATA_DIR.chmod(0o700)
except OSError:
    pass


def _load_app_secret() -> bytes:
    """读取稳定密钥；未配置时以完整临时文件 + hard-link 原子落盘。"""
    configured = (os.environ.get("APP_SECRET") or "").strip()
    if configured:
        if len(configured.encode()) < 32:
            raise RuntimeError("APP_SECRET 至少需要 32 字节")
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
            # 同目录 hard-link：目标只会指向已经完整写入的 inode，多进程竞争时仅一个成功。
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
MEDIA_TOKEN_TTL = max(300, min(86400, int(os.environ.get("MEDIA_TOKEN_TTL", "43200"))))
MEDIA_REQUESTS_PER_MIN = max(1, int(os.environ.get("MEDIA_REQUESTS_PER_MIN", "120")))
MEDIA_MAX_CONCURRENT = max(1, int(os.environ.get("MEDIA_MAX_CONCURRENT", "6")))
TRUST_PROXY = os.environ.get("TRUST_PROXY", "").lower() in ("1", "true", "yes")
TRUST_PROXY_HOPS = max(1, int(os.environ.get("TRUST_PROXY_HOPS", "1")))

app = FastAPI(title="抖音无水印下载器 · 开源版")


@app.middleware("http")
async def _private_api_responses(request: Request, call_next):
    """解析与媒体 API 含短时签名地址，禁止浏览器或共享代理持久化。"""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Pragma"] = "no-cache"
    return response


class ApiError(Exception):
    def __init__(self, status, message, headers=None):
        self.status, self.message = status, message
        self.headers = headers or {}


@app.exception_handler(ApiError)
async def _err(_, exc):
    return JSONResponse(status_code=exc.status, content={"error": exc.message},
                        headers=exc.headers)


def _opener(follow=True):
    handlers = []
    if PROXY:
        sch = PROXY.split("://", 1)[0].lower()
        if sch in ("http", "https"):
            handlers.append(urlreq.ProxyHandler({"http": PROXY, "https": PROXY}))
        elif sch.startswith("socks"):
            import socks
            from sockshandler import SocksiPyHandler
            p = urlparse.urlsplit(PROXY)
            st = socks.SOCKS4 if sch.startswith("socks4") else socks.SOCKS5
            handlers.append(SocksiPyHandler(st, p.hostname, p.port or 1080,
                                            rdns=sch.endswith("h"),
                                            username=p.username, password=p.password))
    if not follow:
        class NR(urlreq.HTTPRedirectHandler):
            def redirect_request(self, *a):
                return None
        handlers.append(NR())
    return urlreq.build_opener(*handlers)


def _open(url, follow=True, headers=None):
    req = urlreq.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        return _opener(follow).open(req, timeout=30)
    except urlerr.HTTPError as e:
        if not follow and e.code in (301, 302, 303, 307, 308):
            return e
        raise


def _find(o, k):
    if isinstance(o, dict):
        if k in o:
            yield o[k]
        for v in o.values():
            yield from _find(v, k)
    elif isinstance(o, list):
        for v in o:
            yield from _find(v, k)


def _safe(desc, fb):
    n = re.sub(r"#\S+", "", desc or "").strip()
    n = re.sub(r'[\\/:*?"<>|\s]+', "_", n).strip("_")
    return (n or fb)[:60]


def _video_signature(vid: str, exp: int) -> str:
    """签名只绑定资源和过期时间；dl/name 可由前端追加，不影响验签。"""
    payload = f"media:v1\nvideo\n{vid}\n{exp}".encode()
    return hmac.new(APP_SECRET, payload, hashlib.sha256).hexdigest()


def _video_url(vid: str, filename: str = "", download: bool = False) -> str:
    exp = int(time.time()) + MEDIA_TOKEN_TTL
    query = {"exp": str(exp), "sig": _video_signature(vid, exp)}
    if download:
        query["dl"] = "1"
        query["name"] = filename or "video.mp4"
    return f"/api/video/{urlparse.quote(vid, safe='_-')}?" + urlparse.urlencode(query)


def _require_video_token(vid: str, exp: str, sig: str) -> None:
    try:
        expiry = int(exp)
    except (TypeError, ValueError):
        expiry = 0
    expected = _video_signature(vid, expiry)
    now = int(time.time())
    if (expiry < now or expiry > now + MEDIA_TOKEN_TTL + 300
            or not re.fullmatch(r"[0-9a-f]{64}", sig or "")
            or not hmac.compare_digest(sig, expected)):
        raise ApiError(403, "媒体链接无效或已过期，请重新解析")


def _client_ip(request: Request) -> str:
    """仅在显式信任反代时读取 XFF，并从右侧按可信代理层数取客户端地址。"""
    peer = request.client.host if request.client else "?"
    if TRUST_PROXY:
        parts = [p.strip() for p in
                 (request.headers.get("x-forwarded-for") or "").split(",") if p.strip()]
        if len(parts) >= TRUST_PROXY_HOPS:
            return parts[-TRUST_PROXY_HOPS][:64]
    return str(peer)[:64]


_media_lock = threading.Lock()
_media_hits: dict[str, list[float]] = {}
_media_active: dict[str, int] = {}
_media_last_sweep = 0.0


class _MediaLease:
    def __init__(self, ip: str):
        self.ip = ip
        self.released = False


def _media_acquire(request: Request) -> _MediaLease:
    """按 IP 预占一次媒体请求和一个流式并发槽；返回释放租约所需的 IP。"""
    global _media_last_sweep
    ip = _client_ip(request)
    now = time.monotonic()
    with _media_lock:
        if now - _media_last_sweep >= 60:
            for key, values in list(_media_hits.items()):
                fresh = [t for t in values if now - t < 60]
                if fresh:
                    _media_hits[key] = fresh
                elif not _media_active.get(key):
                    _media_hits.pop(key, None)
            _media_last_sweep = now

        hits = [t for t in _media_hits.get(ip, []) if now - t < 60]
        if len(hits) >= MEDIA_REQUESTS_PER_MIN:
            _media_hits[ip] = hits
            raise ApiError(429, "媒体请求过于频繁，请稍后重试",
                           {"Retry-After": "60"})
        if _media_active.get(ip, 0) >= MEDIA_MAX_CONCURRENT:
            raise ApiError(429, "同时播放或下载数量过多，请稍后重试",
                           {"Retry-After": "2"})
        hits.append(now)
        _media_hits[ip] = hits
        _media_active[ip] = _media_active.get(ip, 0) + 1
    return _MediaLease(ip)


def _media_release(lease: _MediaLease) -> None:
    with _media_lock:
        if lease.released:
            return
        lease.released = True
        active = _media_active.get(lease.ip, 0) - 1
        if active > 0:
            _media_active[lease.ip] = active
        else:
            _media_active.pop(lease.ip, None)


class _MediaStreamingResponse(StreamingResponse):
    """确保响应头发送失败或流中断时也释放媒体并发租约。"""
    def __init__(self, *args, finalize, **kwargs):
        self._media_finalize = finalize
        super().__init__(*args, **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._media_finalize()


def _media_finalizer(upstream, lease: _MediaLease):
    """返回线程安全、幂等的上游关闭 + 并发租约释放函数。"""
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

    return finalize


def _single_range(request: Request) -> str:
    """只接受浏览器常用的单段 bytes Range，拒绝多段请求放大。"""
    value = (request.headers.get("range") or "").strip()
    if not value:
        return ""
    if len(value) > 80:
        raise ApiError(416, "仅支持单段 Range 请求", {"Accept-Ranges": "bytes"})
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if not match or not any(match.groups()):
        raise ApiError(416, "仅支持单段 Range 请求", {"Accept-Ranges": "bytes"})
    start, end = match.groups()
    if ((start and end and int(start) > int(end))
            or (not start and int(end) <= 0)):
        raise ApiError(416, "Range 范围无效", {"Accept-Ranges": "bytes"})
    return value


def parse_share(text):
    m = re.search(r"https://v\.douyin\.com/[\w-]+/?", text or "")
    if not m:
        raise ApiError(400, "未找到 v.douyin.com 分享链接")
    loc = _open(m.group(0), follow=False).headers.get("Location", "")
    km = re.search(r"/share/(video|note|slides)/(\d+)", loc)
    if not km:
        raise ApiError(404, "链接已失效或类型不支持")
    kind = "note" if km.group(1) == "slides" else km.group(1)
    item_id = km.group(2)
    html = _open(f"https://www.iesdouyin.com/share/{kind}/{item_id}/").read().decode("utf-8", "ignore")
    dm = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", html, re.S)
    if not dm:
        raise ApiError(502, "分享页无数据（可能被风控，配置 PROXY 重试）")
    item = next((i for i in _find(json.loads(dm.group(1)), "item_list") if i), None)
    if not item:
        raise ApiError(404, "视频不存在、已删除或私密")
    item = item[0]
    au = item.get("author") or {}
    avatar = (next(_find(au, "url_list"), None) or [""])[0]
    base = _safe(item.get("desc", ""), item_id)
    st = item.get("statistics") or {}
    res = {"kind": kind, "item_id": item_id, "title": item.get("desc") or "（无标题）",
           "author": au.get("nickname") or "", "avatar": avatar,
           "stats": {"digg": st.get("digg_count"), "comment": st.get("comment_count"),
                     "collect": st.get("collect_count"), "share": st.get("share_count")}}
    if kind == "note":
        res["images"] = [{"url": im["url_list"][0], "filename": f"{base}_{i:02d}.jpeg"}
                         for i, im in enumerate(item.get("images") or [], 1) if im.get("url_list")]
        return res
    v = item.get("video") or {}
    play = (next(_find(v.get("play_addr") or {}, "url_list"), None) or [""])[0]
    vid = re.search(r"video_id=([A-Za-z0-9_-]+)", play)
    vid_value = vid.group(1) if vid else ""
    filename = f"{base}.mp4"
    cover = (next(_find(v.get("cover") or {}, "url_list"), None) or [""])[0]
    res.update({"duration_ms": v.get("duration") or 0, "cover": cover,
                "video": {"url": f"https://aweme.snssdk.com/aweme/v1/play/?video_id={vid_value}&ratio=720p&line=0" if vid_value else "",
                          "proxy_url": _video_url(vid_value) if vid_value else "",
                          "download_url": _video_url(
                              vid_value, filename, download=True) if vid_value else "",
                          "filename": filename, "width": v.get("width"), "height": v.get("height")}})
    return res


class Body(BaseModel):
    text: str


@app.post("/api/parse")
def api_parse(body: Body):
    return parse_share(body.text)


def _stream(r, finalize, chunk=256 * 1024):
    try:
        while True:
            b = r.read(chunk)
            if not b:
                break
            yield b
    finally:
        finalize()


@app.get("/api/video/{vid}")
def api_video(vid: str, request: Request, exp: str = "", sig: str = "",
              dl: str = "", name: str = "video.mp4"):
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,120}", vid):
        raise ApiError(400, "非法的视频 ID")
    _require_video_token(vid, exp, sig)
    range_header = _single_range(request)
    lease = _media_acquire(request)
    extra = dict(CDN_HEADERS)
    if range_header:
        extra["Range"] = range_header
    r = None
    try:
        r = _open(f"https://aweme.snssdk.com/aweme/v1/play/?video_id={vid}&ratio=720p&line=0", headers=extra)
        h = {"Accept-Ranges": "bytes", "Cache-Control": "private, no-store",
             "X-Content-Type-Options": "nosniff"}
        for k in ("Content-Length", "Content-Range"):
            if r.headers.get(k):
                h[k] = r.headers[k]
        if dl:
            safe_name = re.sub(r'[\\/:*?"<>|\r\n]+', "_",
                               (name or "video.mp4"))[:80]
            h["Content-Disposition"] = (
                "attachment; filename*=UTF-8''" + urlparse.quote(safe_name))
        status = r.status if hasattr(r, "status") else r.getcode()
    except Exception:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass
        _media_release(lease)
        raise ApiError(502, "拉取视频失败")
    finalize = _media_finalizer(r, lease)
    return _MediaStreamingResponse(
        _stream(r, finalize), finalize=finalize, status_code=status,
        media_type="video/mp4", headers=h)


# ---------------------------------------------------------------- 多语言 + SEO

SUPPORTED_LANGS = {"zh": "zh-CN", "en": "en"}


def _origin(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    return f"{proto}://{host}"


def _pick_lang(request: Request) -> str:
    q = (request.query_params.get("lang") or "").lower()
    if q in SUPPORTED_LANGS:
        return q
    c = (request.cookies.get("lang") or "").lower()
    if c in SUPPORTED_LANGS:
        return c
    al = (request.headers.get("accept-language") or "").lower()
    return "zh" if al.startswith("zh") or not al else "en"


def _seo_head(lang: str, origin: str) -> str:
    zh = lang == "zh"
    canon = f"{origin}/" if zh else f"{origin}/?lang=en"
    m = {
        "zh": {"t": "抖音无水印下载器 · 开源版",
               "d": "免费开源的抖音无水印下载工具：粘贴分享链接即可在线预览并下载抖音视频与图集的无水印原片。免登录、无广告、直连优先且支持流式兜底、可自建。",
               "k": "抖音下载,抖音无水印下载,抖音视频下载,douyin downloader,抖音图集下载,开源,自建",
               "s": "抖音无水印下载器", "l": "zh_CN"},
        "en": {"t": "Douyin Downloader — No Watermark, Free & Open Source",
               "d": "Free, open-source Douyin (Chinese TikTok) no-watermark downloader. Paste a share link to preview and download original videos & photo galleries. No login, no ads, browser-direct with streaming fallback, self-hostable.",
               "k": "douyin downloader,douyin video download,no watermark,tiktok downloader,open source,self-hosted",
               "s": "Douyin Downloader", "l": "en_US"},
    }[lang]
    faq = {"zh": [("这个抖音下载器如何处理数据？", "基础版无需登录，不建立用户或分析数据库，也不保存媒体文件。解析和兼容转发时，服务器或反向代理会处理完成网络请求所需的 IP、浏览器信息和请求地址；IP 仅在进程内存中用于媒体限频。"),
                  ("下载的视频有水印吗？", "没有水印，是无水印原片，也不加二次水印。"),
                  ("支持图集吗？", "支持，图集会自动识别，可逐张下载原图。")],
           "en": [("How does this downloader handle data?", "This edition requires no login, creates no user or analytics database, and stores no media files. During parsing or streaming fallback, the server or reverse proxy processes the IP address, browser information, and request URL required for network transport; IPs are used only in process memory for media rate limits."),
                  ("Do downloads have a watermark?", "No — you get the original with no watermark, and we never add our own."),
                  ("Are photo galleries supported?", "Yes — image posts are auto-detected and each original image can be downloaded.")]}[lang]
    graph = [{"@type": "WebApplication", "name": m["s"], "url": f"{origin}/",
              "applicationCategory": "MultimediaApplication", "operatingSystem": "All",
              "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"}, "description": m["d"]},
             {"@type": "FAQPage", "mainEntity": [
                 {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
                 for q, a in faq]}]
    jsonld = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False)
    e = lambda s: s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
    return f'''<title>{e(m["t"])}</title>
<meta name="description" content="{e(m["d"])}">
<meta name="keywords" content="{e(m["k"])}">
<meta name="robots" content="index,follow,max-image-preview:large">
<meta name="theme-color" content="#0E1013">
<link rel="canonical" href="{canon}">
<link rel="alternate" hreflang="zh-CN" href="{origin}/">
<link rel="alternate" hreflang="en" href="{origin}/?lang=en">
<link rel="alternate" hreflang="x-default" href="{origin}/">
<meta property="og:type" content="website">
<meta property="og:title" content="{e(m["t"])}">
<meta property="og:description" content="{e(m["d"])}">
<meta property="og:url" content="{canon}">
<meta property="og:image" content="{origin}/og.svg">
<meta property="og:locale" content="{m["l"]}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{e(m["t"])}">
<meta name="twitter:image" content="{origin}/og.svg">
<script type="application/ld+json">{jsonld}</script>
<script>window.__LANG={lang!r};</script>'''


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    lang = _pick_lang(request)
    origin = _origin(request)
    html = Path("static/index.html").read_text("utf-8")
    html = (html.replace("{{HTMLLANG}}", SUPPORTED_LANGS[lang])
                .replace("{{SEO_HEAD}}", _seo_head(lang, origin)))
    resp = HTMLResponse(html)
    resp.set_cookie("lang", lang, max_age=31536000, samesite="lax")
    return resp


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots(request: Request):
    return f"User-agent: *\nAllow: /\n\nSitemap: {_origin(request)}/sitemap.xml\n"


@app.get("/sitemap.xml")
def sitemap(request: Request):
    o = _origin(request)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           f'  <url><loc>{o}/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>\n'
           '</urlset>\n')
    return Response(xml, media_type="application/xml")


@app.get("/og.svg")
def og_image():
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
<rect width="1200" height="630" fill="#0E1013"/>
<circle cx="200" cy="150" r="360" fill="#FE2C55" opacity="0.28"/><circle cx="1050" cy="130" r="320" fill="#25F4EE" opacity="0.22"/>
<text x="90" y="330" font-family="sans-serif" font-size="82" font-weight="800" fill="#fff">Douyin Downloader</text>
<text x="92" y="410" font-family="sans-serif" font-size="38" font-weight="700" fill="#25F4EE">No Watermark · Open Source · No Ads</text>
<text x="94" y="470" font-family="sans-serif" font-size="30" fill="#8A93A0">Paste a link · browser-direct · self-hostable</text></svg>'''
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/healthz")
def healthz():
    return {"ok": True}
