# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

抖音无水印下载器：FastAPI 单体服务 + 代理池 + 管理后台 + 用户体系 + 按次计费的开放 API。代码与注释均为中文，保持一致。

## 常用命令

```bash
./run.sh                                   # 本地启动（首次自动建 .venv 并装依赖），默认 :3344
PORT=8010 ADMIN_PASSWORD=xxx ./run.sh      # 换端口 / 改管理密码
.venv/bin/uvicorn server:app --reload --port 3344 --no-access-log   # 开发热重载（手动方式）
python3 douyin_dl.py "分享文案或短链" [输出目录]      # 纯标准库 CLI 版，不依赖服务
python3 tools/testproxy.py 8899            # 本地测试代理：验证"出站请求确实走代理"，逐条打印 CONNECT
.venv/bin/python -m unittest tests.test_security_reliability   # 后端测试套件（TestClient，约 2 秒跑完）
.venv/bin/python -m unittest tests.test_security_reliability.DurableBillingTests   # 单跑一个类（加 .方法名 可单跑一个用例）
node --test tests/*.js                     # 前端逻辑测试；必须写 *.js 且在仓库根目录跑（文件名不匹配默认 glob，裸目录会报错）
swift tools/render_og.swift static/og.svg static/og.png   # 重渲染 og.png 位图（1200×630，macOS/AppKit）
docker build -t douyin-dl . && docker run -p 3344:8000 -e ADMIN_PASSWORD=xxx -v $(pwd)/data:/data douyin-dl
```

页面：`/` 下载器 · `/transcript` 文案提取落地页 · `/api-docs` API 文档 · `/api-console` 用户 API 控制台 · `/admin_d` 管理后台（隐藏入口，首页不暴露） · `/s/{sid}` 分享页。
站点级路由（改站点文案/新增可收录页面时要一并同步）：`/robots.txt`、`/llms.txt`、`/sitemap.xml`、`/og.svg`（内联 SVG 卡片图）、`/og.png`（`static/og.png` 位图，微信/多数抓取器不渲染 SVG，卡片兜底必须用它）。

依赖刻意保持最小：`fastapi` / `uvicorn` / `PySocks`（socks 代理）/ `openpyxl`（批量导出 xlsx）/ `segno`（分享页二维码），**无 requests**。Dockerfile 只 `COPY server.py` + `COPY static`——新增任何运行时需要的文件（静态资源、模板）必须确保它在 `static/` 内且已提交，否则容器里 404/500。

测试在 `tests/`（命令见上），改安全/计费/配额/媒体流/前端下载播放相关代码后必须跑：`test_security_reliability.py` 是后端套件（覆盖媒体签名与 Range/流租约、代理健康、原子配额、持久化计费、隐私存储、ATC 统一解析与主动文案模式、播放日志及文案回归；导入 `server` 前已设临时 `DATA_DIR`/`MIHOMO_OFF=1`，不碰真实 `data/`）；两个 Node 测试（`test_download_flow.js`、`test_share_playback.js`）会把 `static/index.html`、`static/share.html`、`oss/static/index.html` 里的 JS 按**锚点字符串**（`function downloadTarget`、`let _playSession = 0;` 等）切出来在 vm 里跑——改前端下载/播放代码时必须保留锚点或同步更新测试。测试不能替代真实链接实测：第三方接口随时可变，改解析逻辑必须手动跑服务验证。无 lint 配置。`/healthz` 可做存活探针（含 `version` 字段）。

## 版本号与 README 维护（每次改动必做）

版本号唯一来源是 `server.py` 顶部的 `APP_VERSION`（语义化：修 bug +patch，新功能 +minor，不兼容改动 +major）。**每次功能性改动必须**：① bump `APP_VERSION`；② 同步 README.md 顶部版本号；③ 在 README「更新日志」表新增一行（版本、日期、内容）；④ 若功能有增删，同步 README 功能列表与部署说明。仅改文档/注释不 bump。

## 架构

### 单文件后端 + 单文件前端

`server.py`（约 7000 行）是全部后端，按注释分隔线分层，大体顺序即依赖顺序：常量 → SQLite 层 → 防薅羊毛/限频 → 用户鉴权/防机器人 → 代理池 → 内置 mihomo 内核 → 应用设置+计费 → HTTP 出站层 → 工具函数 → 核心解析 → 分享页 → **AnyToCopy 统一解析（ATC）** → 公共 API → 开放 API v1 → 管理后台 → 健康检查 → 页面+SEO → **用户鉴权 API**。定位代码用 `grep -n "^# -----" server.py` 列分隔线，不要记行号。新增功能应放进对应分区，不要拆包。

注意用户体系被拆成了相距很远的两块：**底层**（`hash_pw`/`current_user`/滑块验证码，在「用户鉴权 / 防机器人」分区）在文件前部，**路由**（`/api/auth/*`）在文件**最末尾**的同名分区。改用户相关功能时两处都要看。

`static/*.html` 每个页面是自包含单文件（内联 CSS + 原生 JS，无构建、无框架、无依赖）。没有 `StaticFiles` 挂载，每个页面都有独立路由，分两类：
- **模板替换型**：`index.html` / `api-docs.html`（替换 `{{HTMLLANG}}` / `{{SEO_HEAD}}` / `{{ORIGIN}}`）、`share.html`（见分享页一节）。
- **纯 `FileResponse`**：`admin.html`（`/admin_d`）、`api-console.html`（`/api-console`），无占位符、不做 SEO。

新增可被搜索引擎收录的页面时，必须走模板 + `_seo_head()`，否则占位符会原样输出到页面。

### 解析链路（`_parse_share`）

从整段分享文案中取受支持平台的 HTTPS 公开作品链接（`ATC_PLATFORM_HOSTS`，安全后缀匹配）→ `_atc_extract()` 调 `POST /video/extract` → 每 4 秒调 `GET /video/query` → `_atc_result_to_parse()` 归一化为站内数据契约。**普通解析只传 `workUrl`，绝不传 `taskType`；只有用户主动提取文案时才传 `taskType=TEXT`。** 结果进 `_cache`（30 分钟）和 `atc_cache`（保存刷新/文案任务所需的 `work_url` 与媒体元数据）。抖音分享页创建仍只接受规范的 `v.douyin.com` 短链，不要与通用解析入口混用。

`server.py` 主服务已统一到 ATC；`oss/server.py` 与 `douyin_dl.py` 是独立的精简/CLI 实现，不共享主服务运行时配置。本项目所说“后台全部使用 ATC”指 `server.py` 中的网页、开放 API 与分享 worker 三条入口。

### 流量分工：ATC 直连播放，视频下载统一同源流式

新解析的 `result.video.url`/`atc_url` 是 ATC 返回的无水印直链；`video.download_url` 是带 `exp`/`sig` 的 `/api/atc/video/{item_id}`。该路由只能按 item_id 读取服务端 `atc_cache`，不接受 URL 参数，且二次校验抖音媒体域名白名单，不是通用代理。前端先发 1 字节 Range 预检，再交给隐藏 iframe 原生流式保存，不把完整视频读进 Blob。`_media_signature()` 绑定 `kind/resource/exp`，但不绑定 `dl/name`；入口继续经过单 IP 限频、流生命周期并发租约和单段 Range 校验。旧分享页的 `/api/video/{vid}` 仍保留兼容；不得重新引入任意 URL 代理。图集仍逐张浏览器直连。

**抖音 CDN 已上线 Referer 防盗链**（v1.10.1 实测：douyinvod 对带第三方站点 Referer 的媒体请求一律 403，无 Referer 或 douyin.com Referer 正常）。因此所有嵌入抖音媒体的页面（`static/index.html`、`static/share.html`、`oss/static/index.html`）的 `<head>` 必须保留 `<meta name="referrer" content="no-referrer">`——`<video>` 元素不支持 `referrerpolicy` 属性，只有文档级策略能覆盖媒体请求；新增引用抖音直链的标签/页面时不要绕过它。服务器侧出站不受影响：`CDN_HEADERS` 固定带 `Referer: https://www.douyin.com/`。

### 出站请求：抖音媒体经 `open_url()`，AnyToCopy 固定直连

抖音媒体与旧兼容请求必须走 `open_url()`（基于 `urllib` + `PySocks`，无 requests）。它按 `ProxyManager.candidates()` 的轮换策略依次尝试代理，失败自动转移；解析页的 403/401 或验证页判定为 IP 被封 → `mark_banned()` 落库 + 禁用 + 换下一个。媒体请求是例外：单个资源/地区限制也会返回 401/403/429/5xx，因此只做中性换代理与换域名，既不 `mark_ok`，也不能 `mark_fail`/`mark_banned` 污染代理健康状态。`force_proxy=True`（默认）时没有可用代理就直接报 503，**绝不直连抖音**。AnyToCopy API 是明确例外：`_atc_request()` 用标准库 `urlopen` 固定直连第三方 API，便于出口鉴权且不污染抖音代理健康。任何其他新代码不得直接 `urlopen`/`requests` 请求抖音。

### 代理池

`ProxyManager`（`threading.Lock` 保护）持久化在 `data/config.json`（代理列表 + 策略），不在 SQLite 里。支持 `scheme://user:pass@host:port`、`host:port:user:pass`、`user:pass@host:port`、`host:port` 四种输入格式（`parse_proxy` 归一化，无前缀按 `default_protocol`，默认 socks5）。策略：`round_robin`/`random`/`least_fail`、每请求重试数、连续失败自动禁用、后台 `_health_loop` 定时并发测速（出口 IP + 抖音可达性）并对恢复的代理自愈解禁。

**内置 mihomo 内核（机场订阅）**：代理池只认 http/socks，机场的 vmess/trojan 等加密协议进不来，因此由 `MihomoManager`（`server.py` 内「内置 mihomo 内核」分区）把订阅落地成一个本地 socks5 端口再接进池子。它托管一个完整的子进程生命周期：`ensure_binary()` 按平台下载内核（`data/mihomo/`，`MIHOMO_DL_BASE`/`MIHOMO_VERSION` 可换源换版本，`MIHOMO_OFF=1` 全禁）→ `write_config()` 渲染 YAML → `_start_locked()`/`stop()`/`reload()` → 后台 `supervise()` 守护线程（每 5s，仿 `_health_loop`）。`ProxyManager.sync_managed()` 维护那条 `id="mihomo"` 的托管代理条目。后台 `/api/admin/mihomo` 读写，订阅存 `app_settings` 表的 `mihomo_sub_url`。改动时注意三条硬约束：① **隔离**——只绑 `127.0.0.1` + `allow-lan:false` + 随机高端口 + `authentication` 账密（连本机也要验证），保证同机其他项目连不进、不改系统代理、不开 TUN；② **fail-closed**——`fallback` 组必须挂一个永远失败的 `blackhole` 成员，否则空 provider 会让内核注入 COMPATIBLE(=DIRECT) 导致服务器真实 IP 泄漏；③ **exec 路径**——用 `MIHOMO_BIN.resolve()` 绝对路径且不传 `cwd`（`DATA_DIR` 是相对路径，传 cwd 会让相对二进制路径解析错）。子进程随服务启停（startup 起 `supervise`，shutdown 调 `stop`），pidfile 清理孤儿，务必单 worker。

### 分享页（`/s/{sid}`）

把抖音链接变成"微信里点开就能看"的页面。规划见 [docs/分享页功能规划.md](docs/分享页功能规划.md)。

同样**不落地任何媒体字节**：`shares` 表只存元数据快照与媒体地址，`atc_cache.work_url` 用于临时签名链过期后惰性重解析。`_parse_share()` 处理新短链，`_parse_item(kind,item_id)` 处理存量分享页刷新，两者都必须最终进入 `_atc_parse_work_url()`。升级前已存的 `vid` 与抖音直连线路继续兼容。

`static/share.html` 是独立模板（占位符 `{{SHARE_HEAD}}` / `{{SHARE_DATA}}` / `{{WECHAT}}`），刻意比首页轻——微信里首屏要 1 秒内出内容，因此不要往里加极光动画之类的装饰。注入 `{{SHARE_DATA}}` 的 JSON 已把 `<` 转义，改这段时别把转义丢了。

分享页 SEO 走 `_share_head()`（per-share OG）而非 `_seo_head()`，且**一律 noindex**，`robots.txt` 也 Disallow `/s/`——不收录他人作品是合规底线，不要"优化"掉。

播放线路由 `app_settings.share_play_priority`（默认 `["atc","proxy","dy1","dy2"]`，后台「视频解析 API」页签拖拽排序）驱动。新结果的 `source=atc`，前端只构建 AnyToCopy 直连与同源流两条有效线路；`dy1`/`dy2` 仅供升级前已有 `vid` 的分享数据兼容。ATC 地址带签名会过期：`atc_url_ttl` 内从缓存注入，过期后移除死链并按 `atc_cache.work_url` 惰性刷新。微信 WebView 拦截直连时常不报 error 只挂起，因此微信内直连看门狗为 4 秒（普通浏览器 7 秒），随后落到同源流。每级尝试会记录 `play_try`、`fallback`、`play_ok`/`play_fail`（含 source/stage/detail/ms，v1.13 起失败额外记下一线路 `next_src`，但不记带签名地址）。后台「分享页」和「播放日志」页签用于诊断成功率、耗时与失败链路。数据库列仍通过 `PRAGMA table_info` + `ALTER TABLE` 就地补齐。

**转发流量统计**：`/api/video` 与 `/api/atc/video` 每条流结束时按实际转发字节计量（`_ResumableVideoStream.sent`，经 `_media_finalizer` 的 `on_close` 钩子），先进内存 `_traffic_pending`（finalize 可能跑在事件循环线程，不能直接写 SQLite），由 `_sweeper` 每 5 分钟、后台读取时与 shutdown 时 `_flush_media_traffic()` 落库到 `media_traffic` 表（天 × 用途聚合，`scope`=play/download，无个人标识，保留 1 年）。后台看板走 `GET /api/admin/traffic-stats?days=`；浏览器直连媒体的流量不经过服务器。

**域名池**：`_share_origin()` 给新链接分配域名，优先级为 **后台主域名（`app_settings.share_primary_domain`，即时生效不重启）→ `SHARE_DOMAINS` 环境变量域名池（轮换）→ 请求来源**。短码与域名解耦，某域名被微信封了在后台停用即可（`share_domains_off` 也对主域名生效，会自动退回池子/来源），存量链接换域名照样打开。主站域名与分享域名要物理隔离——分享域名被封是日常，主站被牵连是灾难。

**微信卡片**：两条完全不同的机制，别混。① **粘贴链接进聊天窗口 → 永远是纯文字链**，微信不做 URL 展开，任何 og 标签都改不了这一点；② **在微信内打开页面 → 点右上角 ··· 转发 → 才是卡片**。网页无法用按钮唤起微信分享面板，只能引导用户去点 ···。所以产品文案不要承诺「发到微信会自动变成卡片」——首页弹窗、分享页引导、FAQ（中英 + JSON-LD）、`llms.txt` 里的说法必须一致，改一处要四处同步。

卡片图走 `_card_cover()`：抖音封面原始直链是 `p26-sign.douyinpic.com/….webp?x-signature=…&x-expires=…`，**去掉主机的 `-sign` 并把扩展名换成 `.jpeg`**，抖音会返回同一张图的**无签名、不过期 JPEG**（签名覆盖路径，只换扩展名不去 `-sign` 会 403，两步必须一起做）。这解决 webp 在微信缩略图上支持不稳定、以及签名约 14 天过期后存量分享页全变无图卡片两个问题。无封面时兜底 `og.png` 而**不是 `og.svg`**——微信不渲染 SVG。

**微信 JS-SDK**：`/api/wx/jssdk` 出签名，未配置公众号时返回 `enabled:false`，前端静默降级到微信默认卡片。`jsapi_ticket` **存 app_settings 表**（全局唯一 + 有频次上限，存内存会导致多 worker 互相顶掉）。这里的出站请求**刻意不走 `open_url()`**：公众号要求服务器出口 IP 在白名单内，走代理反而失败。

**海报**在前端用 canvas 合成（封面走 CDN 的 `ACAO:*` 跨域绘制，二维码用同源 PNG——SVG 在部分内核会污染画布导致导出抛 SecurityError），服务器同样不参与、不落地。微信封图片远少于封链接，海报是链接被封时的传播兜底。导出**必须用 `toDataURL()` 出 `data:` URI，不要用 `toBlob`+`createObjectURL`**：微信 WebView 对 `blob:` 图片长按不弹「保存图片／发送给朋友」，海报就既存不下也发不出去。

### AnyToCopy 统一解析（ATC）

主解析服务：网页、开放 API 与分享 worker 全部调用 `/video/extract`。硬约束：① 普通解析只传 `workUrl`，默认不获取语音文案；② 只有主动文案任务才传 `taskType=TEXT` 并走 `atc_jobs`；③ 对方并发上限 5，主解析用 `_atc_primary_slots` 限制在途为 3，后台队列在途 ≤2；④ 结果按 `item_id` 缓存在 `atc_cache`，地址用 `atc_url_ttl` 判新鲜，文案缓存命中不扣次；⑤ ATC API 出站刻意不走代理池，只存 URL/元数据不落地媒体。文案提取 API（`POST/GET /api/atc/transcript`）仅登录用户，配额主体用 `atc:user:{id}`。后台「视频解析 API」页签管理密钥、主开关、默认关闭的文案开关、TTL 与播放顺序。

### 滑块验证码与 `pass_token` 门禁

自研的防机器人链路（无 Pillow、无第三方库）：`_png()` 是手写的极简 PNG 编码器（stdlib `zlib`+`struct`），`make_captcha()` 服务端生成带缺口的背景图。**缺口坐标只存在服务端 `_captchas` 与像素里**，绝不出现在响应体中——抓包拿不到答案，改这段时别把坐标漏进返回值。`verify_captcha()` 校验落点 + 行为轨迹 + PoW（`POW_BITS=14`，抬高批量自动化成本）+ 蜜罐字段。

关键边界：滑块签发的 `pass_token` **只保护 `/api/auth/register` 与 `/api/auth/login`**，由 `_do_auth_guard()` 一次性消费。`/api/parse`、`/api/parse/batch` 与 `/api/share` 不经过滑块；网页解析靠原子免费额度预占保护，开放 API `/api/v1/*` 则用 API Key 原子计费。新增注册/登录入口必须复用滑块令牌；新增解析入口必须复用额度预占与失败退款，不能把两套门禁混在一起。验证码接口本身有 `_captcha_rate_ok()` 限频，防的是生成图片的 CPU-DoS。

### 配额与计费

- **网页免费配额**：`usage_daily` 表按 subject 计数，登录用户按 `user:{id}`（`FREE_USER_DAILY`），匿名按用途化 HMAC 后的 `ip:` + 30 天随机第一方匿名 ID `fp:`（取最大值，`FREE_ANON_DAILY`）。解析前必须通过 `reserve_quota()` 在 `BEGIN IMMEDIATE` 事务里原子预占；成功用 `settle_quota()` 结算实际条数，失败或未处理部分必须 `release_quota()` / 结算退款，过期预占由后台清理。
- **开放 API 计费**（分为单位）：创建 `/api/v1/jobs` 时在同一个 `BEGIN IMMEDIATE` 事务里快照单价、整批扣减 `balance_cents`、增加 `reserved_cents`，并写入 `jobs`、逐条 `job_items` 与 `api_ledger reserve`；余额不够则整批拒绝。`Idempotency-Key` 可安全重放。非 daemon 的有界 worker 通过 `_claim_job_item()` 获取数据库租约，`_finish_job_item()` 用 CAS 在同一事务里把成功项从 reserved 转入 spent/calls，失败项精确退回 balance，同时写账本和唯一 `api_logs`。启动时 `_recover_legacy_api_jobs()`、`_reconcile_api_job_accounts()` 恢复旧任务并对账，重启不会丢任务或重复计费。`job_items` 是事实源，`jobs` 只是聚合/结果快照；不要退回“一请求一条 daemon 线程”的实现。

### 隐私与数据最小化

- 前端只允许使用 `dyanon`：16 字节随机第一方匿名 ID，固定 30 天过期；加载时主动删除旧 `dyfp`。**不得重新引入 Canvas、硬件参数、屏幕、字体等浏览器指纹。** 请求头继续用 `X-FP` 只是为了后端兼容。
- 服务端持久化网络标识前必须经 `APP_SECRET` 做按用途、按周期隔离的 HMAC 摘要；浏览器信息只保留诊断所需的粗粒度字段。访问、解析、播放事件与 API 任务/结果的保留期为 `DATA_RETENTION_DAYS`（强制 1–30 天，默认 30 天），到期数据由每 5 分钟执行的清理任务删除。
- 默认部署必须关闭 Uvicorn/Nginx access log；不得让原始 IP、完整 UA/Referer、媒体签名、下载文件名或 API Key 进入 URL/日志。开放 API 只从 `X-API-Key` 读取密钥，吊销和充值等管理操作把密钥放 JSON body。
- 站内账号是可选功能；注册会保存邮箱与加盐密码哈希，账号资料随账号保留。服务器不落地保存视频或图片文件，兼容线路只做流式转发；每次解析会把规范化作品链接提交给 AnyToCopy 获取元数据和媒体地址，普通解析不请求语音文案，主动文案任务才传 `taskType=TEXT`；浏览器直连媒体时，媒体源会收到请求方的网络与浏览器信息。
- 对外文案不得使用“零隐私采集”“不采集任何数据”“不记录账号”等绝对说法。中英文页面与 README 应明确以上数据范围、保留期和第三方直连边界。

### 状态存储与部署约束

SQLite 在 `data/app.db`（WAL），所有访问经 `db_exec()` + 全局 `_db_lock`；schema 在 `_SCHEMA` 里用 `CREATE TABLE IF NOT EXISTS` 就地演进（无迁移框架，改表要自行考虑既有库的兼容）。

**会话与验证码状态仍在进程内存字典**（`_user_sessions`、`_sessions`、`_captchas`、`_passes`、限频计数），由 `_sweeper` 线程每 5 分钟清理。后果：重启即掉线；**多 worker 部署会话不互认**——默认按单 worker 运行，会话仍需改造为共享存储。开放 API 作业及计费已持久化，不受该限制。签名与摘要密钥默认原子持久化到权限 `0600` 的 `data/.app-secret`，生产/多实例也可显式设置同一个 `APP_SECRET`。

`TRUST_PROXY=1` 才采信 `X-Forwarded-For`（否则客户端可伪造头绕过所有基于 IP 的风控），并连带开启 Cookie `Secure`；从右侧按 `TRUST_PROXY_HOPS` 取值。Nginx 应覆盖为 `$remote_addr`，不要用会保留客户端伪造左侧值的 `$proxy_add_x_forwarded_for`。

### 环境变量

`ADMIN_PASSWORD`(默认 douyin-admin，生产必改) · `DATA_DIR`(默认 `data`) · `APP_SECRET`(可选；未设则原子生成 `data/.app-secret`) · `CAPTCHA_SECRET`(旧部署兼容/可单独覆盖) · `DATA_RETENTION_DAYS`(1–30，默认 30) · `FREE_ANON_DAILY`(3) · `FREE_USER_DAILY`(10) · `QUOTA_RESERVATION_TTL`(3600 秒) · `NEW_KEY_BALANCE`(新 Key 试用余额，分) · `API_JOB_WORKERS`(2) · `API_JOB_LEASE_SECONDS`(600) · `API_JOB_HEARTBEAT_SECONDS`(30) · `MEDIA_TOKEN_TTL`(43200) · `MEDIA_REQUESTS_PER_MIN`(120) · `MEDIA_MAX_CONCURRENT`(6) · `TRUST_PROXY` · `TRUST_PROXY_HOPS`(1) · `COOKIE_SECURE` · `SHARE_DOMAINS`(分享域名池，逗号分隔) · `SHARE_TTL_ANON_DAYS`(7) · `SHARE_TTL_USER_DAYS`(30) · `MIHOMO_VERSION` / `MIHOMO_DL_BASE` / `MIHOMO_OFF`(内置内核版本/下载源/总开关) · `HOST`(run.sh 默认 127.0.0.1) · `PORT`(仅 `run.sh` 用)。

**运行时可改的配置不走环境变量**：API 单价、微信公众号密钥、机场订阅、分享主域名、AnyToCopy 主解析都在 `app_settings` 表（`api_price_cents` / `wx_appid` / `wx_secret` / `mihomo_sub_url` / `share_primary_domain` / `atc_api_key` / `atc_api_secret` / `atc_enabled` / `atc_transcript_enabled` / `atc_transcript_daily` / `atc_url_ttl` / `share_play_priority`，`atc_base_url` / `atc_play_enhance` 仅保留旧库兼容；后台改、即时生效）。AnyToCopy 基址固定为 `ATC_DEFAULT_BASE`，不能由后台覆盖，防止凭据发往非预期主机。代理列表与轮换策略在 `data/config.json`。新增"运营要随时调"的开关优先进 `app_settings` + 后台，而不是加环境变量。

### 前端 i18n / SEO

服务端按 `?lang=` → cookie → `Accept-Language` 选语言，`_seo_head()` 生成整段 title/description/OG/hreflang/JSON-LD（含 FAQPage、HowTo 结构化数据），并注入 `window.__LANG`。前端 HTML 里写的是中文原文，英文通过 `I18N.en` 字典 + `data-i18n` / `data-i18n-html` / `data-i18n-ph` 属性覆盖。**新增文案要同时加中文 HTML、`data-i18n` 属性和 en 词条**；改 SEO 文案要同步 `_seo_head` 的 zh/en 两份。

## `oss/` —— 独立的开源精简版

`oss/` 不是本服务的一部分，而是要 force-push 到公开仓库 `d100000/dy-download` 的**最小可用版**（约 300 行：只有粘贴链接 → 解析 → 视频同源流式下载 / 图集浏览器直连，代理仅一个 `PROXY` 环境变量）。管理后台、代理池、用户体系、计费 API、数据分析**不得进入 `oss/`**。它与根目录的 `server.py` / `static/index.html` 是手工同步的两份代码：改了根目录的解析逻辑，若需要同步，要手动移植到 `oss/server.py`，反之亦然；`tests/test_download_flow.js` 同时覆盖两份前端下载实现，可兜底同步回归。发布流程见 `oss/PUBLISH.md`（`./publish.sh` 会强制覆盖远程历史）。

## 参考文档

`docs/产品文档.md`：解析方案的实测记录、抖音六层限制机制与代理池对策。`docs/软件介绍.md`：功能全貌与架构概述。`docs/分享页功能规划.md`：分享页的产品方案、微信兼容专项与待实测清单。`docs/商业化与产品规划.md`：三视角商业化方案。`docs/机场代理接入.md`：机场订阅（vmess/trojan 等）无法直接入池，用 mihomo 边车落地成本地 socks5 端口再加进代理池的部署方案。
