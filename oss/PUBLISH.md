# 发布开源基础版到 GitHub（只上传最小可用版）

本目录 `oss/` 就是要公开的**最小可用版**——只含"粘贴链接 → 无水印下载"。
完整版（管理后台 / 代理池 / 用户体系 / 计费 API / 数据分析）**保留在本机，不上传**。

## ⚠️ 重要：历史泄露

之前几次提交已经把含**完整版代码（含管理后台）的 `server.py`** 推到了
`github.com/d100000/dy-download` 的历史里。只是"以后不传"不够——**必须重写历史**
才能把泄露的代码从公开仓库彻底移除。下面的脚本用一次**强制覆盖（force push）**
把远程重置为"只有本目录内容、单条干净提交"。

> 强制覆盖会**清空远程原有历史**，不可逆。确认后再执行。

## 一键发布

```bash
cd oss
./publish.sh          # 会二次确认后 force-push
```

脚本做的事：
1. 在临时目录用 `oss/` 内容初始化一个全新 git 仓库（干净单提交）；
2. `git push -f` 覆盖 `d100000/dy-download` 的 `main`——远程从此只剩最小版，历史里的完整版代码被移除。

## 之后建议

- 到仓库 **Settings → Topics** 加标签，利于 GitHub 搜索收录：
  `douyin` `tiktok` `downloader` `douyin-downloader` `no-watermark` `video-downloader`
  `open-source` `self-hosted` `python` `fastapi`
- **About** 描述（开头放主关键词，5–15 词）：
  `Open-source, self-hosted Douyin (TikTok) video downloader — no watermark, no login.`
- 完整版联系方式：在 README 的"联系作者"处填你的真实联系方式。
