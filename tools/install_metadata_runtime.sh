#!/usr/bin/env bash
# 只安装可选 worker 运行时；不执行第三方安装脚本，不接触应用 .venv/数据。
set -euo pipefail
metadata_root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
metadata_python="${METADATA_PYTHON:-${PYTHON:-python3.12}}"
metadata_venv="${1:-${METADATA_VENV:-$metadata_root/.venv-metadata}}"
if [ "$#" -gt 1 ]; then
    echo 'Usage: PYTHON=python3.12 tools/install_metadata_runtime.sh [INSTALL_DIR]' >&2
    exit 1
fi
metadata_commit="737bf3dfe9de1dbff57990c0ec4c9e02c75c3d0f"
metadata_archive="https://codeload.github.com/Evil0ctal/Douyin_TikTok_Download_API/tar.gz/$metadata_commit?metadata_runtime=1"
metadata_sha256="8da662c5656ae039678b82cbc0688253537760b1f6fccbf037858c2c23f3e18e"
"$metadata_python" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else "metadata runtime requires Python 3.12")'
if [ -n "${METADATA_SDK_ARCHIVE:-}" ]; then
    # 支持预下载/离线部署；pip 和运行时仍核验同一个固定源码 SHA256。
    metadata_archive="$("$metadata_python" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve(strict=True).as_uri())' "$METADATA_SDK_ARCHIVE")"
fi
metadata_venv="$("$metadata_python" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$metadata_venv")"
metadata_main_venv="$("$metadata_python" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$metadata_root/.venv")"
if [ "$metadata_venv" = "$metadata_main_venv" ]; then
    echo 'Refusing to replace the application .venv.' >&2
    exit 1
fi
"$metadata_python" -m venv "$metadata_venv"
"$metadata_venv/bin/python" -m pip install -r "$metadata_root/requirements-metadata.txt"
"$metadata_venv/bin/python" -m pip install --timeout 60 --no-deps \
    "dtk @ $metadata_archive#sha256=$metadata_sha256"
# dtk 项目元数据还声明了完整 API 的依赖，核心部署不运行这些入口；因此完整
# pip check 会报告那些有意不安装的组件。这里执行真实核心导入和来源核验。
PYTHONPATH="$metadata_root${PYTHONPATH:+:$PYTHONPATH}" "$metadata_venv/bin/python" - <<'PY'
import asyncio
from tools.metadata_http import DEFAULT_UA, _SDKBackend
async def check():
    backend = _SDKBackend('', DEFAULT_UA, {})
    await backend.close()
asyncio.run(check())
print('Metadata HTTP runtime ready; core imports and pinned SDK origin verified. No network request performed.')
PY
echo "Configure BROWSER_TITLE_PYTHON=$metadata_venv/bin/python for the isolated worker."
echo 'Chromium is not installed by this script. Install it separately with this runtime and a shared PLAYWRIGHT_BROWSERS_PATH.'
