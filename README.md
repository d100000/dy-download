# 抖音无水印下载器

免登录、免签名的抖音视频 / 图集下载工具。粘贴分享链接，在线预览并下载无水印原片。内置**代理 IP 池 + 管理后台**，所有出站请求轮换走代理，防止服务器 IP 被封。

## 功能

- 🎬 **视频下载**：自动去水印（720P / MP4），支持在线播放（可拖动进度条）
- 🖼 **图集下载**：逐张原图下载 + 一键打包 ZIP
- 📋 **粘贴即用**：直接粘贴整段分享文案，自动提取短链；支持批量解析
- 🛡 **代理池防封**：socks5/socks5h/socks4/socks4a/http/https，轮换（轮询/随机/最少失败）+ 失败自动转移 + 强制代理防真实 IP 泄露
- 🧩 **多格式代理解析**：`scheme://user:pass@host:port`、`host:port:user:pass`、`user:pass@host:port`、`host:port`，无前缀默认按 socks5；入库前可预览规范化结果
- ❤️ **后台健康检查**：定时并发测速（出口 IP + 抖音可达性）、连续失败自动禁用、恢复自愈
- 🔀 **UA 轮换 + Referer 伪装**：降低请求指纹一致性
- 🔐 **管理后台**：密码鉴权，增删/启停/预览/单个或批量测速，策略即时配置，出站统计
- 🌓 **明暗双主题**；⚠️ **完整报错**（链接失效 / 删除 / 私密 / 直播）

## 运行

### 本地（推荐：一键脚本）

```bash
./run.sh                       # 默认端口 3344，首次自动建 venv + 装依赖
PORT=8010 ./run.sh             # 换端口
ADMIN_PASSWORD=your-secret ./run.sh
# 下载器：http://localhost:3344     管理后台：http://localhost:3344/admin
```

手动方式：

```bash
pip install -r requirements.txt
ADMIN_PASSWORD=your-secret uvicorn server:app --host 0.0.0.0 --port 3344
```

### Docker

```bash
docker build -t douyin-dl .
docker run -p 3344:8000 -e ADMIN_PASSWORD=your-secret -v $(pwd)/data:/data douyin-dl
# 访问 http://localhost:3344   （容器内部仍监听 8000）
```

`-v .../data:/data` 用于持久化代理池配置；`ADMIN_PASSWORD` 生产务必修改（默认 `douyin-admin`）。

### 命令行版（无需服务器）

```bash
python3 douyin_dl.py "分享文案或短链" [输出目录]
```

## 代理后台

1. 打开 `/admin`，用 `ADMIN_PASSWORD` 登录。
2. 「添加代理」粘贴一个或多个代理（换行/逗号/分号分隔，自动去重），兼容多种格式：
   ```
   socks5://user:pass@1.2.3.4:1080     # 完整写法
   1.2.3.4:1080:user:pass              # 代理商常见导出格式
   user:pass@1.2.3.4:8080              # 省略协议
   1.2.3.4:1080                        # 裸 host:port，按「默认协议」解析
   ```
   点「预览解析」可在入库前看到规范化结果；右侧「默认协议」默认 **socks5**（代理多为 socks5）。
3. 点「测速」确认出口 IP、延迟与**抖音可达性**。之后**所有解析、封面、视频、图集请求都会按策略轮换走这些代理**，失败自动转移到下一个。
4. 「代理策略与安全」可配：强制走代理、轮换策略（轮询/随机/最少失败）、每请求重试数、自动健康检查与间隔、连续失败自动禁用阈值。后台会定时测速，坏代理自动禁用、恢复后自愈。

**如何验证代理真的生效**：仓库自带一个本地测试代理 `tools/testproxy.py`：
```bash
python3 tools/testproxy.py 8899          # 另开一个终端，会打印每条经过的请求
# 后台添加 http://127.0.0.1:8899 → 解析任意视频 → 观察该终端逐行打印 CONNECT 日志
```

## 技术原理与反爬分析

短链 302 解析 → 抖音 H5 分享页 `_ROUTER_DATA` 元数据提取 → `playwm→play` 去水印 → 后端流式代理（移动端 UA + Referer + Range）供浏览器播放/下载。抖音的限制机制与本项目对策详见 [docs/产品文档.md](docs/产品文档.md)。

## 免责声明

仅供个人学习、收藏及获得授权的素材备份使用。内容版权归原作者所有，未经授权请勿二次发布或商用。本工具不存储任何视频与账号数据；代理仅用于分散请求来源，请遵守当地法律与抖音用户协议。
