#!/usr/bin/env bash
# 把 oss/（最小可用版）作为一条干净提交强制发布到 GitHub，
# 覆盖远程历史（清除之前泄露的完整版代码）。
set -euo pipefail
cd "$(dirname "$0")"

REMOTE="${REMOTE:-https://github.com/d100000/dy-download.git}"
echo "即将用 oss/ 的内容【强制覆盖】远程仓库的 main 分支："
echo "  $REMOTE"
echo "这会清空远程原有历史（含此前泄露的完整版代码），不可逆。"
read -r -p "确认继续？输入 yes： " ans
[ "$ans" = "yes" ] || { echo "已取消。"; exit 1; }

TMP="$(mktemp -d)"
# 仅复制最小版文件（排除 .venv 等）
rsync -a --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' ./ "$TMP/"
cd "$TMP"
git init -q -b main
git add -A
git -c user.name="${GIT_NAME:-author}" -c user.email="${GIT_EMAIL:-author@example.com}" \
    commit -q -m "抖音无水印下载器 · 开源基础版 (Douyin/TikTok downloader, no watermark)"
git remote add origin "$REMOTE"
git push -f origin main
echo "✓ 已发布最小可用版到 $REMOTE（远程历史已重置为干净单提交）"
rm -rf "$TMP"
