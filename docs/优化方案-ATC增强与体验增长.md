# 优化方案：AnyToCopy 增强线路 + 首页体验精简 + 增长引导

> 状态：已全部实施（v1.12.0）；PM 建议 #3 的图集/批量落地页未做（见文末说明）
> 范围：server.py / static/index.html / static/share.html / static/admin.html / tests / README
> 制定日期：2026-08-07

---

## 0. 总目标与衡量指标

### 产品目标
1. **新增差异化能力**：接入 AnyToCopy 开放 API，提供"语音转文字文案提取"与"分享页增强播放线路"，不触碰现有同步主解析链路。
2. **首屏减负**：首页首屏可操作元素从 ~11 个降到 ~5 个，移动端到达输入框的垂直距离缩短约 1/3。
3. **跑通增长回路**：分享页回流（获客）→ 价值时刻注册引导（转化）→ 个人资产沉淀（留存）。

### 北极星指标（后台均可统计，无需新增埋点框架）
| 指标 | 现状基线 | 目标 |
|---|---|---|
| 分享页 CTA 点击率（`cta_clicks`/`views`） | 待记录 | 提升 ≥50% |
| 匿名→注册转化率（每日新注册 / 每日活跃匿名） | 待记录 | 建立基线后提升 ≥30% |
| 文案提取功能日使用人数 | 0（新功能） | 上线 4 周后 ≥ 日活注册用户的 15% |
| 播放成功率（`play_ok`/`play_try`） | ≥ 现状 | 不因引入 atc 线路下降超过 1 个百分点 |
| 移动端首屏到输入框距离 | 现状 | 缩短 ≥30% |

### 贯穿原则
- ATC 永不进入同步主解析（`/api/parse` 一行不改）；异步、分钟级延迟只放异步场景。
- 总开关默认关；开启后默认优先级 `dy1→dy2→atc→proxy`，与现状行为等价。
- ATC 故障/超额/密钥失效 → 自动熔断降级，主流程零影响。
- 不落地媒体字节；只存 URL 与文案元数据。
- 注册引导全部"可忽略"形态；基础功能永远免注册。

---

## 阶段一：ATC 增强线路基座

**目标**：密钥、开关、数据表、API 客户端、队列全部就绪，但功能对用户不可见。

### 执行路径

1. **配置项**（`app_settings` 表，后台即时生效）：

   | key | 默认 | 说明 |
   |---|---|---|
   | `atc_api_key` / `atc_api_secret` | 空 | Secret 保存后打码回显 |
   | `atc_base_url` | `https://api.anytocopy.com/vip/open-api/v1` | 可换网关 |
   | `atc_enabled` | `0` | 总开关 |
   | `atc_play_enhance` | `1` | API 地址是否纳入分享页播放链 |
   | `atc_transcript_enabled` | `1` | 是否开放文案提取 |
   | `atc_transcript_daily` | `5` | 注册用户每日文案提取次数 |
   | `atc_url_ttl` | `7200` | API 地址有效期估计（秒），上线前实测校准 |
   | `share_play_priority` | `["dy1","dy2","atc","proxy"]` | 播放优先级 JSON 数组 |

2. **数据表**（`_SCHEMA` 就地演进，无迁移框架）：
   ```sql
   CREATE TABLE IF NOT EXISTS atc_cache(
     item_id TEXT PRIMARY KEY,
     video_url TEXT, url_fetched_at INTEGER,
     content TEXT, text_content TEXT,
     audio_url TEXT, duration REAL,
     created INTEGER, updated INTEGER
   );
   CREATE TABLE IF NOT EXISTS atc_jobs(
     id INTEGER PRIMARY KEY AUTOINCREMENT,
     item_id TEXT, work_url TEXT,
     purpose TEXT,              -- play / transcript
     task_id TEXT,
     status TEXT DEFAULT 'pending',  -- pending/submitted/done/failed
     error TEXT, created INTEGER, updated INTEGER
   );
   ```
   - `shares` 表不加列，渲染时按 `item_id` 联查 `atc_cache`。
   - `_sweeper` 顺手清理：`atc_cache` 按 `updated` 超 `DATA_RETENTION_DAYS` 删；`atc_jobs` 终态保留 7 天。

3. **ATC 客户端 `_atc_request()`**（新分区，插在「分享页」分区后）：
   - 纯 `urllib`；**刻意不走代理池**（第三方 API 要求出口稳定，同微信 JS-SDK 先例；ATC 非抖音，无封 IP 风险）。
   - 未配置密钥或总开关关闭 → 返回 None，调用方静默降级。

4. **队列 worker `_atc_worker()`**（守护线程，仿 `_health_loop`，5 秒一轮）：
   - 同时最多 2 个 submitted 任务在轮询（ATC 并发上限 5，留余量）；pending 排队。
   - 轮询间隔 4 秒，单任务 5 分钟超时置 failed。
   - 成功 upsert `atc_cache`；启动时把进程中断遗留的 submitted 任务用原 `task_id` 续查。

5. **入队函数 `_atc_enqueue(item_id, work_url, purpose)`**：同 item_id 有在途任务或缓存未过期 → 直接返回（幂等去重）。

6. **管理后台**（`static/admin.html` 新增「增强线路」卡片）：
   - Key/Secret（password 输入、打码回显）/Base URL + 「测试连接」按钮（提交固定短链试跑，回显耗时与结果）。
   - 开关组：总开关、播放增强、文案提取、每日次数、URL 有效期。
   - 状态面板：队列深度、今日任务/失败数、最近错误。

### 验收
- 后台填入密钥 → 测试连接返回成功与耗时；密钥错误时状态面板显示原因。
- `node --test tests/*.js` 与后端套件全绿；本阶段不产生任何用户可见变化。

---

## 阶段二：分享页增强播放 + 获客回流

**目标**：播放线路可配置化，ATC 地址作为增强线路接入；分享页从"内容页"升级为"获客入口"。

### 执行路径

1. **服务端**（`server.py` 分享渲染处，约 2766 行附近）：
   - 联查 `atc_cache`：`now - url_fetched_at < atc_url_ttl` → 注入 `video.atc_url`。
   - 过期但有记录且开关开启 → 后台重新入队（惰性刷新），本次访问走其他线路。
   - 注入 `play_priority`（读 `share_play_priority`）到 `{{SHARE_DATA}}`。
   - 分享页创建/刷新成功后，若 `atc_enabled && atc_play_enhance` → 后台入队 `purpose=play`（不阻塞响应）。

2. **播放链动态化**（`static/share.html` 408–413 行 lines 数组）：
   - 按 `play_priority` 动态构建线路；`atc` 无 URL 自动跳过。
   - 上报 `source` 增加 `atc` 值 → 后台「播放诊断」自动多一列。
   - ⚠️ 保留锚点字符串（`let _playSession = 0;` 等），改完跑 `node --test tests/test_share_playback.js`。

3. **播放优先级拖拽**（管理后台）：四个线路块（dy1 抖音直连 / dy2 备用域名 / atc API地址 / proxy 服务器代理），Pointer Events 实现拖拽排序（无依赖，约 60 行），保存即时生效。管理员把 atc 拖第一即"微信优先 API 地址"，过期自动落链——替代原方案第 4 条的写死逻辑。

4. **获客回流 CTA**（`static/share.html`，PM 建议 #1）：
   - 页面底部新增回流区："想保存这个视频 / 分享你的作品？→ 打开抖音下载器，粘贴链接 10 秒搞定"，链主站。
   - 分享海报 canvas 增加品牌条（站名 + 域名）——海报进朋友圈是免费广告位。
   - 微信卡片标题格式评估 `「标题」- 抖音下载器`，上线后盯 `cta_clicks` 曲线，掉则回滚。

5. **分享数据反馈**（PM 建议 #4）：「我的分享」列表中，对近 7 天播放 >10 的分享页显示 🔥 标记与播放数高亮——给用户"回来看看"的理由。

### 验收
- 后台拖拽 atc 到首位 → 真机微信打开新分享页，首走 atc 线路（播放诊断可见）。
- 人为把 `atc_url_ttl` 调小模拟过期 → 自动落到下一条线路且后台重新入队。
- 分享页底部 CTA 可见可点，`cta_clicks` 正常计数；海报带品牌条。

---

## 阶段三：文案提取功能 + 注册时刻引导

**目标**：注册用户每天 5 次免费文案提取；在四个价值时刻做可忽略的注册引导。

### 执行路径

1. **后端 API**（新路由，门禁链严格复用现有机制）：
   ```
   POST /api/atc/transcript   {item_id 或分享文案}
   ```
   - 链：`current_user` 登录校验（401 + 引导文案）→ 功能开关 → **缓存命中直接返回（不扣次）** → `reserve_quota(purpose="atc_transcript")` 原子预占 → 入队 → 返回 `{state:"processing"}`；失败 `release_quota` 退款。
   - 配额上限读 `atc_transcript_daily`，进 `usage_daily`。
   ```
   GET /api/atc/transcript?item_id=xxx
   ```
   - 返回 `{state: ready|processing|failed, text?, audio_url?, remaining?}`；前端每 4 秒轮询，最长 3 分钟提示稍后查看。

2. **首页结果卡"获取文案"区块**（`static/index.html`，放在 extras 下方，非按钮形态）：
   - 登录用户：开关 + "今日剩余 N 次"，**默认关**；打开 → 提交 → 轮询 → 展示全文（可复制 + audio 试听）。
   - 匿名用户：开关**不置灰、可点**，点了直接弹登录框（PM 建议 #7-①）："获取文案需要登录，注册后每天免费 5 次"；**登录成功后自动打开开关并继续流程**。
   - 批量解析不提供此开关（并发上限 5 吃不消）。
   - i18n：中文 HTML + `data-i18n` + en 词条三同步。

3. **四个价值时刻注册引导**（PM 建议 #7，全部可关闭形态）：

   | 时机 | 实现 |
   |---|---|
   | ① 点文案开关（见上） | 登录弹窗 + 成功后自动继续 |
   | ② 配额用尽 429 | 报错文案改"今日免费次数已用完，登录后每天 10 次 →"，点击直达注册 |
   | ③ 分享页生成成功弹窗 | 匿名时加一行："当前链接 7 天有效，登录后延长至 30 天" |
   | ④ 第 2~3 次成功解析后 | 结果卡角落轻提示（可关，localStorage 记 7 天不再出现） |

4. **文案提取历史**（PM 建议 #5）：登录用户「我的分享」弹窗旁增加"我的文案"列表（`atc_cache` 按用户维度关联），提取过的文案可回看——内容工作者的资料库资产。

5. **注册漏斗观测**（PM 建议 #8）：滑块验证码加载/成功/失败打点进现有事件表，后台显示失败率；只存粗粒度计数，符合数据最小化。

### 验收
- 匿名点开关 → 弹登录 → 注册 → 自动开始提取，全程无需重复操作。
- 同一视频第二个用户点提取 → 命中缓存、秒回、不扣次数（后台 atc_jobs 无新记录）。
- 第 6 次提取 → 429 文案正确，`release_quota` 无漏扣（后端测试覆盖）。

---

## 阶段四：首页 UI 精简

**目标**：首屏可操作元素 11→5；消除重复信息；为文案区块腾位置。与阶段三同改结果卡，**合并为一次首页改动、一次测试**。

### 执行路径

1. **首屏减负**（`static/index.html` hero 区）：
   - 删 6 个信任徽章（与 trust 区 4 卡片逐条重复）。
   - 删 3 个步骤条（流程由 placeholder 承担）。
   - composer-hint 缩为一句（多条粘贴 + 导出 Excel 合并描述）。
2. **输入区按钮 4→2.5**：留 📋粘贴 + 解析主按钮；删"清空"（输入框有内容时浮小 × 代替）；"上传链接文件"降级为 hint 末尾文字链接。
3. **结果卡按钮 4→3**：主按钮下载视频；ghost 留 分享页/复制文案；**删"新窗口播放"**（页内已有播放器，且其链接走服务器带宽）。
4. **批量表格行内 3→2**：删"播放"（点封面即播放）；顶部批量导出加一句场景化明示（PM 建议 #6）。
5. **导航栏登录后 7→4**：邮箱触发下拉（我的分享 / API 控制台 / 退出），导航只留 邮箱下拉/语言/主题。
6. **bug 修复**：补 `.fallback-hint` 样式定义（当前裸奔）。
7. **i18n 清理**：删 `b_*`、`step*` 等废弃词条，新增词条双语同步。

### 红线
- `function downloadTarget` 等锚点字符串不可动；改完必须 `node --test tests/test_download_flow.js`。

### 验收
- 首屏可操作元素 ≤5 个；移动端实测首屏可见输入框与解析按钮。
- 双语切换无遗漏词条；明暗双主题无样式异常。

---

## 阶段五：SEO 矩阵 + 合规披露 + 测试发布

**目标**：新能力可被搜索引擎找到；第三方披露合规；全量测试后发布。

### 执行路径

1. **文案提取落地页**（PM 建议 #2）：`/transcript` 独立页面，走模板 + `_seo_head()` + hreflang + FAQPage JSON-LD 全套；首页 FAQ 加"怎么提取抖音视频的文案？"。
2. **程序化落地页**（PM 建议 #3）：图集下载、批量下载各一个静态落地页；`sitemap.xml`/`robots.txt`/`llms.txt` 同步。
3. **隐私披露（必做）**：README + 首页 FAQ + 落地页，中英同步："开启增强线路后，解析的抖音链接会提交给第三方服务（AnyToCopy）处理，用于获取播放地址与语音转文字文案；未开启则不发送。"不用绝对化措辞。
4. **测试**：
   - `tests/test_security_reliability.py` 新增：文案配额原子预占/失败退款/缓存命中不扣次/匿名 401/开关静默/优先级非法值校验。
   - `tests/test_share_playback.js`：播放链按 priority 构建、atc 无 URL 跳过。
   - 手动实测：真机微信验证 atc 线路、过期落链、惰性刷新。
5. **发布**：bump `APP_VERSION`（+minor）→ README 版本号/功能列表/更新日志同步 → `git log` 风格提交。

### 验收
- 三套件全绿；新落地页 view-source 无未替换占位符；`GET /healthz` 版本号正确。

---

## 执行顺序与依赖

```
阶段一（基座）──→ 阶段二（分享页增强+获客）──┐
      └──────→ 阶段三（文案提取+注册引导）─┴──→ 阶段五（SEO+合规+发布）
阶段四（首页精简，无依赖，可与一/二并行，但与阶段三合批改首页）
```

| 阶段 | 预估工作量 | 交付物 |
|---|---|---|
| 一 | 1 天 | 后台可配密钥，功能不可见 |
| 二 | 1.5 天 | 播放优先级生效，分享页带回流 CTA |
| 三+四 | 2 天 | 文案提取上线，首页变干净，注册引导生效 |
| 五 | 1 天 | SEO 页面 + 合规 + 发布 |

## 上线后 30 天复盘清单
- [ ] `atc_url_ttl` 实测值校准（上线前先做单次探测）
- [ ] 播放诊断：atc 线路成功率、微信内 proxy 占比变化
- [ ] `cta_clicks` 曲线（阶段二效果）
- [ ] 注册转化率 + 滑块失败率（阶段三效果）
- [ ] ATC 会员转录额度消耗速度 vs 每日 5 次 × 注册用户数，决定是否调上限
