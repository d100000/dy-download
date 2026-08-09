# 抖音无水印下载器

**当前版本：v1.14.0**（线上版本可通过 `GET /healthz` 的 `version` 字段确认）

无需登录抖音账号、基础解析无需注册本站账号的抖音视频 / 图集下载工具。粘贴分享链接，在线预览并下载无水印原片。内置**代理 IP 池 + 管理后台 + 可选用户体系 + 按次计费开放 API**，所有出站请求轮换走代理，防止服务器 IP 被封。

## 功能

- 🎬 **可靠视频下载**：自动去水印（优先 1080P / MP4）；1 字节 Range 预检后由浏览器从同源签名地址原生流式保存
- 🔗 **分享页**：抖音链接发微信打不开？一键生成 `/s/{短码}` 分享页，**微信里点开就能看**，不用装 App；`POST /api/shares` 只校验链接就立即返回本地分享地址，抖音内容由可恢复的持久化 worker 异步获取
- 🖼 **图集下载**：逐张原图浏览器直连下载
- ⚡ **播放直连、下载同源**：所有环境（含微信内）播放都优先直连抖音 CDN，直连失败自动落到 `/api/video/{vid}` 同源流式兜底；所有视频下载走同源转发，服务器不落地保存媒体
- 📋 **粘贴即用**：直接粘贴整段分享文案，自动提取短链；支持批量解析、结果导出 Excel
- 👤 **可选账号 + 免费配额**：匿名/登录用户每日免费额度采用原子预占防并发超用；注册、登录使用滑块验证码防机器人
- 📝 **文案提取（语音转文字，可选增强线路）**：后台配置 AnyToCopy 密钥并开启后，注册用户可把视频语音提取成完整文案（默认每天 5 次，原子预占/失败退款，同一视频全站缓存共享）；API 返回的无水印地址还可作为分享页第四条增强播放线路，播放优先级后台拖拽排序。默认完全关闭，不向第三方发送任何链接
- 💰 **开放 API**：`/api/v1/*` 按次计费（API Key + 余额预扣），异步批量任务 + 轮询查询，文档见 `/api-docs`，用户控制台 `/api-console`
- 🛡 **代理池防封**：socks5/socks5h/socks4/socks4a/http/https，轮换（轮询/随机/最少失败）+ 失败自动转移 + 强制代理防真实 IP 泄露
- ✈️ **内置机场加速**：后台粘贴机场订阅（vmess/trojan 等），自动下载 mihomo 内核落地为本地代理，多节点测速/切换全自动；随机端口 + 账号密码鉴权，**仅本项目可用、不影响服务器其他程序**，机场全挂时 fail-closed 绝不直连
- 🧩 **多格式代理解析**：`scheme://user:pass@host:port`、`host:port:user:pass`、`user:pass@host:port`、`host:port`，无前缀默认按 socks5；入库前可预览规范化结果
- ❤️ **后台健康检查**：定时并发测速（出口 IP + 抖音可达性）、连续失败自动禁用、恢复自愈
- 🔀 **UA 轮换 + Referer 伪装**：降低请求指纹一致性
- 🔐 **管理后台**：密码鉴权，代理管理、API Key 充值、用户管理、数据分析、播放诊断与转发流量统计、分享内容下架
- 🌓 **明暗双主题**；🌍 **中英双语 + SEO**；⚠️ **完整报错**（链接失效 / 删除 / 私密 / 直播）

## 页面入口

| 路径 | 说明 |
|---|---|
| `/` | 下载器主页 |
| `/transcript` | 文案提取落地页（功能在首页结果卡） |
| `/api-docs` | 开放 API 文档 |
| `/api-console` | 用户 API 控制台（Key 管理、余额、日志） |
| `/admin_d` | 管理后台（隐藏入口，首页不暴露） |
| `/healthz` | 存活探针（部署健康检查用） |

### 异步创建分享页

```bash
curl -X POST http://127.0.0.1:3344/api/shares \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000' \
  -d '{"text":"复制打开抖音 https://v.douyin.com/xxxx/"}'
```

接口只在本地校验一条规范的 `v.douyin.com` 短链，完成占位页、配额预占与任务持久化后立即返回 `202 Accepted` 和 `/s/{sid}`。用 `GET /api/shares/{sid}` 查询 `pending / processing / ready / failed`；直接打开分享页也会自动轮询并在完成后刷新。完整契约见 `/api-docs`。

## 快速开始（本地）

```bash
./run.sh                       # 默认端口 3344，首次自动建 venv + 装依赖
PORT=8010 ./run.sh             # 换端口
ADMIN_PASSWORD=your-secret ./run.sh
HOST=0.0.0.0 ADMIN_PASSWORD=your-secret ./run.sh  # 明确允许局域网访问
```

手动方式：

```bash
pip install -r requirements.txt
ADMIN_PASSWORD=your-secret uvicorn server:app --host 0.0.0.0 --port 3344 --no-access-log
```

命令行版（不起服务，纯标准库）：

```bash
python3 douyin_dl.py "分享文案或短链" [输出目录]
```

## 部署到服务器

### 方式一：Docker（推荐）

```bash
git clone https://github.com/d100000/dy-download.git && cd dy-download
docker build -t douyin-dl .

docker run -d --name douyin-dl \
  --restart unless-stopped \
  -p 127.0.0.1:3344:8000 \
  -e ADMIN_PASSWORD=换成强密码 \
  -e TRUST_PROXY=1 \
  -e PUBLIC_ORIGIN=https://your-domain.com \
  -e APP_SECRET=$(openssl rand -hex 32) \
  -v /srv/douyin-dl/data:/data \
  douyin-dl
```

要点：

- `-v .../data:/data` **必须挂载**：SQLite 数据库（用户/计费/分享数据）与代理池配置都在这里，不挂载则容器重建后全部丢失。
- `-p 127.0.0.1:3344:8000` 只绑本机回环，由 Nginx 对外反代（见下），不要把 8000 直接暴露公网。
- `TRUST_PROXY=1` 表示部署在反代之后：此时才采信 `X-Forwarded-For` 拿真实客户端 IP（限频、防薅、日志都依赖它），并自动给会话 Cookie 加 `Secure`。**没有反代时千万别开**，否则客户端可伪造头绕过所有基于 IP 的风控。
- `PUBLIC_ORIGIN` 必须填写最终公网 HTTPS origin；否则反代下 API 返回的 `share_url` 与二维码可能指向内网监听地址。

### 方式二：systemd + venv（裸机）

```bash
git clone https://github.com/d100000/dy-download.git /srv/douyin-dl
cd /srv/douyin-dl
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

`/etc/systemd/system/douyin-dl.service`：

```ini
[Unit]
Description=douyin-dl
After=network.target

[Service]
WorkingDirectory=/srv/douyin-dl
Environment=ADMIN_PASSWORD=换成强密码
Environment=TRUST_PROXY=1
Environment=PUBLIC_ORIGIN=https://your-domain.com
Environment=APP_SECRET=换成至少32字节的随机长字符串
ExecStart=/srv/douyin-dl/.venv/bin/uvicorn server:app --host 127.0.0.1 --port 3344 --no-access-log
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now douyin-dl
curl -s http://127.0.0.1:3344/healthz    # 验证存活
```

### Nginx 反向代理 + HTTPS

```nginx
# 放在 nginx.conf 的 http {} 内；应用层仍会做签名、限频和并发限制，
# 这里再增加一层共享限流，避免多进程/多实例绕过内存计数。
limit_req_zone  $binary_remote_addr zone=douyin_media_req:10m rate=2r/s;
limit_conn_zone $binary_remote_addr zone=douyin_media_conn:10m;

server {
    listen 443 ssl http2;
    server_name your-domain.com;
    # 默认不保存原始 IP、UA、Referer 或含短时媒体签名的完整请求地址。
    access_log off;
    error_log /var/log/nginx/douyin-dl-error.log crit;
    # 证书可用 certbot 签发：certbot --nginx -d your-domain.com

    # 视频播放/下载需要透传 Range，关闭 Nginx 响应缓冲，避免大文件先缓存后返回
    location /api/video/ {
        limit_req zone=douyin_media_req burst=20 nodelay;
        limit_conn douyin_media_conn 6;
        proxy_pass http://127.0.0.1:3344;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 300s;
        proxy_set_header Host $host;
        # 覆盖而非追加，防止客户端预置左侧 XFF 绕过 IP 限制。
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location / {
        proxy_pass http://127.0.0.1:3344;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

若 Nginx 前面还有 Cloudflare/CDN，先用 Nginx `real_ip` 模块把 `$remote_addr`
还原为真实客户端地址，再设置 `TRUST_PROXY_HOPS`；不要直接信任公网传入的 XFF。

分享域名（`SHARE_DOMAINS` 里的每个域名）各自加一个同样配置的 `server` 块，反代到同一个服务即可。

### 环境变量一览

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADMIN_PASSWORD` | `douyin-admin` | 管理后台密码，**生产必改** |
| `HOST` | `127.0.0.1` | 仅 `run.sh` 使用；需局域网监听时在改强密码后显式设 `0.0.0.0` |
| `DATA_DIR` | `data` | 数据目录（SQLite + 代理池配置） |
| `TRUST_PROXY` | 关 | 在 Nginx/Cloudflare 等反代后设 `1`，采信 XFF 并给 Cookie 加 Secure |
| `TRUST_PROXY_HOPS` | `1` | 从 XFF 右侧计算客户端地址时跨过的可信代理层数 |
| `COOKIE_SECURE` | 跟随 `TRUST_PROXY` | 单独控制会话 Cookie 的 Secure 标记 |
| `APP_SECRET` | 自动生成到 `data/.app-secret` | 标识摘要、媒体令牌与验证码签名的稳定密钥；可在生产环境显式设置，并应随 `data/` 一起备份 |
| `CAPTCHA_SECRET` | 跟随 `APP_SECRET` | 旧部署兼容项；只需单独覆盖验证码签名时设置 |
| `FREE_ANON_DAILY` | `3` | 匿名用户每日免费解析次数 |
| `FREE_USER_DAILY` | `10` | 登录用户每日免费解析次数 |
| `QUOTA_RESERVATION_TTL` | `3600` | 崩溃后未结算网页额度预占的自动退款等待秒数 |
| `NEW_KEY_BALANCE` | `100` | 新 API Key 试用余额（单位：分） |
| `API_JOB_WORKERS` | `2` | 单进程内持久化 API 任务 worker 数（1–8） |
| `API_JOB_LEASE_SECONDS` | `600` | API 任务项的数据库租约时长 |
| `API_JOB_HEARTBEAT_SECONDS` | `30` | 执行中任务租约续期间隔 |
| `MEDIA_TOKEN_TTL` | `43200` | 签名媒体地址有效秒数 |
| `MEDIA_REQUESTS_PER_MIN` | `120` | 单 IP 每分钟媒体请求上限 |
| `MEDIA_MAX_CONCURRENT` | `6` | 单 IP 同时播放/下载的媒体流上限 |
| `MEDIA_RESUME_MAX_ATTEMPTS` | `64` | 单个媒体流最多自动续传次数（限制 1–256） |
| `MEDIA_RESUME_MAX_SECONDS` | `3600` | 首次断流后允许自动续传的总时长秒数（限制 30–7200） |
| `MEDIA_RESUME_MAX_FAILURES` | `8` | 同一偏移连续续传失败上限（限制 2–16） |
| `DATA_RETENTION_DAYS` | `30` | 访问/解析/播放事件及已完成 API 任务明细的保留天数；强制限制在 1–30 天 |
| `PUBLIC_ORIGIN` | 空 | 生产环境固定公开 origin，如 `https://www.example.com`；用于防止 Host 头污染 canonical 与返回链接 |
| `SHARE_DOMAINS` | 空 | 分享页专用域名池，逗号分隔，如 `https://s1.example.com,https://s2.example.com` |
| `SHARE_TTL_ANON_DAYS` | `7` | 匿名用户分享链接有效期（天） |
| `SHARE_TTL_USER_DAYS` | `30` | 登录用户分享链接有效期（天） |
| `SHARE_PARSE_WORKERS` | `2` | 单进程异步分享解析 worker 数（1–4） |
| `SHARE_PARSE_LEASE_SECONDS` | `180` | 分享解析任务的 SQLite 租约时长 |
| `SHARE_PARSE_MAX_ATTEMPTS` | `4` | 上游瞬时失败时最多尝试次数（1–8） |
| `SHARE_PARSE_DEADLINE_SECONDS` | `900` | 从创建起允许后台获取内容的总时长 |
| `SHARE_PARSE_QUEUE_MAX` | `200` | 全局 pending/processing 分享任务上限 |
| `SHARE_PARSE_GLOBAL_PER_MINUTE` | `120` | 所有用户合计每分钟最多接受的异步分享任务数 |
| `SHARE_PARSE_GLOBAL_PER_HOUR` | `3000` | 所有用户合计每小时最多接受的异步分享任务数 |
| `SHARE_PARSE_IP_PER_HOUR` | `60` | 单 IP 每小时异步分享提交上限（持久化且删除不重置） |
| `ASYNC_SHARE_BODY_MAX` | `8192` | `POST /api/shares` 在 JSON 解析前允许的 HTTP 请求体字节数 |
| `MIHOMO_VERSION` | `v1.18.10` | 内置机场加速用的 mihomo 内核版本 |
| `MIHOMO_DL_BASE` | GitHub releases | 内核下载源；国内服务器连不上 GitHub 时换成镜像地址 |
| `MIHOMO_OFF` | 关 | 设 `1` 彻底禁用内置机场功能（即使后台配了订阅也不启动） |

API 单价与微信公众号 AppID/Secret 不走环境变量，在管理后台里配置（存数据库）。

### 部署注意事项

- **必须单 worker 运行**（默认即是）。会话、验证码、限频计数都在进程内存里，多 worker 互不相认会导致登录/验证码随机失效。也因此**重启会掉登录态**，属预期行为。
- **上线后第一件事**：打开 `/admin_d` 登录，在「代理」里添加代理 IP。默认强制走代理，**没有可用代理时解析会直接 503**——这是防止服务器真实 IP 被抖音封禁的底线设计，不要关。
- **备份**：定期备份整个 `data/` 目录即可（`app.db`、`config.json`、`.app-secret`）；旧备份也可能含用户/API 明细，应加密并按不超过 30 天的周期轮换删除。
- **分享域名与主站域名物理隔离**：分享域名被微信封是日常消耗品，主站域名被牵连是灾难。`SHARE_DOMAINS` 里不要填主站域名。
- 监控可用 `/healthz` 做存活探针；微信播放默认优先同源代理，所有视频下载也经同源流式线路，因此应按微信内/外分别观察后台播放诊断，并按实际下载量规划服务器带宽。

## 分享页（微信可直开）

解析成功后点「🔗 生成分享页」，得到一个可直接发微信的短链。设计要点见 [docs/分享页功能规划.md](docs/分享页功能规划.md)：

- **服务器不落地任何媒体**：只存元数据 + `video_id`，播放地址每次重拼；图集直链过期时按作品 ID 惰性刷新
- **播放线路统一直连优先**：微信内外均为 `dy1 → dy2 → proxy`（微信内直连看门狗缩短为 4 秒，最坏 8 秒落到同源代理）；全部失败才引导「用浏览器打开」。每条线路的尝试/成败都上报后台「播放尝试日志」
- **合规默认值**：`noindex` 不被搜索引擎收录、有有效期（匿名 7 天 / 登录 30 天）、强制署名并回链原作者、页脚常驻侵权投诉入口，后台可一键下架
- **抗封**：分享域名与主站物理隔离，`SHARE_DOMAINS` 配置域名池并轮换；短码与域名解耦，某域名被封时后台停用即可，存量链接换域名照常打开；海报图作为链接被封时的传播兜底

微信分享卡片可选接入 JS-SDK（后台「分享页 → 微信分享卡片」填认证服务号 AppID/AppSecret，需域名已备案）；不配置则使用微信默认抓取的卡片。

## 代理后台

1. 打开隐藏入口 `/admin_d`，用 `ADMIN_PASSWORD` 登录（首页不暴露该入口）。
2. 「添加代理」粘贴一个或多个代理（换行/逗号/分号分隔，自动去重），兼容多种格式：
   ```
   socks5://user:pass@1.2.3.4:1080     # 完整写法
   1.2.3.4:1080:user:pass              # 代理商常见导出格式
   user:pass@1.2.3.4:8080              # 省略协议
   1.2.3.4:1080                        # 裸 host:port，按「默认协议」解析
   ```
   点「预览解析」可在入库前看到规范化结果；右侧「默认协议」默认 **socks5**。
3. 点「测速」确认出口 IP、延迟与**抖音可达性**。之后**所有解析、封面、视频、图集请求都会按策略轮换走这些代理**，失败自动转移到下一个。
4. 列表支持多选与批量 删除/启用/禁用/测速，可按状态、协议筛选并搜索地址/出口 IP；标记「托管」的条目由「代理设置 → 内置机场加速」统一管理，不参与批量增删启停。
5. 「代理设置」页签可配：强制走代理、轮换策略（轮询/随机/最少失败）、每请求重试数、自动健康检查与间隔、连续失败自动禁用阈值。后台会定时测速，坏代理自动禁用、恢复后自愈。

**如何验证代理真的生效**：仓库自带一个本地测试代理 `tools/testproxy.py`：
```bash
python3 tools/testproxy.py 8899          # 另开一个终端，会打印每条经过的请求
# 后台添加 http://127.0.0.1:8899 → 解析任意视频 → 观察该终端逐行打印 CONNECT 日志
```

## 技术原理与反爬分析

短链 302 解析 → 抖音 H5 分享页 `_ROUTER_DATA` 元数据提取 → `playwm→play` 去水印。

**分工**：解析（短链 + 分享页）在服务器完成、走代理防封；普通浏览器播放优先直连抖音（浏览器自行跟随 302 到 CDN）。由于抖音播放接口的首跳 302 不提供 CORS，视频下载统一使用带 `Content-Disposition` 的 `/api/video/{vid}?exp=...&sig=...&dl=1&name=...` 同源流式响应：前端先发 1 字节 Range 验证线路，再交给浏览器原生下载，不把完整视频堆进页面内存。媒体授权带 HMAC 有效期签名、单 IP 限频与并发限制，只允许单段 Range，服务器只转发字节、不落地文件；主播放域名或代理出口遇到 401/403/5xx 时会切换域名与代理。播放线路按环境调整：普通浏览器先试两个抖音域名再走代理，微信 WebView 则优先同源代理，避免直连域名被拦后等待约 14 秒并丢失播放手势。每条线路的成败都进后台「播放诊断」看板。抖音的限制机制与本项目对策详见 [docs/产品文档.md](docs/产品文档.md)。

## 隐私与数据边界

- **不保存媒体文件**：服务器不落地保存视频或图片；兼容线路仅流式转发媒体字节。分享页会按有效期保存标题、封面、作者、作品 ID 等必要元数据。
- **站内账号可选**：基础解析无需注册，也无需登录抖音。注册本站账号时会保存邮箱与加盐密码哈希；账号资料随账号保留。
- **不做浏览器指纹**：前端仅生成一个 30 天有效的随机第一方匿名 ID，并主动清除旧版 `dyfp`。服务端把 IP 与该 ID 转换为按用途隔离的摘要，用于免费额度与防滥用。
- **明细保留期最多设为 30 天**：用途化标识摘要、粗粒度浏览器信息、访问/解析/播放事件，以及 API 任务与结果按配置保留 1–30 天；到期数据由每 5 分钟运行的清理任务删除。
- **默认最小化基础设施日志**：项目提供的启动脚本、Docker、systemd 与 Nginx 示例均关闭 Uvicorn/Nginx access log，Nginx 示例只保留 critical 级错误。运行时错误日志仍可能带请求上下文；若自行启用云平台/反向代理日志，应只记 `$uri` 等必要字段、避免 query/原始 IP/完整 UA/Referer，并设置不超过 30 天的清理策略。这类外部日志不由应用数据库清理。
- **投诉联系方式可选**：侵权投诉可留联系方式，仅用于跟进处理；保留期最多设为 30 天，到期后进入同一 5 分钟清理周期。
- **直连会向抖音发出请求**：浏览器直连播放视频或加载/下载图片时，抖音会接收到请求方的 IP 等网络信息与浏览器信息；视频下载及同源兼容播放则由本站服务器代为请求。

## 更新日志

| 版本 | 日期 | 内容 |
|---|---|---|
| v1.14.0 | 2026-08-09 | 新增异步分享页 API：`POST /api/shares` 只做本地短链校验，在同一 SQLite 事务中完成占位页、持久限频、幂等、配额预占与任务入队后立即返回 `202 + /s/{sid}`；后台通过非 daemon worker、SQLite 租约/CAS、心跳、崩溃接管与有界重试获取元数据，成功原子结算、失败原子退款。新增 `GET /api/shares/{sid}` 最小状态；分享页支持 pending/processing/failed，ready 后自动刷新，并提醒完成前不要转发到微信以避免占位 OG 卡片被缓存。只保存规范化短链至任务终态，不保存整段分享文案或媒体字节；公开错误仅含稳定错误码。复制分享链接改为抖音风格文案（【标题】@作者 的抖音作品 + 一句引导 + 链接，首页生成弹窗/我的分享/分享页内三处统一）；重写 `/api-docs`：完整可执行的 curl 请求示例、100 条上限与 Idempotency-Key 等真实约束、任务状态机与结果字段说明表、Python 调用示例、错误码补 409 |
| v1.13.2 | 2026-08-08 | 修复分享页竖屏视频在移动端被 `70vh` 限高后向左收缩、右侧出现单边空栏的问题：将高度上限按视频宽高比换算为显式最大宽度并水平居中，同时分别约束横竖屏比例，优先使用动态视口单位 `dvh`、旧版 WebView 回退到 `vh` |
| v1.13.1 | 2026-08-07 | 修复管理后台整页 JS 失效（点击登录无反应）：v1.13.0 新增的播放日志分页声明了 `const PL`，与代理列表原有的同名全局变量冲突，导致整个 `<script>` 解析失败、登录表单等所有绑定全部失效；播放日志状态变量改名为 `PLL` |
| v1.13.0 | 2026-08-07 | 新增管理后台「播放日志」页签：逐条记录每次播放的浏览器环境（微信内/外 + 粗粒度 UA）、线路、尝试/成功/失败、耗时，以及**失败后链上下一条重试的线路**（`share_events` 新增 `next_src` 列，PRAGMA 就地补列；只记线路名不记带签名的媒体地址）；服务端分页（`GET /api/admin/play-logs?page=&size=&result=&wechat=&sid=`，总数/页数/上下页/每页 20-100 条），支持按结果、微信内外、分享页短码筛选，解决日志量大时一次拉取过慢的问题 |
| v1.12.1 | 2026-08-07 | 页面缓存治理：`/api-docs`、`/transcript`、`/api-console`、`/admin_d` 四个此前无缓存头的页面统一加 `Cache-Control: no-cache`（每次回源校验，ETag/Last-Modified 未变返回 304），部署新版前端后用户无需强刷即生效；模板页 `<head>` 增加 `app-version` meta，view-source 可直接确认部署版本；`atc_url_ttl` 按实测校准为 3600 秒 |
| v1.12.0 | 2026-08-07 | 新增可选的 AnyToCopy 增强线路（后台「增强线路」页签配置密钥，默认关闭）：① 分享页播放地址增强——API 返回的无水印地址作为第四条播放线路，带有效期判断、过期自动落链并惰性重新获取，播放线路优先级可在后台拖拽排序（`share_play_priority`）；② 注册用户「文案提取（语音转文字）」——结果卡开关提交异步任务、前端轮询展示，独立每日限额（默认 5 次，原子预占/失败退款），同一视频全站缓存共享、命中不扣次。ATC 全部请求经 `atc_jobs` 队列表串行提交（对方并发上限 5），永不进入同步主解析，故障自动降级。首页 UI 精简：删除与 trust 卡重复的 6 个信任徽章与 3 个步骤条，输入区 4 按钮减为 2 个（清空改为浮动 ×、上传文件降为文字链接），结果卡删「新窗口播放」，批量行内删「播放」，登录后导航改为邮箱下拉菜单；修复 `.fallback-hint` 缺样式。注册引导：匿名点文案开关直接弹登录且成功后自动继续、429 配额用尽附登录链接、第 2 次解析成功后可关闭的轻提示；分享页 CTA 强化、海报页脚带域名、「我的分享」播放 ≥10 显示 🔥；滑块验证码通过/失败内存计数并入后台状态。新增 `/transcript` 文案提取落地页（独立 SEO + FAQ JSON-LD），首页 FAQ 与隐私披露同步第三方边界（开启增强线路才会向 AnyToCopy 提交链接），sitemap/llms.txt 同步 |
| v1.11.0 | 2026-08-05 | 微信内播放改回直连优先：全环境统一 `dy1 → dy2 → proxy`，抖音域名直连不花服务器带宽、同源代理只做兜底（v1.10.1 全页 no-referrer 已解决直连 403；微信内直连看门狗缩短为 4 秒，最坏 8 秒落到代理线路）；首页微信内同样直连优先，`onerror` 自动切同源代理并向微信内用户开放手动「改用服务器代理播放」入口；分享页每条线路开始尝试即上报 `play_try`，后台「最近播放失败明细」升级为「播放尝试日志」（尝试/成功/失败全量明细）；新增转发流量统计：`/api/video` 流式转发按 天 × 用途（播放兜底 / 视频下载）聚合请求数与字节数（新表 `media_traffic`，内存累加、sweeper 定期落库并在关服时冲刷，无个人标识、保留 1 年），后台「分享页」页签新增「转发流量统计」看板（`GET /api/admin/traffic-stats?days=`） |
| v1.10.1 | 2026-08-05 | 修复抖音 CDN 新增 Referer 防盗链导致浏览器直连媒体 403（openresty）：实测同一 douyinvod 地址无 Referer 返回 206、带第三方站点 Referer 返回 403、带 douyin.com Referer 返回 206。首页、分享页与 OSS 版的 `<head>` 统一加 `<meta name="referrer" content="no-referrer">`（`<video>` 不支持 `referrerpolicy` 属性，文档级策略才能覆盖媒体请求），直连播放、图集图片、新窗口播放等浏览器直连入口全部不再带 Referer 出站；同源 `/api/video` 下载/播放线路服务器侧本就以 douyin.com Referer 请求，不受影响 |
| v1.10.0 | 2026-08-04 | 管理后台代理管理改版：代理列表独立成 Tab，支持多选与批量 删除/启用/禁用/测速（新增 `POST /api/admin/proxies/batch`）、按 状态/协议 筛选与 地址/出口 IP/备注 搜索、延迟与成功率排序、分页；mihomo 托管条目显示「托管」徽标且不参与批量增删启停；「代理策略与安全」（按出站安全/轮换解析/健康检查分组）与 mihomo 移入「代理设置」Tab，开放 API 密钥与计费独立成「API 密钥」Tab |
| v1.9.3 | 2026-07-31 | 修复代理长连接中途 EOF、超时或 `IncompleteRead` 导致视频只下载一小段：主版与 OSS 都会保留已收到的 partial 字节，并从精确绝对偏移用 Range 自动续传；每次重连严格校验 `206`、`Content-Range`、总长度、媒体类型与最终域名，保持客户端 `200/206` 和长度响应头一致，动态 finalizer 确保客户端断开时关闭当前上游并释放并发租约，并用总次数、总时限和连续失败三重预算防止无界重连。媒体 tunnel/HTTP 403 不再误判为出口永久封禁。播放与下载线路由固定 720P 提升为优先 1080P，避免页面显示 1920×1080 而实际下载 1280×720 |
| v1.9.2 | 2026-07-30 | 修复点击下载跳转抖音后显示 502：视频下载不再先请求会被首跳 302 CORS 拦截的抖音地址，也彻底移除 `window.open` 外跳兜底；改为 1 字节 Range 预检同源签名流，再交给隐藏下载目标原生流式保存，既让错误留在原页提示，也避免完整视频进入页面内存；`/api/video` 会校验媒体类型与最终域名，在 `aweme.snssdk.com`、当前代理出口或响应内容异常时轮换代理并切换 `iesdouyin.com`，完整版与 OSS 同步；首页与分享页禁止缓存内联脚本，分享页按 User-Agent 区分缓存并自动修复缺失 `vid` 的旧视频记录，避免部署后继续执行旧下载逻辑 |
| v1.9.1 | 2026-07-30 | 修复微信/X5 已经起播却仍误弹“在浏览器中打开观看”：分享页播放器增加单会话复用与线路代次校验，旧线路迟到的错误/超时不再推进新线路；把 `canplay`、`timeupdate` 和 `play()` 成功纳入起播确认，并在错误、超时、最终失败前复核实际播放状态；自动探测失败改为非阻断提示，浏览器引导遮罩只在用户明确点击下载或分享时出现 |
| v1.9.0 | 2026-07-30 | 安全与可靠性收口：修复首页缺失 `apiTabBtn`；代理失败响应不再泄露带凭据 URL；移除未使用的 `/api/media`，`/api/video` 改用限时 HMAC 授权、单 IP 限频/并发和单段 Range；网页额度改为持久化原子预占与失败退款；开放 API 改为整批预授权、逐项幂等结算、数据库租约恢复与账本对账，不再依赖 daemon 任务线程，API Key 仅允许请求头传递且管理操作不再把密钥放进 URL；前端移除 Canvas/硬件指纹与未使用的本地历史，使用 30 天随机第一方匿名 ID，旧敏感明细迁移清理，数据保留期强制为 1–30 天并每 5 分钟清理到期项；默认关闭访问日志，中英文页面、SEO、分享图和文档同步真实数据边界 |
| v1.8.0 | 2026-07-30 | 修复视频无法下载与微信内无法播放：视频结果新增同源 `download_url`，前端仍优先浏览器直连，遇到 CORS/重定向限制后自动切换 `/api/video/{vid}?dl=1&name=...` 流式附件下载，覆盖单条、批量及分享页且不落地文件；微信内播放顺序改为 `proxy → dy1 → dy2`，首页与分享页补齐 `playsinline`/X5 属性，普通浏览器继续 `dy1 → dy2 → proxy` 节省带宽；Nginx 示例补 Range 流式配置；同时修复首页缺少 `apiTabBtn` 导致初始化脚本中断的问题 |
| v1.7.0 | 2026-07-27 | 微信分享卡片修复：① 卡片图改用 `_card_cover()` 归一化——抖音封面 `p26-sign.douyinpic.com/....webp?x-signature=…` 去 `-sign` 主机 + 换 `.jpeg` 扩展名后，抖音会返回<b>无签名、不过期的 JPEG</b>（原 webp 微信缩略图支持不稳定、签名约 14 天过期，过期后存量分享页全变无图卡片），仅对白名单内抖音图床生效、转换失败原样返回；分享页 `og:image` 兜底由 `og.svg` 改为 `og.png`（微信不渲染 SVG），并补 `og:image:type`/`:alt`；JS-SDK 卡片图与海报封面同步改用 `card_cover`。② 修正「粘贴链接会自动变卡片」的错误说法——微信不展开粘贴的网址，只有在微信内打开页面后点右上角 ··· 转发才出卡片；首页分享成功弹窗、分享页「发送给好友」引导、FAQ（中英 + JSON-LD FAQPage）、`llms.txt` 四处文案统一更正，并在「发送给好友」引导里隐藏「复制本页链接」按钮（避免把用户引向只会得到纯链接的路径） |
| v1.6.0 | 2026-07-27 | 播放优先走抖音域名 + 后台播放诊断：分享页播放链路由「直连 → 服务器代理」扩为 <b>dy1 `aweme.snssdk.com` → dy2 `www.iesdouyin.com` → proxy 服务器兜底 → 微信引导</b>，微信内两个抖音域名的可达性因机型/内核而异，多试一个免费域名可显著减少走服务器带宽的比例；每条线路的成功/失败都上报埋点（`play_ok`/`play_fail`，带线路、阶段 error/timeout/giveup、耗时与 media error code），`share_events` 表新增 `source`/`stage`/`detail`/`ms` 四列（启动时 `PRAGMA table_info` 就地补列，老库兼容）；后台「分享页」页签新增「播放诊断」看板——按 微信内/外 × 线路 看成功率与平均起播耗时、微信内播放失败的作品排行、最近失败明细（带 UA 定位机型内核） |
| v1.5.0 | 2026-07-27 | 修复分享海报在微信内无法保存/转发：canvas 导出由 `toBlob`+`createObjectURL` 改为 `toDataURL()`，微信 WebView 不认 `blob:` 图片（长按无「保存图片/发送给朋友」），改用 `data:` URI 后可正常长按保存与转发；站点定位升级为「下载 + 分享」双卖点：首屏新增分享副标题与「↗ 一键分享」徽章、信任区新增「一键分享给好友」卡片、步骤 3 改为「下载原片 · 或生成分享页发给好友」；SEO/GEO 全面补充分享语义——zh/en 标题描述关键词新增「抖音视频分享/怎么分享到微信/免 App 观看」等词，FAQ 新增 2 条分享问答（页面与 JSON-LD FAQPage 同步共 7 条）、HowTo 第三步与 `featureList` 补分享能力、`llms.txt` 改写为下载+分享双主线 |
| v1.4.0 | 2026-07-27 | 管理后台登录防爆破：单 IP 在 15 分钟内密码错误达 5 次即临时锁定（返回 429、剩余次数提示，成功登录清零，内存计数随 `_sweeper` 清理）；分享页新增「↗ 分享给好友」按钮——微信内引导用户点右上角 ··· 转发（发出去是带封面标题的卡片而非纯地址，卡片文案由 JS-SDK `setupWxShare` 定制），微信外调用 Web Share API、不支持则退回复制链接；微信卡片图兜底由 `og.svg` 改为 `og.png`（微信不渲染 SVG） |
| v1.3.0 | 2026-07-27 | SEO/GEO 优化：社交卡片改用位图 `og.png`（微信/多数抓取器不渲染 SVG，`og.svg` 保留兜底），补 `og:image:type`/`:alt`；JSON-LD `@graph` 新增 `Organization`+`WebSite` 节点并与 `WebApplication` 交叉引用；新增面向大模型的 `/llms.txt`，`robots.txt` 显式放行 GPTBot/PerplexityBot/ClaudeBot 等 AI 抓取器并声明 `LLM:`；`sitemap.xml` 补 `lastmod`；补 `author`/`application-name`/苹果 PWA 头、明/暗双 `theme-color`、抖音接口 `preconnect`/`dns-prefetch`；图集结果图补 `loading=lazy`/`decoding=async` |
| v1.2.0 | 2026-07-27 | 分享链接支持在后台设置「主分享域名」（即时生效、无需重启），所有新链接固定落在指定的可微信打开域名上；优化微信分享卡片：卡片标题改为抖音文案原文、作者进描述行，`_share_head` 补 `og:site_name`/`twitter:description`，JS-SDK 分享文案与网页抓取卡片保持一致；`/api/wx/jssdk` 签名白名单纳入主域名，反代头缺失时仍可签名 |
| v1.1.0 | 2026-07-27 | 内置机场加速：后台一键接入机场订阅，自动下载 mihomo 内核落地为本地代理并注入代理池；随机端口+鉴权+仅绑 127.0.0.1 做到项目独占、不影响同机其他程序，机场无可用节点时 fail-closed 绝不退回直连；详见 [docs/机场代理接入.md](docs/机场代理接入.md) |
| v1.0.0 | 2026-07-27 | 引入版本号机制（`APP_VERSION`，`/healthz` 可查）；README 新增服务器部署章节（Docker / systemd / Nginx / 环境变量一览）；修正 `run.sh` 打印的后台入口为 `/admin_d` |
| v0.5 | 2026-07-26 | 分享页：抖音链接一键变成微信可直开的作品页（域名池、三级降级播放、海报、JS-SDK）；补入产品与商业化文档 |
| v0.4 | 2026-07-18 | 用户体系 + 异步计费开放 API + 数据分析后台 + 防薅羊毛（滑块验证码/配额）+ 多语言/SEO + 全站加固 |
| v0.3 | 2026-07-18 | 开放 API 商业化 + 批量解析 + 首页视觉/性能优化 |
| v0.2 | 2026-07-17 | 浏览器直连播放/下载 + 作者交互 + 全代理化 + 封禁检测 |
| v0.1 | 2026-07-17 | 初版：Web 服务 + 代理池管理后台 |

> v0.x 为追溯编号（对应 git 提交历史）；自 v1.0.0 起每次更新都会在此登记并同步 `server.py` 的 `APP_VERSION`。

## 免责声明

仅供个人学习、收藏及获得授权的素材备份使用。内容版权归原作者所有，未经授权请勿二次发布或商用。本站不保存视频或图片文件；站内账号为可选，数据处理与保留范围见上方「隐私与数据边界」。代理仅用于分散请求来源，请遵守当地法律与抖音用户协议。
