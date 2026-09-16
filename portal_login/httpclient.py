"""极简 HTTP 客户端（标准库实现）。

为什么不用 urllib/curl 子进程：
- http.client 直接复用同主机的 TCP/TLS 连接（keep-alive），
  实测对 api.215123.cn 的请求中位耗时从 curl 冷启动的 ~252ms 降到 ~78ms；
- 天然忽略 http_proxy 环境变量，认证流量强制 WAN 直连；
- 连接被网关静默断开时自动重建并重试一次。
"""

import http.client
import os
import socket
import ssl
from urllib.parse import urlsplit, urljoin


# 连接层异常：丢弃旧连接后值得重建重试一次
_CONN_ERRORS = (
    http.client.HTTPException,
    ConnectionError,
    OSError,
    TimeoutError,
    socket.timeout,
    ssl.SSLError,
)


class Response:
    def __init__(self, status, headers, body, url):
        self.status = status
        self.headers = headers          # http.client.HTTPMessage（大小写不敏感）
        self.body = body or b""
        self.url = url                  # 最终 URL（跟随跳转后）

    @property
    def text(self):
        return self.body.decode("utf-8", errors="replace")

    def location(self):
        # HTTPMessage.get 本身大小写不敏感，无需小写回退
        return self.headers.get("Location")


def _ssl_context():
    ctx = ssl.create_default_context()
    # OpenWrt 上 CA bundle 的常见路径，存在哪个补哪个（默认路径已覆盖大多数系统）
    for cafile in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
        if os.path.isfile(cafile):
            try:
                ctx.load_verify_locations(cafile=cafile)
                break
            except ssl.SSLError:
                pass
    return ctx


class HttpClient:
    """按 (scheme, host, port) 缓存长连接的 GET 客户端。"""

    REDIRECT_STATUS = {301, 302, 303, 307, 308}

    def __init__(self, user_agent="portal-login/5.0"):
        self._conns = {}
        self._user_agent = user_agent

    @staticmethod
    def _split(url):
        """解析 URL，返回 (urlsplit 结果, 连接缓存键)。端口解析只此一处。"""
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        port = parts.port or (443 if scheme == "https" else 80)
        return parts, (scheme, parts.hostname, port)

    def _make_conn(self, scheme, host, port, timeout):
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=timeout,
                                               context=_ssl_context())
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def _get_conn(self, key, timeout):
        conn = self._conns.get(key)
        if conn is None:
            scheme, host, port = key
            conn = self._make_conn(scheme, host, port, timeout)
            self._conns[key] = conn
        return conn

    def _drop_conn(self, key):
        conn = self._conns.pop(key, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _raw_request(self, url, headers, timeout):
        parts, key = self._split(url)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        req_headers = {"User-Agent": self._user_agent, "Accept": "*/*"}
        if headers:
            req_headers.update(headers)

        conn = self._get_conn(key, timeout)
        conn.request("GET", path, headers=req_headers)
        resp = conn.getresponse()
        body = resp.read()  # 必须读完，连接才能复用
        return Response(resp.status, resp.msg, body, url)

    def get(self, url, headers=None, timeout=15, allow_redirects=True,
            max_redirects=10):
        """发起 GET，默认跟随 3xx 跳转。连接异常自动重建并重试一次。

        返回 Response。网络不可达时抛出 HttpClientError（由上层统一处理）。
        """
        current = url
        for hop in range(max_redirects + 1):
            try:
                resp = self._raw_request(current, headers, timeout)
            except _CONN_ERRORS:
                self._drop_conn(self._split(current)[1])
                try:
                    resp = self._raw_request(current, headers, timeout)
                except _CONN_ERRORS as exc:
                    raise HttpClientError(
                        "请求失败: %s (%s)" % (current, exc)) from exc

            if (not allow_redirects or resp.status not in self.REDIRECT_STATUS
                    or not resp.location()):
                return resp
            if hop >= max_redirects:
                raise HttpClientError("重定向次数过多: %s" % url)
            current = urljoin(current, resp.location())

        raise HttpClientError("重定向次数过多: %s" % url)

    def close(self):
        for conn in list(self._conns.values()):
            try:
                conn.close()
            except Exception:
                pass
        self._conns.clear()


class HttpClientError(Exception):
    """网络层错误（超时、拒绝连接、TLS 失败等）。"""
