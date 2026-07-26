# 抖音无水印下载器

**当前版本：v1.2.0**（线上版本可通过 `GET /healthz` 的 `version` 字段确认）

免登录、免签名的抖音视频 / 图集下载工具。粘贴分享链接，在线预览并下载无水印原片。内置**代理 IP 池 + 管理后台 + 用户体系 + 按次计费开放 API**，所有出站请求轮换走代理，防止服务器 IP 被封。

## 功能

- 🎬 **视频下载**：自动去水印（720P / MP4），在线播放（可拖动进度条）
- 🔗 **分享页**：抖音链接发微信打不开？一键生成 `/s/{短码}` 分享页，**微信里点开就能看**，不用装 App；带二维码与分享海报、访问数据统计，默认 noindex 且强制署名回链原作者
- 🖼 **图集下载**：逐张原图浏览器直连下载
- ⚡ **省流不暴露 IP**：解析信息才走服务器（+代理），**播放与下载由用户浏览器直连抖音 CDN**，视频字节不经过服务器；直连失败可一键切服务器代理兜底
- 📋 **粘贴即用**：直接粘贴整段分享文案，自动提取短链；支持批量解析、结果导出 Excel
- 👤 **用户体系 + 免费配额**：匿名/登录用户每日免费额度，自研滑块验证码防批量薅取
- 💰 **开放 API**：`/api/v1/*` 按次计费（API Key + 余额预扣），异步批量任务 + 轮询查询，文档见 `/api-docs`，用户控制台 `/api-console`
- 🛡 **代理池防封**：socks5/socks5h/socks4/socks4a/http/https，轮换（轮询/随机/最少失败）+ 失败自动转移 + 强制代理防真实 IP 泄露
- ✈️ **内置机场加速**：后台粘贴机场订阅（vmess/trojan 等），自动下载 mihomo 内核落地为本地代理，多节点测速/切换全自动；随机端口 + 账号密码鉴权，**仅本项目可用、不影响服务器其他程序**，机场全挂时 fail-closed 绝不直连
- 🧩 **多格式代理解析**：`scheme://user:pass@host:port`、`host:port:user:pass`、`user:pass@host:port`、`host:port`，无前缀默认按 socks5；入库前可预览规范化结果
- ❤️ **后台健康检查**：定时并发测速（出口 IP + 抖音可达性）、连续失败自动禁用、恢复自愈
- 🔀 **UA 轮换 + Referer 伪装**：降低请求指纹一致性
- 🔐 **管理后台**：密码鉴权，代理管理、API Key 充值、用户管理、数据分析、分享内容下架
- 🌓 **明暗双主题**；🌍 **中英双语 + SEO**；⚠️ **完整报错**（链接失效 / 删除 / 私密 / 直播）

## 页面入口

| 路径 | 说明 |
|---|---|
| `/` | 下载器主页 |
| `/api-docs` | 开放 API 文档 |
| `/api-console` | 用户 API 控制台（Key 管理、余额、日志） |
| `/admin_d` | 管理后台（隐藏入口，首页不暴露） |
| `/healthz` | 存活探针（部署健康检查用） |

## 快速开始（本地）

```bash
./run.sh                       # 默认端口 3344，首次自动建 venv + 装依赖
PORT=8010 ./run.sh             # 换端口
ADMIN_PASSWORD=your-secret ./run.sh
```

手动方式：

```bash
pip install -r requirements.txt
ADMIN_PASSWORD=your-secret uvicorn server:app --host 0.0.0.0 --port 3344
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
  -e CAPTCHA_SECRET=$(openssl rand -hex 32) \
  -v /srv/douyin-dl/data:/data \
  douyin-dl
```

要点：

- `-v .../data:/data` **必须挂载**：SQLite 数据库（用户/计费/分享数据）与代理池配置都在这里，不挂载则容器重建后全部丢失。
- `-p 127.0.0.1:3344:8000` 只绑本机回环，由 Nginx 对外反代（见下），不要把 8000 直接暴露公网。
- `TRUST_PROXY=1` 表示部署在反代之后：此时才采信 `X-Forwarded-For` 拿真实客户端 IP（限频、防薅、日志都依赖它），并自动给会话 Cookie 加 `Secure`。**没有反代时千万别开**，否则客户端可伪造头绕过所有基于 IP 的风控。

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
Environment=CAPTCHA_SECRET=换成随机长字符串
ExecStart=/srv/douyin-dl/.venv/bin/uvicorn server:app --host 127.0.0.1 --port 3344
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
server {
    listen 443 ssl http2;
    server_name your-domain.com;
    # 证书可用 certbot 签发：certbot --nginx -d your-domain.com

    location / {
        proxy_pass http://127.0.0.1:3344;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

分享域名（`SHARE_DOMAINS` 里的每个域名）各自加一个同样配置的 `server` 块，反代到同一个服务即可。

### 环境变量一览

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADMIN_PASSWORD` | `douyin-admin` | 管理后台密码，**生产必改** |
| `DATA_DIR` | `data` | 数据目录（SQLite + 代理池配置） |
| `TRUST_PROXY` | 关 | 在 Nginx/Cloudflare 等反代后设 `1`，采信 XFF 并给 Cookie 加 Secure |
| `COOKIE_SECURE` | 跟随 `TRUST_PROXY` | 单独控制会话 Cookie 的 Secure 标记 |
| `CAPTCHA_SECRET` | 随机生成 | 验证码/令牌签名密钥；**生产建议固定**，否则重启后未消费的令牌全部失效 |
| `FREE_ANON_DAILY` | `3` | 匿名用户每日免费解析次数 |
| `FREE_USER_DAILY` | `10` | 登录用户每日免费解析次数 |
| `NEW_KEY_BALANCE` | `100` | 新 API Key 试用余额（单位：分） |
| `SHARE_DOMAINS` | 空 | 分享页专用域名池，逗号分隔，如 `https://s1.example.com,https://s2.example.com` |
| `SHARE_TTL_ANON_DAYS` | `7` | 匿名用户分享链接有效期（天） |
| `SHARE_TTL_USER_DAYS` | `30` | 登录用户分享链接有效期（天） |
| `MIHOMO_VERSION` | `v1.18.10` | 内置机场加速用的 mihomo 内核版本 |
| `MIHOMO_DL_BASE` | GitHub releases | 内核下载源；国内服务器连不上 GitHub 时换成镜像地址 |
| `MIHOMO_OFF` | 关 | 设 `1` 彻底禁用内置机场功能（即使后台配了订阅也不启动） |

API 单价与微信公众号 AppID/Secret 不走环境变量，在管理后台里配置（存数据库）。

### 部署注意事项

- **必须单 worker 运行**（默认即是）。会话、验证码、限频计数都在进程内存里，多 worker 互不相认会导致登录/验证码随机失效。也因此**重启会掉登录态**，属预期行为。
- **上线后第一件事**：打开 `/admin_d` 登录，在「代理」里添加代理 IP。默认强制走代理，**没有可用代理时解析会直接 503**——这是防止服务器真实 IP 被抖音封禁的底线设计，不要关。
- **备份**：定期备份整个 `data/` 目录即可（`app.db` + `config.json`）。
- **分享域名与主站域名物理隔离**：分享域名被微信封是日常消耗品，主站域名被牵连是灾难。`SHARE_DOMAINS` 里不要填主站域名。
- 监控可用 `/healthz` 做存活探针；服务器带宽异常时先看分享页 `fallback` 埋点（浏览器直连失败转服务器代理的比例）。

## 分享页（微信可直开）

解析成功后点「🔗 生成分享页」，得到一个可直接发微信的短链。设计要点见 [docs/分享页功能规划.md](docs/分享页功能规划.md)：

- **服务器不落地任何媒体**：只存元数据 + `video_id`，播放地址每次重拼；图集直链过期时按作品 ID 惰性刷新
- **播放三级降级**：浏览器直连抖音 CDN → 服务器代理兜底 → 微信内引导「用浏览器打开」
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
4. 「代理策略与安全」可配：强制走代理、轮换策略（轮询/随机/最少失败）、每请求重试数、自动健康检查与间隔、连续失败自动禁用阈值。后台会定时测速，坏代理自动禁用、恢复后自愈。

**如何验证代理真的生效**：仓库自带一个本地测试代理 `tools/testproxy.py`：
```bash
python3 tools/testproxy.py 8899          # 另开一个终端，会打印每条经过的请求
# 后台添加 http://127.0.0.1:8899 → 解析任意视频 → 观察该终端逐行打印 CONNECT 日志
```

## 技术原理与反爬分析

短链 302 解析 → 抖音 H5 分享页 `_ROUTER_DATA` 元数据提取 → `playwm→play` 去水印。

**分工**：解析（短链 + 分享页）在服务器完成、走代理防封；播放/下载的无水印地址交给用户浏览器直连抖音（浏览器自行跟随 302 到 CDN，按自身 IP 解析直链，实测该接口对桌面 UA 无 UA 均放行、CDN 返回 `Access-Control-Allow-Origin: *`）。浏览器直连失败时可切换 `/api/video/{vid}` 走服务器代理兜底。抖音的限制机制与本项目对策详见 [docs/产品文档.md](docs/产品文档.md)。

## 更新日志

| 版本 | 日期 | 内容 |
|---|---|---|
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

仅供个人学习、收藏及获得授权的素材备份使用。内容版权归原作者所有，未经授权请勿二次发布或商用。本工具不存储任何视频与账号数据；代理仅用于分散请求来源，请遵守当地法律与抖音用户协议。
