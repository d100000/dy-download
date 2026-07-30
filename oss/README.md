<p align="center">
  <img src="screenshots/banner.svg" alt="抖音无水印下载器 Douyin TikTok Video Downloader" width="100%">
</p>

<h1 align="center">抖音无水印下载器 · Douyin / TikTok Video Downloader</h1>

<p align="center">
  <b>开源、免登录、无水印</b>的抖音视频 / 图集下载工具。粘贴分享链接即可在线预览并下载无水印原片；视频播放优先直连，视频下载使用同源签名流式线路，全程不落地、不存储媒体文件。<br>
  <b>Open-source, self-hosted, no-watermark</b> downloader for Douyin (Chinese TikTok). Paste a share link to preview and download original videos or image galleries — direct playback with signed same-origin video downloads, and no media files are stored on the server.
</p>

<p align="center">
  <code>抖音下载</code> · <code>抖音无水印</code> · <code>douyin downloader</code> · <code>tiktok downloader</code> · <code>no watermark</code> · <code>open source</code> · <code>self-hosted</code> · <code>视频下载</code> · <code>去水印</code>
</p>

---

## ✨ 功能 · Features

- 🎬 **无水印视频下载** — 720P / MP4，在线预览可拖动进度 · No-watermark video download
- 🖼 **图集下载** — 图文作品逐张原图下载 · Image-gallery (slideshow) download
- 🔒 **免登录 · 不建用户画像** — 不创建用户或分析数据库，网络请求所需信息仅用于当次处理与内存限频 · No login, user profiles, or analytics database
- ⚡ **可靠下载** — 1 字节 Range 预检后由浏览器从同源地址原生流式保存，服务器不落地 · Range preflight and native same-origin streaming, zero server storage
- 🛡 **媒体防滥用** — 流式地址带 HMAC 有效期签名，并按 IP 限制请求频率、并发数及多段 Range · Signed, expiring and rate-limited media URLs
- 🚫 **无广告** — 干净、无弹窗、无捆绑 · Ad-free
- 🌓 明暗双主题 · 移动端适配 · Dark/light theme, mobile-ready
- 🛡 可选代理 — 环境变量 `PROXY=socks5://...` 让解析走代理防封 IP · Optional proxy to avoid IP bans

## 🚀 快速开始 · Quick Start

```bash
# 1) 本地运行 (Local)
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000 --no-access-log
# 打开 http://localhost:8000

# 2) Docker
docker build -t douyin-dl .
docker run -p 8000:8000 \
  -e APP_SECRET=请替换为至少32字节随机密钥 \
  douyin-dl

# 3) 走代理（可选，防封 IP / Optional proxy）
PROXY=socks5://user:pass@host:port uvicorn server:app --port 8000 --no-access-log
```

仓库的 `./run.sh` 默认只监听 `127.0.0.1`；需要局域网访问时显式使用
`HOST=0.0.0.0 ./run.sh`，并在前面配置可信反向代理与限流。

未设置 `APP_SECRET` 时，程序会在 `DATA_DIR`（默认 `data/`）原子生成
`data/.app-secret`。本地部署会在后续重启中复用；容器部署应显式设置
`APP_SECRET`，或把 `/app/data` 挂载为持久卷。多个实例必须使用同一个密钥，
否则一个实例签发的媒体地址无法被另一个实例验证。

### 安全与隐私边界

- `/api/parse` 返回的同源视频播放、下载地址带有短时 `exp`/`sig` HMAC
  签名；缺失、过期或篡改都会被拒绝。文件名和下载模式不参与签名，因此前端
  可以在同一份媒体授权上安全追加 `dl=1&name=...`。
- `/api/video` 只接受浏览器常用的单段 `bytes` Range，并按客户端 IP
  限制请求频率和同时播放/下载数。限频状态只存在进程内存，不写数据库。
- 未被产品使用的通用 `/api/media?url=...` 代理已移除，避免服务器成为任意
  抖音 CDN 转发器。
- 本基础版不创建用户、行为分析或请求日志数据库，也不保存视频和图片文件。
  但解析时分享链接会发送给本服务；浏览器直连播放或图片时，抖音 CDN 会看到浏览器
  的网络请求信息；视频下载与同源兼容播放时，本服务或前置反向代理会按网络通信需要接触
  IP、User-Agent 和请求地址。仓库内启动脚本、Docker 和示例命令默认关闭 Uvicorn
  access log；若自行增加 Nginx、云平台或容器访问日志，应避免记录原始 IP、完整
  User-Agent/Referer 和含媒体签名的 query，并设置短期清理策略。
- 只有服务确实位于可信反向代理之后时才设置 `TRUST_PROXY=1`。多层可信代理可用
  `TRUST_PROXY_HOPS` 指定层数；公网直接运行时不要开启，否则客户端可伪造转发头
  绕过 IP 限制。

相关环境变量：

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `APP_SECRET` | 自动持久化生成 | HMAC 媒体签名密钥；生产和多实例部署应显式配置 |
| `DATA_DIR` | `data` | 自动密钥文件目录 |
| `MEDIA_TOKEN_TTL` | `43200` | 媒体授权有效秒数，限制在 300–86400 秒 |
| `MEDIA_REQUESTS_PER_MIN` | `120` | 单 IP 每分钟媒体请求上限 |
| `MEDIA_MAX_CONCURRENT` | `6` | 单 IP 同时播放或下载上限 |
| `TRUST_PROXY` | 关闭 | 是否采信可信反代写入的 `X-Forwarded-For` |
| `TRUST_PROXY_HOPS` | `1` | 从 XFF 右侧计算的可信代理层数 |
| `PROXY` | 空 | 服务端访问抖音时使用的 HTTP/SOCKS 代理 |

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
