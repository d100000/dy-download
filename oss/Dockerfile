FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
COPY static ./static
EXPOSE 8000
# 可选：-e PROXY=socks5://user:pass@host:port 让解析走代理防封 IP
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
