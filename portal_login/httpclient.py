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
        """解析 URL，返回 (urlsplit 结果, 主机键 (scheme, hostname, port))。"""
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
        """key 为主机键 + timeout：socket 超时在建连时固化，不同 timeout 不能复用同一条连接。"""
        conn = self._conns.get(key)
        if conn is None:
            conn = self._make_conn(key[0], key[1], key[2], timeout)
            self._conns[key] = conn
        return conn

    def _drop_conn(self, key):
        conn = self._conns.pop(key, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _raw_request(self, method, url, body, headers, timeout):
        parts, key = self._split(url)
        key = key + (timeout,)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        req_headers = {"User-Agent": self._user_agent, "Accept": "*/*"}
        if headers:
            req_headers.update(headers)

        conn = self._get_conn(key, timeout)
        conn.request(method, path, body=body, headers=req_headers)
        resp = conn.getresponse()
        raw = resp.read()  # 必须读完，连接才能复用
        return Response(resp.status, resp.msg, raw, url)

    def _request(self, method, url, body=None, headers=None, timeout=15):
        """单次请求；连接异常时丢弃旧连接重建重试一次。"""
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            return self._raw_request(method, url, body, headers, timeout)
        except _CONN_ERRORS:
            self._drop_conn(self._split(url)[1] + (timeout,))
            try:
                return self._raw_request(method, url, body, headers, timeout)
            except _CONN_ERRORS as exc:
                raise HttpClientError("请求失败: %s (%s)" % (url, exc)) from exc

    def get(self, url, headers=None, timeout=15, allow_redirects=True,
            max_redirects=10):
        """发起 GET，默认跟随 3xx 跳转。连接异常自动重建并重试一次。

        返回 Response。网络不可达时抛出 HttpClientError（由上层统一处理）。
        """
        current = url
        for hop in range(max_redirects + 1):
            resp = self._request("GET", current, None, headers, timeout)

            if (not allow_redirects or resp.status not in self.REDIRECT_STATUS
                    or not resp.location()):
                return resp
            if hop >= max_redirects:
                raise HttpClientError("重定向次数过多: %s" % url)
            current = urljoin(current, resp.location())

        raise HttpClientError("重定向次数过多: %s" % url)

    def post(self, url, body, headers=None, timeout=15):
        """发起 POST（不跟随跳转）。body 为 str/bytes。

        用于 loginByPhoneAndUid（JSON body）。网络不可达时抛 HttpClientError。
        """
        return self._request("POST", url, body, headers, timeout)

    def close(self):
        for conn in list(self._conns.values()):
            try:
                conn.close()
            except Exception:
                pass
        self._conns.clear()


class HttpClientError(Exception):
    """网络层错误（超时、拒绝连接、TLS 失败等）。"""
