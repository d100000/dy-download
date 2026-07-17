#!/usr/bin/env python3
"""极简本地测试代理：支持普通 HTTP 转发与 HTTPS 的 CONNECT 隧道。

仅用于验证下载器的"所有出站请求走代理"链路是否真实生效——
每个经过的请求都会打印一行日志。切勿用于生产。

用法:  python3 tools/testproxy.py [port]   # 默认 8899
"""
import select
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlreq

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
_count = 0
_lock = threading.Lock()


def _tick(kind, target):
    global _count
    with _lock:
        _count += 1
        n = _count
    print(f"[proxy] #{n:03d} {kind} {target}", flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):    # 静音默认日志，用自定义
        pass

    def do_CONNECT(self):
        _tick("CONNECT", self.path)
        host, _, port = self.path.partition(":")
        try:
            upstream = socket.create_connection((host, int(port or 443)), timeout=15)
        except Exception as e:
            self.send_error(502, str(e))
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        self._tunnel(self.connection, upstream)

    def do_GET(self):
        self._forward()

    def do_POST(self):
        self._forward()

    def _forward(self):
        _tick(self.command, self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else None
            req = urlreq.Request(self.path, data=body, method=self.command)
            for k, v in self.headers.items():
                if k.lower() not in ("proxy-connection", "connection", "host"):
                    req.add_header(k, v)
            with urlreq.urlopen(req, timeout=20) as r:
                data = r.read()
                self.send_response(r.status)
                for k, v in r.headers.items():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except Exception as e:
            try:
                self.send_error(502, str(e))
            except Exception:
                pass

    @staticmethod
    def _tunnel(a, b):
        socks = [a, b]
        try:
            while True:
                r, _, x = select.select(socks, [], socks, 30)
                if x or not r:
                    break
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    (b if s is a else a).sendall(data)
        except Exception:
            pass
        finally:
            for s in socks:
                try:
                    s.close()
                except Exception:
                    pass


if __name__ == "__main__":
    print(f"[proxy] listening on http://127.0.0.1:{PORT}  (HTTP + CONNECT)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
