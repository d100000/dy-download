<p align="center">
  <img src="screenshots/banner.svg" alt="抖音无水印下载器 Douyin TikTok Video Downloader" width="100%">
</p>

<h1 align="center">抖音无水印下载器 · Douyin / TikTok Video Downloader</h1>

<p align="center">
  <b>开源、免登录、无水印</b>的抖音视频 / 图集下载工具。粘贴分享链接即可在线预览并下载无水印原片，视频由你的浏览器<b>直连抖音 CDN</b>，服务器不落地、不存储任何内容。<br>
  <b>Open-source, self-hosted, no-watermark</b> downloader for Douyin (Chinese TikTok). Paste a share link, preview and download the original video / image-gallery with no watermark — media streams <b>directly from your browser</b>, nothing is stored on the server.
</p>

<p align="center">
  <code>抖音下载</code> · <code>抖音无水印</code> · <code>douyin downloader</code> · <code>tiktok downloader</code> · <code>no watermark</code> · <code>open source</code> · <code>self-hosted</code> · <code>视频下载</code> · <code>去水印</code>
</p>

---

## ✨ 功能 · Features

- 🎬 **无水印视频下载** — 720P / MP4，在线预览可拖动进度 · No-watermark video download
- 🖼 **图集下载** — 图文作品逐张原图下载 · Image-gallery (slideshow) download
- 🔒 **零隐私 · 免登录** — 前端完全开源、不采集隐私、无需登录 · No login, no tracking, fully open-source
- ⚡ **浏览器直连** — 播放/下载由用户浏览器直连抖音，服务器不落地 · Browser-direct, zero server storage
- 🚫 **无广告** — 干净、无弹窗、无捆绑 · Ad-free
- 🌓 明暗双主题 · 移动端适配 · Dark/light theme, mobile-ready
- 🛡 可选代理 — 环境变量 `PROXY=socks5://...` 让解析走代理防封 IP · Optional proxy to avoid IP bans

## 🚀 快速开始 · Quick Start

```bash
# 1) 本地运行 (Local)
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000
# 打开 http://localhost:8000

# 2) Docker
docker build -t douyin-dl . && docker run -p 8000:8000 douyin-dl

# 3) 走代理（可选，防封 IP / Optional proxy）
PROXY=socks5://user:pass@host:port uvicorn server:app --port 8000
```

## 🔌 API

```bash
curl -X POST http://localhost:8000/api/parse \
  -H 'Content-Type: application/json' \
  -d '{"text":"https://v.douyin.com/xxxx/"}'
# 返回标题、作者、点赞/评论/收藏、无水印视频地址、图集图片地址…
```

---

## ⭐ 完整版（Pro）· Full Edition

<p align="center"><img src="screenshots/pro-preview.svg" alt="完整版管理后台预览 Admin dashboard preview" width="100%"></p>

本仓库是**最小可用的开源基础版**，只包含"粘贴链接 → 无水印下载"。以下**完整版能力不在此开源**：

| 完整版功能 | 说明 |
|---|---|
| 🧑‍💼 **用户体系** | 注册/登录、滑块验证码、防爆破/蜜罐/防脚本、每日额度 |
| 🛡 **代理 IP 池** | 多协议、轮换、失败转移、被封自动检测与禁用、管理后台 |
| 🔑 **异步计费 API** | API Key、余额、按条计费、任务提交/查询、开发者控制台 |
| 📊 **数据分析后台** | 新增用户 / PV·UV / 使用 / API 调用 / 营收 / 留存趋势 |
| 📦 **批量 + Excel** | 批量解析、表格视图、一键导出 xlsx |

> **需要完整版（管理后台 / 代理池 / 付费 API / 数据分析）？请联系作者。**
> **Need the full edition (admin dashboard / proxy pool / paid API / analytics)? Please contact the author.**
>
> 📮 联系方式：在本仓库提 Issue，或见作者主页。

---

## ⚠️ 免责声明 · Disclaimer

仅供个人学习、收藏及**获得授权**的素材备份使用；下载内容版权归原作者所有，未经授权请勿二次发布或商用。本工具不破解任何加密、不绕过登录鉴权，仅访问抖音对外公开的 H5 分享页。For personal, authorized backup use only. All content copyright belongs to its original creators.

## 📄 License

MIT
