#!/usr/bin/env python3
"""抖音无水印下载器 · 开源基础版 (Douyin Downloader · Open-Source Edition)

免登录、免签名，粘贴分享链接即可在线预览并下载无水印视频 / 图集。
视频与图片由用户浏览器直连抖音 CDN，服务器不落地、不暴露内容。

启动:  uvicorn server:app --host 0.0.0.0 --port 8000
可选:  环境变量 PROXY=socks5://user:pass@host:port 让服务器解析走代理（防封 IP）

> 这是最小可用的开源版。完整版（管理后台、代理池、用户体系、异步计费 API、
> 数据分析）不在本仓库开源，如需请联系作者，见 README。
"""
import json
import os
import re
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
ALLOWED = ("douyinpic.com", "douyinvod.com", "iesdouyin.com", "snssdk.com",
           "byteimg.com", "zjcdn.com", "douyin.com", "pstatp.com", "amemv.com")

app = FastAPI(title="抖音无水印下载器 · 开源版")


class ApiError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


@app.exception_handler(ApiError)
async def _err(_, exc):
    return JSONResponse(status_code=exc.status, content={"error": exc.message})


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
    vid = re.search(r"video_id=([\w-]+)", play)
    cover = (next(_find(v.get("cover") or {}, "url_list"), None) or [""])[0]
    res.update({"duration_ms": v.get("duration") or 0, "cover": cover,
                "video": {"url": f"https://aweme.snssdk.com/aweme/v1/play/?video_id={vid.group(1)}&ratio=720p&line=0" if vid else "",
                          "proxy_url": f"/api/video/{vid.group(1)}" if vid else "",
                          "filename": f"{base}.mp4", "width": v.get("width"), "height": v.get("height")}})
    return res


class Body(BaseModel):
    text: str


@app.post("/api/parse")
def api_parse(body: Body):
    return parse_share(body.text)


def _stream(r, chunk=256 * 1024):
    try:
        while True:
            b = r.read(chunk)
            if not b:
                break
            yield b
    finally:
        r.close()


@app.get("/api/video/{vid}")
def api_video(vid, request: Request, dl: str = "", name: str = "video.mp4"):
    if not re.fullmatch(r"[\w-]{8,120}", vid):
        raise ApiError(400, "非法的视频 ID")
    extra = dict(CDN_HEADERS)
    if request.headers.get("range"):
        extra["Range"] = request.headers["range"]
    try:
        r = _open(f"https://aweme.snssdk.com/aweme/v1/play/?video_id={vid}&ratio=720p&line=0", headers=extra)
    except Exception:
        raise ApiError(502, "拉取视频失败")
    h = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    for k in ("Content-Length", "Content-Range"):
        if r.headers.get(k):
            h[k] = r.headers[k]
    if dl:
        h["Content-Disposition"] = f"attachment; filename*=UTF-8''{urlparse.quote(name)}"
    status = r.status if hasattr(r, "status") else 200
    return StreamingResponse(_stream(r), status_code=status, media_type="video/mp4", headers=h)


@app.get("/api/media")
def api_media(url: str):
    host = urlparse.urlsplit(url).hostname or ""
    if not any(host == s or host.endswith("." + s) for s in ALLOWED):
        raise ApiError(403, "域名不在允许范围")
    try:
        r = _open(url, headers=CDN_HEADERS)
    except Exception:
        raise ApiError(502, "拉取失败")
    return StreamingResponse(_stream(r), media_type=r.headers.get("Content-Type", "image/jpeg"))


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
               "d": "免费开源的抖音无水印下载工具：粘贴分享链接即可在线预览并下载抖音视频与图集的无水印原片。免登录、无广告、浏览器直连、可自建。",
               "k": "抖音下载,抖音无水印下载,抖音视频下载,douyin downloader,抖音图集下载,开源,自建",
               "s": "抖音无水印下载器", "l": "zh_CN"},
        "en": {"t": "Douyin Downloader — No Watermark, Free & Open Source",
               "d": "Free, open-source Douyin (Chinese TikTok) no-watermark downloader. Paste a share link to preview and download original videos & photo galleries. No login, no ads, browser-direct, self-hostable.",
               "k": "douyin downloader,douyin video download,no watermark,tiktok downloader,open source,self-hosted",
               "s": "Douyin Downloader", "l": "en_US"},
    }[lang]
    faq = {"zh": [("这个抖音下载器安全吗？", "安全。前端完全开源可审查，不采集隐私、无需登录，视频由你的浏览器直连获取，服务器不存储。"),
                  ("下载的视频有水印吗？", "没有水印，是无水印原片，也不加二次水印。"),
                  ("支持图集吗？", "支持，图集会自动识别，可逐张下载原图。")],
           "en": [("Is this downloader safe?", "Yes. The front-end is fully open-source and auditable — no data collection, no login; your browser fetches the video and the server stores nothing."),
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
