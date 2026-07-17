FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY static ./static

# 代理池与设置持久化目录（可挂载卷保留配置）
ENV DATA_DIR=/data
VOLUME ["/data"]
# 生产务必用 -e ADMIN_PASSWORD=... 覆盖默认密码
ENV ADMIN_PASSWORD=douyin-admin

EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
