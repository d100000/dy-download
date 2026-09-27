FROM python:3.12-slim

WORKDIR /app
ARG WITH_BROWSER_TITLE=0
ARG WITH_METADATA_HTTP=0
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
COPY requirements.txt requirements-lock.txt requirements-browser.txt requirements-metadata.txt ./
COPY browser_titles.py metadata_fields.py ./
COPY tools/install_metadata_runtime.sh tools/metadata_http.py ./tools/
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

# 可选安装标题浏览器；默认镜像继续支持纯 HTTP 解析。
RUN if [ "$WITH_METADATA_HTTP" = "1" ]; then \
      PYTHON=python3.12 bash tools/install_metadata_runtime.sh /app/.venv-metadata \
      && /app/.venv-metadata/bin/python -m playwright install --with-deps --only-shell chromium \
      && rm -rf /var/lib/apt/lists/*; \
    elif [ "$WITH_BROWSER_TITLE" = "1" ]; then \
      pip install --no-cache-dir -r requirements-browser.txt \
      && python -m playwright install --with-deps --only-shell chromium \
      && rm -rf /var/lib/apt/lists/*; \
    fi

COPY server.py browser_titles.py metadata_fields.py ./
COPY tools/browser_title_worker.py ./tools/browser_title_worker.py
COPY static ./static

# 代理池与设置持久化目录（可挂载卷保留配置）
ENV DATA_DIR=/data
VOLUME ["/data"]
# 容器默认 fail-closed：必须通过 -e/secret 提供至少 12 位的管理密码。
ENV REQUIRE_ADMIN_PASSWORD=1

EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
