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
bash tools/test.sh                        # 全部 Python + Node 回归；先装 requirements-dev.txt
.venv/bin/python -m unittest tests.test_security_reliability.DurableBillingTests   # 单跑一个类（加 .方法名 可单跑一个用例）
node --test tests/*.js                     # 前端逻辑测试；必须写 *.js 且在仓库根目录跑（文件名不匹配默认 glob，裸目录会报错）
swift tools/render_og.swift static/og.svg static/og.png   # 重渲染 og.png 位图（1200×630，macOS/AppKit）
docker build -t douyin-dl . && docker run -p 3344:8000 -e ADMIN_PASSWORD=a-strong-random-password -v $(pwd)/data:/data douyin-dl
```

页面：`/` 下载器 · `/transcript` 文案提取落地页 · `/api-docs` API 文档 · `/api-console` 用户 API 控制台 · `/admin_d` 管理后台（隐藏入口，首页不暴露） · `/s/{sid}` 分享页。
站点级路由（改站点文案/新增可收录页面时要一并同步）：`/robots.txt`、`/llms.txt`、`/sitemap.xml`、`/og.svg`（内联 SVG 卡片图）、`/og.png`（`static/og.png` 位图，微信/多数抓取器不渲染 SVG，卡片兜底必须用它）。

依赖刻意保持最小：`fastapi` / `uvicorn` / `PySocks`（socks 代理）/ `openpyxl`（批量导出 xlsx）/ `segno`（分享页二维码），**无 requests**。抖音官方完整视频链路还需要系统可执行的 Chrome/Chromium（可用 `DOUYIN_BROWSER_BIN` 指定）；没有浏览器时只尝试官方 SSR/JSON-LD 元数据，不能保证返回媒体地址。仓库 Dockerfile 已安装 `/usr/bin/chromium`，裸机部署需自行安装浏览器或设置 `DOUYIN_BROWSER_BIN`；运行依赖通过 requirements-lock.txt 固定兼容版本；测试依赖见 requirements-dev.txt，统一入口 tools/test.sh，以包名发现测试，不要使用缺少 -t . 的 unittest discover。新增任何运行时需要的文件（静态资源、模板）必须确保它在 `static/` 内且已提交，否则容器里 404/500。

测试在 `tests/`（命令见上），改安全/计费/配额/媒体流/前端下载播放相关代码后必须跑：`test_security_reliability.py` 是后端套件（覆盖媒体签名与 Range/流租约、代理健康、原子配额、持久化计费、隐私存储、按平台解析与主动文案模式、播放日志及文案回归；导入 `server` 前已设临时 `DATA_DIR`/`MIHOMO_OFF=1`，不碰真实 `data/`）；三个 Node 测试（`test_download_flow.js`、`test_share_playback.js`、`test_video_metadata.js`）会把 `static/index.html`、`static/share.html`、`oss/static/index.html` 里的 JS 按**锚点字符串**（`function downloadTarget`、`let _playSession = 0;` 等）切出来在 vm 里跑——改前端下载/播放代码时必须保留锚点或同步更新测试。本地回归必须使用独立空闲端口和独立 `DATA_DIR`，不得把用户正在查看的端口切换到另一份测试数据库；服务重启需保留原数据目录和 `.app-secret`，否则存量分享将 404、旧签名将 403。测试不能替代真实链接实测：解析服务和源平台随时可变，改解析逻辑必须手动跑服务验证。无 lint 配置。`/healthz` 可做存活探针（含 `version` 字段）。

## 版本号与 README 维护（每次改动必做）

版本号唯一来源是 `server.py` 顶部的 `APP_VERSION`（语义化：修 bug +patch，新功能 +minor，不兼容改动 +major）。**每次功能性改动必须**：① bump `APP_VERSION`；② 同步 README.md 顶部版本号；③ 在 README「更新日志」表新增一行（版本、日期、内容）；④ 若功能有增删，同步 README 功能列表与部署说明。仅改文档/注释不 bump。

## 架构

### 单文件后端 + 单文件前端

`server.py`（约 9200 行）是全部后端，按注释分隔线分层，大体顺序即依赖顺序：常量 → SQLite 层 → 防薅羊毛/限频 → 用户鉴权/防机器人 → 代理池 → 内置 mihomo 内核 → 应用设置+计费 → HTTP 出站层 → 工具函数 → **按平台解析（抖音官方 + AnyToCopy/ATC 兼容层）** → 分享页 → 公共 API → 开放 API v1 → 管理后台 → 健康检查 → 页面+SEO → **用户鉴权 API**。定位代码用 `grep -n "^# -----" server.py` 列分隔线，不要记行号。新增功能应放进对应分区，不要拆包。

注意用户体系被拆成了相距很远的两块：**底层**（`hash_pw`/`current_user`/滑块验证码，在「用户鉴权 / 防机器人」分区）在文件前部，**路由**（`/api/auth/*`）在文件**最末尾**的同名分区。改用户相关功能时两处都要看。

`static/*.html` 每个页面是自包含单文件（内联 CSS + 原生 JS，无构建、无框架、无依赖）。没有 `StaticFiles` 挂载，每个页面都有独立路由，分两类：
- **模板替换型**：`index.html` / `api-docs.html`（替换 `{{HTMLLANG}}` / `{{SEO_HEAD}}` / `{{ORIGIN}}`）、`share.html`（见分享页一节）。
- **仅版本注入型**：`admin.html`（`/admin_d`）、`api-console.html`（`/api-console`），不做 SEO。全部页面经 `_frontend_template()` 注入应用/时间戳版本，`_frontend_response()` 禁止持久缓存；本地图标的 `?v=` 与启动时固定的 `FRONTEND_VERSION` 一致时才使用 immutable 长缓存。

首页底部通过 `{{FRONTEND_VERSION}}` 展示同一版本标识，由 `_frontend_template()` 在服务端替换；不要在 HTML 写死版本，也不要另发请求获取版本。中英标签使用首页现有的翻译表。

新增可被搜索引擎收录的页面时，必须走模板 + `_seo_head()`，否则占位符会原样输出到页面。

### 解析链路（`_parse_share`）

从分享文案提取安全 HTTPS 公开作品链接，网页、开放 API 与分享 worker 统一经过 `_parse_cached()` / `_atc_parse_work_url()`。优先调用主解析服务；抖音缺少媒体、作者、互动数或媒体元数据时调用 `_complete_douyin_result()`，通过官方接口只补缺失字段。主服务失败则走官方兜底。合法统计值 0 不视为空，补充失败不丢已有媒体，不可混合不同作品或不同来源的媒体地址与下载端点。普通解析只传 `workUrl`，主动转录才传 `taskType=TEXT`。

`server.py` 使用主服务优先、抖音官方补全；`oss/server.py` 与 `douyin_dl.py` 是独立的旧实现，不共享主服务配置。

2026-09-09 已核对官方视频文档 `https://www.anytocopy.com/account/api/docs`：POST `/video/extract` 与 GET `/video/query` 均使用 query 参数，鉴权只放请求头。`WAITING` / `PROCESSING` 以及 `FAILED` / `FAILURE` 响应也可能带媒体地址，所有入口必须使用 `_atc_result_complete()`；`_atc_basic_result_ready()` 只表示有媒体，不能作为显式任务状态的替代。仅无状态的旧同步响应兼容立即返回。查询可重试原 taskId，但未知是否受理的 POST 不自动重发；并发拒绝退回队列后至少等 5 秒，各 purpose 都受超时约束。`_ParserServiceError` 的内部分类区分临时网络异常和永久鉴权/权益失败，公开错误不得包含上游名称或原文。`content` 与 `title` 分别保存；`createBy` / `createTime` 是任务元数据，不能映射为作品作者/发布时间。文档没有保证互动数、作者详情和媒体地址 TTL，继续按需补全与点击刷新。

### 部署功能检查与元数据修复

`/api/admin/checks` 及其 `/run`、`/shares/{sid}/repair` 仅管理员可访问。GET 只读配置与最近 100 份有效分享；真实上游调用只由显式 POST 启动，全局一项异步任务，完成冷却 10 秒，结果在内存中保存。报告只允许有限状态/错误分类和公开元数据，禁止异常原文、签名媒体链接或凭据。媒体预检仅检查 1 字节，不能宣称完整下载/微信播放已验证。

`_metadata_missing()` 判断核心展示字段，不把有效的 0 当缺失。主服务抖音缓存缺少元数据时，`_retry_cached_metadata()` 每作品最多每 60 秒重试官方补全，不重复提交主解析。成功解析的 `_remember_parse_result()` 同步补齐同作品至多 500 份旧分享的缺失字段，保留已有计数、媒体线路、自定义标题、有效期和下架状态；更新缓存别名时保留原过期时间。`_merge_metadata_snapshot()` 必须校验 item_id/kind/platform。分享 GET 不补数据、不请求上游；浏览器打开后预检媒体元数据，仅缺失/失效时通过签名 POST 续期。新增回归在 `tests/test_function_checks.py`，修改本链路须加入常规后端测试命令。

### 流量分工：解析服务地址与同源流式媒体

新主服务响应使用 `source: parser`、`video.direct_url` 与 `/api/media/video/{item_id}`；官方补充媒体使用 `/api/douyin/video/{item_id}`。解析页、批量与分享页通过 `videoDirectDownloadURL()` 选主服务原片（兼容旧 `atc_url`），浏览器 CORS 下载、显示进度，校验 200/媒体类型/非空/已知长度后保存 Blob；不得把官方接口重定向地址当主服务原片。主服务地址缺失/过期或直连失败时，下载和播放恢复均 POST `video.download_refresh_url`（`/api/media/video/{item_id}/download-link`，独立 `download_link` HMAC）。返回 202 时显示加载并有界轮询，后台复用 `atc_jobs` 的 `download` 任务，优先用 `parse_snapshots.canonical_url/source_url`、有效分享的 `source_url`，再回退 `atc_cache.work_url` 保存的来源重新提交普通解析；不调用官方补全、不覆盖信息快照、不重复扣解析配额。任务创建原子去重、最多 32 个在途、30 秒冷却和 5 分钟超时。新地址重新缓存并更新前端媒体；失败后才预检同源备用流，服务器默认代理优先，空代理池或全部失败后直接连接；管理员可显式开启严格代理模式。媒体路径必须与缓存来源匹配；旧 `/api/atc/video` 路由仅作兼容。同源下载保留签名、白名单、Range 与并发校验。

ATC 的 `duration_ms`、`video.width`、`video.height` 可能为空。首页不得用 `0:00`、`720P` 等看似真实的值兜底；单条结果通过 `<video preload="metadata">` 的 `loadedmetadata` / `durationchange` 从媒体链接补全，失败显示「暂未读取」。批量列表为避免一次解析触发几十条媒体请求，只在用户打开视频预览时读取并回填对应行。

**抖音 CDN 已上线 Referer 防盗链**（v1.10.1 实测：douyinvod 对带第三方站点 Referer 的媒体请求一律 403，无 Referer 或 douyin.com Referer 正常）。因此所有嵌入抖音媒体的页面（`static/index.html`、`static/share.html`、`oss/static/index.html`）的 `<head>` 必须保留 `<meta name="referrer" content="no-referrer">`——`<video>` 元素不支持 `referrerpolicy` 属性，只有文档级策略能覆盖媒体请求；新增引用抖音直链的标签/页面时不要绕过它。服务器侧出站不受影响：`CDN_HEADERS` 固定带 `Referer: https://www.douyin.com/`。

### 出站请求：统一解析服务与媒体出站

所有平台普通解析优先通过 `_atc_request()` 请求服务端固定基址；抖音缺失字段或主服务失败时才访问官方网页/接口。普通解析不请求转录。官方补充与媒体请求继续遵守代理策略。

### 代理池

v1.24.0 起 `force_proxy` 默认 false，代理优先，空池 / 全部失败后直连。旧配置未带 `proxy_policy_version=1` 时只迁移一次，之后保留管理员显式选择。mihomo 内部 blackhole 保持，指定 `proxy_override` 的 IP 绑定请求不隐式换出口。主解析视频备用流仅允许 `_primary_media_allowed()` 中的抖音 / TikTok CDN；抖音官方路径继续使用原 `_host_allowed()`。

`ProxyManager`（`threading.Lock` 保护）持久化在 `data/config.json`（代理列表 + 策略），不在 SQLite 里。支持 `scheme://user:pass@host:port`、`host:port:user:pass`、`user:pass@host:port`、`host:port` 四种输入格式（`parse_proxy` 归一化，无前缀按 `default_protocol`，默认 socks5）。策略：`round_robin`/`random`/`least_fail`、每请求重试数、连续失败自动禁用、后台 `_health_loop` 定时并发测速（出口 IP + 抖音可达性）并对恢复的代理自愈解禁。

**内置 mihomo 内核（机场订阅）**：代理池只认 http/socks，机场的 vmess/trojan 等加密协议进不来，因此由 `MihomoManager`（`server.py` 内「内置 mihomo 内核」分区）把订阅落地成一个本地 socks5 端口再接进池子。它托管一个完整的子进程生命周期：`ensure_binary()` 按平台下载内核（`data/mihomo/`，`MIHOMO_DL_BASE`/`MIHOMO_VERSION` 可换源换版本，`MIHOMO_OFF=1` 全禁）→ `write_config()` 渲染 YAML → `_start_locked()`/`stop()`/`reload()` → 后台 `supervise()` 守护线程（每 5s，仿 `_health_loop`）。`ProxyManager.sync_managed()` 维护那条 `id="mihomo"` 的托管代理条目。后台 `/api/admin/mihomo` 读写，订阅存 `app_settings` 表的 `mihomo_sub_url`。改动时注意三条硬约束：① **隔离**——只绑 `127.0.0.1` + `allow-lan:false` + 随机高端口 + `authentication` 账密（连本机也要验证），保证同机其他项目连不进、不改系统代理、不开 TUN；② **fail-closed**——`fallback` 组必须挂一个永远失败的 `blackhole` 成员，否则空 provider 会让内核注入 COMPATIBLE(=DIRECT) 导致服务器真实 IP 泄漏；③ **exec 路径**——用 `MIHOMO_BIN.resolve()` 绝对路径且不传 `cwd`（`DATA_DIR` 是相对路径，传 cwd 会让相对二进制路径解析错）。子进程随服务启停（startup 起 `supervise`，shutdown 调 `stop`），pidfile 清理孤儿，务必单 worker。

### 分享页（`/s/{sid}`）

把抖音和 TikTok 视频链接变成"微信里点开就能看"的页面。规划见 [docs/分享页功能规划.md](docs/分享页功能规划.md)。

同样**不落地任何媒体字节**：`shares` 表只存净化后的元数据快照，抖音短时媒体地址和刷新所需的官方作品链接留在受保护列 / 内存缓存中，不进入公开 payload；其他平台的短时地址保存在 `atc_cache`。成功解析通过 `_remember_parse_result()` 把完整展示数据写入 `parse_snapshots`（24 小时 TTL，每 5 分钟清理），作者补充资料同步保存到有效快照；已生成分享仍按自身有效期保存。分享页读取不得调用上游或 `_atc_enqueue()`，媒体仅在播放/下载时按需刷新；不要恢复访问时自动刷新/轮询媒体的旧逻辑。`_parse_share()` 处理新短链，`_parse_item(kind,item_id)` 处理存量分享页刷新：主服务优先，抖音缺失信息走官方补全；升级前已存的 `vid` 仅作为历史兼容数据处理，不得成为新解析的旁路。

`static/ui-locales.json` 与 `static/ui-i18n.js` 由 `_frontend_template()` 内联至首页与分享页，保持 HTML 自包含。静态系统标签使用 `data-ui`，动态反馈使用 `uiText()`；不要翻译作品标题、作者、文案或标签。语言按 query / Cookie / Accept-Language 选择，分享页使用 `{{LANG}}` 与切换按钮。

`static/share.html` 是独立模板（占位符 `{{SHARE_HEAD}}` / `{{SHARE_DATA}}` / `{{WECHAT}}`），刻意比首页轻——微信里首屏要 1 秒内出内容，因此不要往里加极光动画之类的装饰。注入 `{{SHARE_DATA}}` 的 JSON 已把 `<` 转义，改这段时别把转义丢了。

分享页 SEO 走 `_share_head()`（per-share OG）而非 `_seo_head()`，且**一律 noindex**，`robots.txt` 也 Disallow `/s/`——不收录他人作品是合规底线，不要"优化"掉。

分享页由 `app_settings.share_play_priority` 驱动：抖音优先使用官方签名直链，过期或 WebView 拦截时切换到 `/api/douyin/video/{item_id}`；其他平台使用兼容层直链及 `/api/atc/video/{item_id}`。旧 `/api/video/{vid}` 仅供升级前已有 `vid` 的分享数据。微信 WebView 拦截直连时常不报 error 只挂起，因此看门狗会在超时后落到同源流。每级尝试会记录 `play_try`、`fallback`、`play_ok` / `play_fail`（含 source/stage/detail/ms，失败额外记下一线路 `next_src`，但不记带签名地址）。后台「分享页」和「播放日志」页签用于诊断成功率、耗时与失败链路。数据库列仍通过 `PRAGMA table_info` + `ALTER TABLE` 就地补齐。

**转发流量统计**：`/api/video`、`/api/douyin/video` 与 `/api/atc/video` 每条流结束时按实际转发字节计量（`_ResumableVideoStream.sent`，经 `_media_finalizer` 的 `on_close` 钩子），先进内存 `_traffic_pending`（finalize 可能跑在事件循环线程，不能直接写 SQLite），由 `_sweeper` 每 5 分钟、后台读取时与 shutdown 时 `_flush_media_traffic()` 落库到 `media_traffic` 表（天 × 用途聚合，`scope`=play/download，无个人标识，保留 1 年）。后台看板走 `GET /api/admin/traffic-stats?days=`；浏览器直连媒体的流量不经过服务器。

**域名池**：`_share_origin()` 给新链接分配域名，优先级为 **后台主域名（`app_settings.share_primary_domain`，即时生效不重启）→ `SHARE_DOMAINS` 环境变量域名池（轮换）→ 请求来源**。短码与域名解耦，某域名被微信封了在后台停用即可（`share_domains_off` 也对主域名生效，会自动退回池子/来源），存量链接换域名照样打开。主站域名与分享域名要物理隔离——分享域名被封是日常，主站被牵连是灾难。

**微信卡片**：两条完全不同的机制，别混。① **粘贴链接进聊天窗口 → 永远是纯文字链**，微信不做 URL 展开，任何 og 标签都改不了这一点；② **在微信内打开页面 → 点右上角 ··· 转发 → 才是卡片**。网页无法用按钮唤起微信分享面板，只能引导用户去点 ···。所以产品文案不要承诺「发到微信会自动变成卡片」——首页弹窗、分享页引导、FAQ（中英 + JSON-LD）、`llms.txt` 里的说法必须一致，改一处要四处同步。

卡片图走 `_card_cover()`：抖音封面原始直链是 `p26-sign.douyinpic.com/….webp?x-signature=…&x-expires=…`，**去掉主机的 `-sign` 并把扩展名换成 `.jpeg`**，抖音会返回同一张图的**无签名、不过期 JPEG**（签名覆盖路径，只换扩展名不去 `-sign` 会 403，两步必须一起做）。这解决 webp 在微信缩略图上支持不稳定、以及签名约 14 天过期后存量分享页全变无图卡片两个问题。无封面时兜底 `og.png` 而**不是 `og.svg`**——微信不渲染 SVG。

**微信 JS-SDK**：`/api/wx/jssdk` 出签名，未配置公众号时返回 `enabled:false`，前端静默降级到微信默认卡片。`jsapi_ticket` **存 app_settings 表**（全局唯一 + 有频次上限，存内存会导致多 worker 互相顶掉）。这里的出站请求**刻意不走 `open_url()`**：公众号要求服务器出口 IP 在白名单内，走代理反而失败。

**海报**在前端用 canvas 合成（封面走 CDN 的 `ACAO:*` 跨域绘制，二维码用同源 PNG——SVG 在部分内核会污染画布导致导出抛 SecurityError），服务器同样不参与、不落地。微信封图片远少于封链接，海报是链接被封时的传播兜底。导出**必须用 `toDataURL()` 出 `data:` URI，不要用 `toBlob`+`createObjectURL`**：微信 WebView 对 `blob:` 图片长按不弹「保存图片／发送给朋友」，海报就既存不下也发不出去。

### 按平台解析（抖音官方 + AnyToCopy / ATC 兼容层）

抖音短链、数字作品 ID、分享 worker 与缓存刷新均优先使用主解析服务，再由官方链路按需补全。主任务最多 3 个在途，后台任务最多 2 个；文案任务仍需用户主动开启并按原配额结算。作者接口只补缺失字段，不覆盖已有精确计数；数值统一为整数或 null。公开接口使用中性名称，兼容旧路由和内部数据库字段。

### 算术题、滑块验证码与 `pass_token` 门禁

注册/登录先调用 `/api/auth/math` 取得一次性加减题，再以 `/api/auth/math/verify` 验证答案；通过后用返回的 `math_token` 放入 `X-Auth-Math` 请求头加载 `/api/auth/captcha`。题目与授权均绑定 IP、有效期 5 分钟，题目尝试一次即作废，授权最多加载 10 张滑块。不得仅在浏览器判断答案或无门禁签发滑块。

自研的防机器人链路（无 Pillow、无第三方库）：`_png()` 是手写的极简 PNG 编码器（stdlib `zlib`+`struct`），`make_captcha()` 服务端生成带缺口的背景图。**缺口坐标只存在服务端 `_captchas` 与像素里**，绝不出现在响应体中——抓包拿不到答案，改这段时别把坐标漏进返回值。`verify_captcha()` 校验落点 + 行为轨迹 + PoW（`POW_BITS=14`，抬高批量自动化成本）+ 蜜罐字段。

关键边界：滑块签发的 `pass_token` **只保护 `/api/auth/register` 与 `/api/auth/login`**，由 `_do_auth_guard()` 一次性消费。`/api/parse`、`/api/parse/batch` 与 `/api/share` 不经过滑块；网页解析靠原子免费额度预占保护，开放 API `/api/v1/*` 则用 API Key 原子计费。新增注册/登录入口必须复用滑块令牌；新增解析入口必须复用额度预占与失败退款，不能把两套门禁混在一起。验证码接口本身有 `_captcha_rate_ok()` 限频，防的是生成图片的 CPU-DoS。

### 配额与计费

- **网页余额**：`users.balance_cents/reserved_cents/spent_cents/wallet_version` 与 `wallet_ledger` 保存整数分及流水。`_reserve_quota_in_conn()` 先预留免费次数，再按事务内读取的 `web_parse_price_cents` / `transcript_price_cents`（各默认 3）预留余额；`quota_reservations.free_units/price_cents/user_id` 锁定计费方式。结算只保留成功数，优先使用免费次数，其余退款。旧预留 `free_units IS NULL` 按全免费兼容。不得把网页余额和现有 API Key 余额混用。
- **文案结算**：`atc_jobs.quota_reservation_id` 与任务入队一起保存；`_atc_claim_update()` 把终态与结算原子提交，启动恢复也结算过期/终态任务。普通预留清理不能退掉尚在队列中的分享或文案。缓存和在途文案重复请求不收费。管理员调整余额必须使用版本校验及 request_id 幂等键，设置的是可用余额，不动预留。`tests/test_wallet_billing.py` 与 `tests/test_wallet_ui.js` 覆盖财务回归。

- **每日免费次数配置**：后台用户页“网页额度与计费”通过 `/api/admin/billing-prices` 的可选 `user_daily` 字段保存 0–10000 的整数，不传字段保持旧配置。保存无需重启，不重置 `usage_daily`；已有预留仍按原免费单位和价格结算。`/api/quota`、后台用户列表与首页中英注册提示必须统一读取配置，不能再写死每日 10 次。匿名和文案免费次数独立。
- **网页免费配额**：`usage_daily` 表按 subject 计数，登录用户按 `user:{id}`（运行时 `free_user_daily()` 读取后台 `app_settings.free_user_daily`，未设置才回退 `FREE_USER_DAILY`），匿名按用途化 HMAC 后的 `ip:` + 30 天随机第一方匿名 ID `fp:`（取最大值，`FREE_ANON_DAILY`）。解析前必须通过 `reserve_quota()` 在 `BEGIN IMMEDIATE` 事务里原子预占；成功用 `settle_quota()` 结算实际条数，失败或未处理部分必须 `release_quota()` / 结算退款，过期预占由后台清理。
- **开放 API 计费**（分为单位）：创建 `/api/v1/jobs` 时在同一个 `BEGIN IMMEDIATE` 事务里快照单价、整批扣减 `balance_cents`、增加 `reserved_cents`，并写入 `jobs`、逐条 `job_items` 与 `api_ledger reserve`；余额不够则整批拒绝。`Idempotency-Key` 可安全重放。非 daemon 的有界 worker 通过 `_claim_job_item()` 获取数据库租约，`_finish_job_item()` 用 CAS 在同一事务里把成功项从 reserved 转入 spent/calls，失败项精确退回 balance，同时写账本和唯一 `api_logs`。启动时 `_recover_legacy_api_jobs()`、`_reconcile_api_job_accounts()` 恢复旧任务并对账，重启不会丢任务或重复计费。`job_items` 是事实源，`jobs` 只是聚合/结果快照；不要退回“一请求一条 daemon 线程”的实现。

### 隐私与数据最小化

- 前端只允许使用 `dyanon`：16 字节随机第一方匿名 ID，固定 30 天过期；加载时主动删除旧 `dyfp`。**不得重新引入 Canvas、硬件参数、屏幕、字体等浏览器指纹。** 请求头继续用 `X-FP` 只是为了后端兼容。
- 服务端持久化网络标识前必须经 `APP_SECRET` 做按用途、按周期隔离的 HMAC 摘要；浏览器信息只保留诊断所需的粗粒度字段。访问、解析、播放事件与 API 任务/结果的保留期为 `DATA_RETENTION_DAYS`（强制 1–30 天，默认 30 天），到期数据由每 5 分钟执行的清理任务删除。
- 默认部署必须关闭 Uvicorn/Nginx access log；不得让原始 IP、完整 UA/Referer、媒体签名、下载文件名或 API Key 进入 URL/日志。开放 API 只从 `X-API-Key` 读取密钥，吊销和充值等管理操作把密钥放 JSON body。
- 站内账号是可选功能；注册会保存邮箱与加盐密码哈希，账号资料随账号保留。服务器不落地保存视频或图片文件，媒体线路只做流式转发；公开作品链接优先提交给已配置的内容解析服务，抖音缺失字段由官方接口补全。普通解析不请求语音文案，主动文案任务才传 `taskType=TEXT`；浏览器直连媒体时，媒体源会收到请求方的网络与浏览器信息。
- 对外文案不得使用“零隐私采集”“不采集任何数据”“不记录账号”等绝对说法。中英文页面与 README 应明确以上数据范围、保留期和第三方直连边界。

### 来源存储与播放续期

`parse_snapshots.source_url/canonical_url` 单独保存从原输入提取的分享链接与可用的原平台作品链接，抖音作品规范化到无追踪参数的 video/note URL；不保存整段分享文案。公开 payload 继续移除内部来源与临时媒体签名。解析快照保留 24 小时，创建分享时把来源复制到 `shares.source_url`，随分享自身有效期保留；启动迁移从旧 `atc_cache.work_url` 补空来源。`_saved_source_in_conn()` 只恢复有效记录，刷新只改媒体缓存，不延长分享有效期、不重新计费、不清空完整元数据。

分享页下载前通过 `refreshShareDownloadData()` 重读 `/api/share/{sid}`，只更新媒体字段与签名，不重复解析或扣次；404/过期/下架禁止继续使用旧地址，下载按钮全流程防连点。分享页 `prepareShareMedia()` 打开时仅加载媒体元数据，有效不调用解析；无地址、error 或 12 秒未读到元数据时复用有界下载刷新任务。播放错误先续期一次，再走已有备用线路。首页/批量预览同样按需续期，不能自动播放原本暂停的视频，也不能让迟到响应修改已替换的视频元素。中英提示使用 `uiText()`，新状态测试在 `test_playback_refresh.js` 与 `test_share_playback.js`。

### 状态存储与部署约束

SQLite 在 `data/app.db`（WAL），所有访问经 `db_exec()` + 全局 `_db_lock`；schema 在 `_SCHEMA` 里用 `CREATE TABLE IF NOT EXISTS` 就地演进（无迁移框架，改表要自行考虑既有库的兼容）。

**普通用户会话已持久化**：`user_sessions` 只保存随机令牌的 SHA-256 摘要，浏览器使用 30 天 HttpOnly/SameSite=Lax Cookie；重启不掉线，退出删除对应摘要，禁用/删除用户立即失效。旧版内存会话升级后需重新登录一次。管理员会话与验证码仍在进程内存（`_sessions`、`_math_challenges`、`_math_grants`、`_captchas`、`_passes`、限频计数），每 5 分钟清理，仍须单 worker 部署。开放 API 作业及计费已持久化，不受该限制。签名与摘要密钥默认原子持久化到权限 `0600` 的 `data/.app-secret`，生产/多实例也可显式设置同一个 `APP_SECRET`。

`TRUST_PROXY=1` 才采信 `X-Forwarded-For`（否则客户端可伪造头绕过所有基于 IP 的风控），并连带开启 Cookie `Secure`；从右侧按 `TRUST_PROXY_HOPS` 取值。Nginx 应覆盖为 `$remote_addr`，不要用会保留客户端伪造左侧值的 `$proxy_add_x_forwarded_for`。

### 环境变量

`ADMIN_PASSWORD`(本地默认 douyin-admin；Docker 必填强密码) · `REQUIRE_ADMIN_PASSWORD`(Docker 默认 1，密码缺失/过短/默认值时拒绝启动) · `DATA_DIR`(默认 `data`) · `APP_SECRET`(可选；未设则原子生成 `data/.app-secret`) · `CAPTCHA_SECRET`(旧部署兼容/可单独覆盖) · `DATA_RETENTION_DAYS`(1–30，默认 30) · `FREE_ANON_DAILY`(3) · `FREE_USER_DAILY`(10) · `QUOTA_RESERVATION_TTL`(3600 秒) · `NEW_KEY_BALANCE`(每用户仅首 Key 试用余额，分) · `API_JOB_WORKERS`(2) · `API_JOB_LEASE_SECONDS`(600) · `API_JOB_HEARTBEAT_SECONDS`(30) · `MEDIA_TOKEN_TTL`(43200) · `MEDIA_REQUESTS_PER_MIN`(120) · `MEDIA_MAX_CONCURRENT`(6) · `IMAGE_REQUESTS_PER_MIN`(240) · `IMAGE_MAX_BYTES`(50 MiB) · `ATC_WORKERS`(1) · `PARSE_TEXT_MAX`(8192) · `BATCH_TEXT_MAX`(65536) · `DOUYIN_BROWSER_BIN`(Chrome/Chromium 可执行文件，可选) · `DOUYIN_BROWSER_ENABLED`(auto/1/0) · `DOUYIN_BROWSER_TIMEOUT` · `DOUYIN_BROWSER_START_TIMEOUT` · `TRUST_PROXY` · `TRUST_PROXY_HOPS`(1) · `COOKIE_SECURE` · `SHARE_DOMAINS`(分享域名池，逗号分隔) · `SHARE_TTL_ANON_DAYS`(7) · `SHARE_TTL_USER_DAYS`(30) · `MIHOMO_VERSION` / `MIHOMO_DL_BASE` / `MIHOMO_OFF`(内置内核版本/下载源/总开关) · `HOST`(run.sh 默认 127.0.0.1) · `PORT`(仅 `run.sh` 用)。

**运行时可改的配置不走环境变量**：API 单价、微信公众号密钥、机场订阅、分享主域名、AnyToCopy 服务都在 `app_settings` 表（`api_price_cents` / `wx_appid` / `wx_secret` / `mihomo_sub_url` / `share_primary_domain` / `atc_api_key` / `atc_api_secret` / `atc_enabled` / `atc_transcript_enabled` / `atc_transcript_daily` / `atc_url_ttl` / `share_play_priority`，`atc_base_url` / `atc_play_enhance` 仅保留旧库兼容；后台改、即时生效）。AnyToCopy 基址固定为 `ATC_DEFAULT_BASE`，不能由后台覆盖，防止凭据发往非预期主机。代理列表与轮换策略在 `data/config.json`。新增"运营要随时调"的开关优先进 `app_settings` + 后台，而不是加环境变量。

### 前端 i18n / SEO

服务端按 `?lang=` → cookie → `Accept-Language` 选语言，`_seo_head()` 生成整段 title/description/OG/hreflang/JSON-LD（含 FAQPage、HowTo 结构化数据），并注入 `window.__LANG`。前端 HTML 里写的是中文原文，英文通过 `I18N.en` 字典 + `data-i18n` / `data-i18n-html` / `data-i18n-ph` 属性覆盖。**新增文案要同时加中文 HTML、`data-i18n` 属性和 en 词条**；改 SEO 文案要同步 `_seo_head` 的 zh/en 两份。

全部页面（包括管理后台）、API 文档与公开错误保持供应商中性：不展示底层服务商名称、接口基址、原始上游响应、凭据或堆栈。历史任务错误在读取时同样净化。内部函数名、持久化字段与服务端固定配置可保持兼容。隐私说明如实说明公开作品链接提交给第三方内容解析服务，抖音缺失字段由服务器补全。

## `oss/` —— 独立的开源精简版

`oss/` 不是本服务的一部分，而是要 force-push 到公开仓库 `d100000/dy-download` 的**最小可用版**（约 300 行：只有粘贴链接 → 解析 → 视频同源流式下载 / 图集浏览器直连，代理仅一个 `PROXY` 环境变量）。管理后台、代理池、用户体系、计费 API、数据分析**不得进入 `oss/`**。它与根目录的 `server.py` / `static/index.html` 是手工同步的两份代码：改了根目录的解析逻辑，若需要同步，要手动移植到 `oss/server.py`，反之亦然；`tests/test_download_flow.js` 同时覆盖两份前端下载实现，可兜底同步回归。发布流程见 `oss/PUBLISH.md`（`./publish.sh` 会强制覆盖远程历史）。

## 参考文档

`docs/产品文档.md`：解析方案的实测记录、抖音六层限制机制与代理池对策。`docs/软件介绍.md`：功能全貌与架构概述。`docs/分享页功能规划.md`：分享页的产品方案、微信兼容专项与待实测清单。`docs/商业化与产品规划.md`：三视角商业化方案。`docs/机场代理接入.md`：机场订阅（vmess/trojan 等）无法直接入池，用 mihomo 边车落地成本地 socks5 端口再加进代理池的部署方案。

最新部署步骤与只读预检：`docs/项目部署文档.md`、`tools/deploy_check.py`；模板位于 `deploy/`。微信签名票据刷新与凭据编辑共用 `_wx_ticket_lock`，更改 AppID 或 AppSecret 均清除缓存；公开接口在网络/上游响应异常时中性降级，不返回异常原文。

### 管理员解析时间线

`parse_logs` 保存每次解析的有界白名单事件；`_parse_log_scope` + ContextVar 隔离并发，嵌套调用复用记录，网页/批量/API/分享入口保留自身入口标识。后台媒体与文案提交/轮询通过 `_traced_media_job` 复用运行中记录。日志写入失败不能影响解析、计费和退款；不得记录异常原文、完整链接、分享文本或请求头。新增阶段/原因必须同时补 `_PARSE_LOG_CODES` 中英词条，并保持 `_parse_log_public` 读取白名单净化。保留期清理和启动中断标记必须保留。相关测试 `test_parse_logs.py` / `test_parse_logs_ui.js`。
