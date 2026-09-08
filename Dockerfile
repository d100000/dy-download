FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY static ./static

# 代理池与设置持久化目录（可挂载卷保留配置）
ENV DATA_DIR=/data
VOLUME ["/data"]
# 容器默认 fail-closed：必须通过 -e/secret 提供至少 12 位的管理密码。
ENV REQUIRE_ADMIN_PASSWORD=1
# Chromium is used only for the official Douyin detail request; the value is
# explicit so deployments do not depend on distro-specific binary discovery.
ENV DOUYIN_BROWSER_BIN=/usr/bin/chromium

EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
