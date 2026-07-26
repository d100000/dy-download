# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

抖音无水印下载器：FastAPI 单体服务 + 代理池 + 管理后台 + 用户体系 + 按次计费的开放 API。代码与注释均为中文，保持一致。

## 常用命令

```bash
./run.sh                                   # 本地启动（首次自动建 .venv 并装依赖），默认 :3344
PORT=8010 ADMIN_PASSWORD=xxx ./run.sh      # 换端口 / 改管理密码
.venv/bin/uvicorn server:app --reload --port 3344   # 开发热重载（手动方式）
python3 douyin_dl.py "分享文案或短链" [输出目录]      # 纯标准库 CLI 版，不依赖服务
python3 tools/testproxy.py 8899            # 本地测试代理：验证"出站请求确实走代理"，逐条打印 CONNECT
docker build -t douyin-dl . && docker run -p 3344:8000 -e ADMIN_PASSWORD=xxx -v $(pwd)/data:/data douyin-dl
```

页面：`/` 下载器 · `/api-docs` API 文档 · `/api-console` 用户 API 控制台 · `/admin_d` 管理后台（隐藏入口，首页不暴露）。

无测试套件、无 lint 配置，验证靠手动跑服务 + 真实链接解析（抖音接口随时可变，改解析逻辑必须实测）。`/healthz` 可做存活探针（含 `version` 字段）。

## 版本号与 README 维护（每次改动必做）

版本号唯一来源是 `server.py` 顶部的 `APP_VERSION`（语义化：修 bug +patch，新功能 +minor，不兼容改动 +major）。**每次功能性改动必须**：① bump `APP_VERSION`；② 同步 README.md 顶部版本号；③ 在 README「更新日志」表新增一行（版本、日期、内容）；④ 若功能有增删，同步 README 功能列表与部署说明。仅改文档/注释不 bump。

## 架构

### 单文件后端 + 单文件前端

`server.py`（~2770 行）是全部后端，按注释分隔线（`# ----- 常量与存储`、`# ----- 代理池管理` …）分层，大体顺序即依赖顺序：常量 → SQLite 层 → 防薅羊毛/限频 → 用户鉴权/防机器人 → 代理池 → 应用设置+计费 → HTTP 出站层 → 工具函数 → 核心解析 → 分享页 → 公共 API → 开放 API v1 → 管理后台 → 健康检查 → 页面+SEO → **用户鉴权 API**。新增功能应放进对应分区，不要拆包。

注意用户体系被拆成了相距很远的两块：**底层**（`hash_pw`/`current_user`/滑块验证码，约 242 行起）在文件前部，**路由**（`/api/auth/*`）在文件**最末尾**。改用户相关功能时两处都要看。

`static/*.html` 每个页面是自包含单文件（内联 CSS + 原生 JS，无构建、无框架、无依赖）。没有 `StaticFiles` 挂载，每个页面都有独立路由，分两类：
- **模板替换型**：`index.html` / `api-docs.html`（替换 `{{HTMLLANG}}` / `{{SEO_HEAD}}` / `{{ORIGIN}}`）、`share.html`（见分享页一节）。
- **纯 `FileResponse`**：`admin.html`（`/admin_d`）、`api-console.html`（`/api-console`），无占位符、不做 SEO。

新增可被搜索引擎收录的页面时，必须走模板 + `_seo_head()`，否则占位符会原样输出到页面。

### 解析链路（`_parse_share`）

短链 302 取 `Location` → 判定 `video`/`note`/`slides` → 抓 `iesdouyin.com/share/{kind}/{id}/` → 正则提取 `window._ROUTER_DATA` JSON → 取 `item_list[0]` → 从 `play_addr` 抠出 `video_id` → 拼 `aweme.snssdk.com/aweme/v1/play/?video_id=...`（即 `playwm`→`play` 去水印）。分享页拿不到 `_ROUTER_DATA` 视为被风控：若走了代理则把该代理标记封禁并禁用。结果进 `_cache`（30 分钟，按原文与 item_id 双键）。

这套链路在仓库里有**三份互不共享代码的实现**，改动时要想清楚同步范围：`server.py`（主服务，且内部还有 `_parse_share` / `_parse_item` 两个入口）、`oss/server.py`（开源精简版）、`douyin_dl.py`（纯标准库 CLI）。抖音改页面结构时三份都会坏，但只有主服务是必须立刻修的。

### 流量分工：服务器只解析，字节走浏览器直连

这是全项目的核心设计取舍，改动前先理解：解析（短链 + 分享页）在服务器完成并走代理；**视频/图片字节由用户浏览器直连抖音 CDN**（`result.video.url` 是可直接 GET 的播放接口，浏览器自行跟 302）。`/api/video/{vid}` 与 `/api/media` 只是直连失败时的服务器兜底，会消耗服务器带宽并暴露服务器/代理 IP。因此已刻意去掉"图集打包 ZIP"这类必须服务器下载的功能——不要重新引入。

### 出站请求：一律经 `open_url()`

`open_url()` 是唯一允许的出站入口（基于 `urllib` + `PySocks`，无 requests）。它按 `ProxyManager.candidates()` 的轮换策略依次尝试代理，失败自动转移，403/401 或验证页判定为 IP 被封 → `mark_banned()` 落库 + 禁用 + 换下一个。`force_proxy=True`（默认）时没有可用代理就直接报 503，**绝不直连**——这是防止服务器真实 IP 暴露/被封的底线。任何新代码不得直接 `urlopen`/`requests` 请求抖音。

`/api/media` 有 SSRF 白名单 `ALLOWED_HOST_SUFFIXES`，且跟随重定向后会**复核最终 host**，新增代理型端点必须照做。

### 代理池

`ProxyManager`（`threading.Lock` 保护）持久化在 `data/config.json`（代理列表 + 策略），不在 SQLite 里。支持 `scheme://user:pass@host:port`、`host:port:user:pass`、`user:pass@host:port`、`host:port` 四种输入格式（`parse_proxy` 归一化，无前缀按 `default_protocol`，默认 socks5）。策略：`round_robin`/`random`/`least_fail`、每请求重试数、连续失败自动禁用、后台 `_health_loop` 定时并发测速（出口 IP + 抖音可达性）并对恢复的代理自愈解禁。

**内置 mihomo 内核（机场订阅）**：代理池只认 http/socks，机场的 vmess/trojan 等加密协议进不来，因此由 `MihomoManager`（`server.py` 内「内置 mihomo 内核」分区）把订阅落地成一个本地 socks5 端口再接进池子。它托管一个完整的子进程生命周期：`ensure_binary()` 按平台下载内核（`data/mihomo/`，`MIHOMO_DL_BASE`/`MIHOMO_VERSION` 可换源换版本，`MIHOMO_OFF=1` 全禁）→ `write_config()` 渲染 YAML → `_start_locked()`/`stop()`/`reload()` → 后台 `supervise()` 守护线程（每 5s，仿 `_health_loop`）。`ProxyManager.sync_managed()` 维护那条 `id="mihomo"` 的托管代理条目。后台 `/api/admin/mihomo` 读写，订阅存 `app_settings` 表的 `mihomo_sub_url`。改动时注意三条硬约束：① **隔离**——只绑 `127.0.0.1` + `allow-lan:false` + 随机高端口 + `authentication` 账密（连本机也要验证），保证同机其他项目连不进、不改系统代理、不开 TUN；② **fail-closed**——`fallback` 组必须挂一个永远失败的 `blackhole` 成员，否则空 provider 会让内核注入 COMPATIBLE(=DIRECT) 导致服务器真实 IP 泄漏；③ **exec 路径**——用 `MIHOMO_BIN.resolve()` 绝对路径且不传 `cwd`（`DATA_DIR` 是相对路径，传 cwd 会让相对二进制路径解析错）。子进程随服务启停（startup 起 `supervise`，shutdown 调 `stop`），pidfile 清理孤儿，务必单 worker。

### 分享页（`/s/{sid}`）

把抖音链接变成"微信里点开就能看"的页面。规划见 [docs/分享页功能规划.md](docs/分享页功能规划.md)。

同样**不落地任何媒体字节**：`shares` 表只存元数据快照 + `video_id`，播放地址每次渲染时用 `_play_api(vid)` 重拼（该地址无签名无时效，天然长期有效）；图集 CDN 直链会过期，超过 `SHARE_REFRESH_TTL` 时按 `item_id` 惰性重解析——为此 `_parse_share()` 已拆出 `_parse_item(kind, item_id)`，**改解析逻辑时注意两个入口都要能用**。

`static/share.html` 是独立模板（占位符 `{{SHARE_HEAD}}` / `{{SHARE_DATA}}` / `{{WECHAT}}`），刻意比首页轻——微信里首屏要 1 秒内出内容，因此不要往里加极光动画之类的装饰。注入 `{{SHARE_DATA}}` 的 JSON 已把 `<` 转义，改这段时别把转义丢了。

分享页 SEO 走 `_share_head()`（per-share OG）而非 `_seo_head()`，且**一律 noindex**，`robots.txt` 也 Disallow `/s/`——不收录他人作品是合规底线，不要"优化"掉。

播放走三级降级：浏览器直连 CDN → `/api/video/{vid}` 服务器代理 → 微信内引导"用浏览器打开"。每次降级上报 `fallback` 埋点，**这是服务器带宽成本的核心监控指标**。

**域名池**：`_share_origin()` 给新链接分配域名，优先级为 **后台主域名（`app_settings.share_primary_domain`，即时生效不重启）→ `SHARE_DOMAINS` 环境变量域名池（轮换）→ 请求来源**。短码与域名解耦，某域名被微信封了在后台停用即可（`share_domains_off` 也对主域名生效，会自动退回池子/来源），存量链接换域名照样打开。主站域名与分享域名要物理隔离——分享域名被封是日常，主站被牵连是灾难。

**微信 JS-SDK**：`/api/wx/jssdk` 出签名，未配置公众号时返回 `enabled:false`，前端静默降级到微信默认卡片。`jsapi_ticket` **存 app_settings 表**（全局唯一 + 有频次上限，存内存会导致多 worker 互相顶掉）。这里的出站请求**刻意不走 `open_url()`**：公众号要求服务器出口 IP 在白名单内，走代理反而失败。

**海报**在前端用 canvas 合成（封面走 CDN 的 `ACAO:*` 跨域绘制，二维码用同源 PNG——SVG 在部分内核会污染画布导致 `toBlob` 抛 SecurityError），服务器同样不参与、不落地。微信封图片远少于封链接，海报是链接被封时的传播兜底。

### 滑块验证码与 `pass_token` 门禁

自研的防机器人链路（无 Pillow、无第三方库）：`_png()` 是手写的极简 PNG 编码器（stdlib `zlib`+`struct`），`make_captcha()` 服务端生成带缺口的背景图。**缺口坐标只存在服务端 `_captchas` 与像素里**，绝不出现在响应体中——抓包拿不到答案，改这段时别把坐标漏进返回值。`verify_captcha()` 校验落点 + 行为轨迹 + PoW（`POW_BITS=14`，抬高批量自动化成本）+ 蜜罐字段。

关键耦合：**`/api/parse` 硬性要求一个一次性 `pass_token`**（`issue_pass()` 签发 → `consume_pass()` 消费，缺则必拒）。因此任何新的网页端解析入口都要先走滑块拿令牌；而开放 API `/api/v1/*` 用 API Key 计费，**不经过**这套门禁。验证码接口本身有 `_captcha_rate_ok()` 限频，防的是生成图片的 CPU-DoS。



- **网页免费配额**：`usage_daily` 表按 subject 计数，登录用户按 `user:{id}`（`FREE_USER_DAILY`），匿名按 `ip:` + 前端指纹 `fp:`（取最大值，`FREE_ANON_DAILY`）。仅解析**成功**才 `reserve_quota`。
- **开放 API 计费**（分为单位）：`try_reserve()` 用带条件的 `UPDATE ... WHERE balance_cents>=?` 做原子预扣，`api_settle()` 成功计入 spent/calls、失败退款并写 `api_logs`。`/api/v1/jobs` 提交后在 **daemon 线程**里跑 `_run_job`，客户端轮询 `/api/v1/jobs/{id}`；结果每 5 条落一次库以免 O(n²) 序列化。

### 状态存储与部署约束

SQLite 在 `data/app.db`（WAL），所有访问经 `db_exec()` + 全局 `_db_lock`；schema 在 `_SCHEMA` 里用 `CREATE TABLE IF NOT EXISTS` 就地演进（无迁移框架，改表要自行考虑既有库的兼容）。

**会话与验证码状态都在进程内存字典**（`_user_sessions`、`_sessions`、`_captchas`、`_passes`、限频计数），由 `_sweeper` 线程每 5 分钟清理。后果：重启即掉线；**多 worker 部署会话不互认**——默认按单 worker 运行，多进程时至少要设 `CAPTCHA_SECRET`，会话仍需改造为共享存储。

`TRUST_PROXY=1` 才采信 `X-Forwarded-For`（否则客户端可伪造头绕过所有基于 IP 的风控），并连带开启 Cookie `Secure`。

### 环境变量

`ADMIN_PASSWORD`(默认 douyin-admin，生产必改) · `DATA_DIR`(默认 `data`) · `FREE_ANON_DAILY`(3) · `FREE_USER_DAILY`(10) · `NEW_KEY_BALANCE`(新 Key 试用余额，分) · `TRUST_PROXY` · `COOKIE_SECURE` · `CAPTCHA_SECRET` · `SHARE_DOMAINS`(分享域名池，逗号分隔) · `SHARE_TTL_ANON_DAYS`(7) · `SHARE_TTL_USER_DAYS`(30)。API 单价与微信公众号密钥存在 `app_settings` 表（`api_price_cents` / `wx_appid` / `wx_secret`，后台可改），不是环境变量。

### 前端 i18n / SEO

服务端按 `?lang=` → cookie → `Accept-Language` 选语言，`_seo_head()` 生成整段 title/description/OG/hreflang/JSON-LD（含 FAQPage、HowTo 结构化数据），并注入 `window.__LANG`。前端 HTML 里写的是中文原文，英文通过 `I18N.en` 字典 + `data-i18n` / `data-i18n-html` / `data-i18n-ph` 属性覆盖。**新增文案要同时加中文 HTML、`data-i18n` 属性和 en 词条**；改 SEO 文案要同步 `_seo_head` 的 zh/en 两份。

## `oss/` —— 独立的开源精简版

`oss/` 不是本服务的一部分，而是要 force-push 到公开仓库 `d100000/dy-download` 的**最小可用版**（约 300 行：只有粘贴链接 → 解析 → 浏览器直连下载，代理仅一个 `PROXY` 环境变量）。管理后台、代理池、用户体系、计费 API、数据分析**不得进入 `oss/`**。它与根目录的 `server.py` / `static/index.html` 是手工同步的两份代码：改了根目录的解析逻辑，若需要同步，要手动移植到 `oss/server.py`，反之亦然。发布流程见 `oss/PUBLISH.md`（`./publish.sh` 会强制覆盖远程历史）。

## 参考文档

`docs/产品文档.md`：解析方案的实测记录、抖音六层限制机制与代理池对策。`docs/软件介绍.md`：功能全貌与架构概述。`docs/分享页功能规划.md`：分享页的产品方案、微信兼容专项与待实测清单。`docs/商业化与产品规划.md`：三视角商业化方案。`docs/机场代理接入.md`：机场订阅（vmess/trojan 等）无法直接入池，用 mihomo 边车落地成本地 socks5 端口再加进代理池的部署方案。
