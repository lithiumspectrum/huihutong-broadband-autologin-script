"""本机模拟慧湖通/eportal 链路的 HTTP 服务器（仅 selftest 使用）。

模拟内容：
  /generate_204                       未认证 302 → /sso/login.html?redirect=<eportal基址>
                                      已认证 204
  /sso/login.html                     200 HTML（最终 URL 含 redirect= 参数，对应现场B）
  /web-app/auth/certificateLogin     200 带递增编号的 satoken
  /ac/auth/selectBindBroadband       200 已绑定 service 列表
  /ac/auth/addBindBroadband          200 绑定成功（写入 state.bound_services）
  /ac/auth/oauthRedirect             chain 场景 200；401 场景首次 401、二次 200
                                     bind/bound 场景：未绑定服务商 → 500「系统异常，请联系客服」
  /eportal/login_sso.jsp             302 success.jsp（同时标记已放行）
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs


class MockState:
    def __init__(self, scenario="chain"):
        self.scenario = scenario
        self.authed = scenario == "online"
        self.mint_count = 0
        self.oauth_calls = 0
        self.select_calls = 0
        self.bind_calls = 0
        # bind 场景：服务商未绑定（oauthRedirect 会抛业务 500）
        # bound 场景：已绑定（oauthRedirect 直接成功）
        self.require_bind = scenario in ("bind", "bound")
        self.bound_services = [1] if scenario == "bound" else []


def start_mock(scenario="chain"):
    """启动 mock，返回 (base_url, state, server)。调用方负责 server.shutdown()。"""
    state = MockState(scenario)
    server = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = server.server_address[1]
    base = "http://127.0.0.1:%d" % port
    ep_base = ("%s/eportal/login_sso.jsp?wlanuserip=10.0.0.2&wlanacname=MOCK"
               % base)
    state.ep_base = ep_base

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # 同时验证 keep-alive 复用

        def log_message(self, fmt, *args):
            pass

        def _send(self, status, body=b"", content_type="text/plain",
                  location=None):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", content_type)
            # 204/304 按 RFC 不得携带 Content-Length 与 body
            if status not in (204, 304):
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD" and status not in (204, 304):
                self.wfile.write(body)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj, ensure_ascii=False),
                       "application/json")

        def do_GET(self):
            self._handle()

        def do_POST(self):
            self._handle()

        def _handle(self):
            parts = urlsplit(self.path)
            path = parts.path

            if path == "/generate_204":
                if state.authed:
                    self._send(204)
                else:
                    # 现场 B：redirect= 后为一次编码原文；mock 直接用裸 URL，
                    # extract_portal_url 截取原文后由 oauth 层统一编码
                    sso = "/sso/login.html?clientId=mock&redirect=" + ep_base
                    self._send(302, location=sso)
                return

            if path == "/sso/login.html":
                # 客户端跟随到此后最终 URL 含 redirect=，直接 200
                self._send(200, "<html><body>mock sso</body></html>",
                           "text/html")
                return

            if path == "/web-app/auth/certificateLogin":
                state.mint_count += 1
                self._json(200, {
                    "success": True, "code": 200, "message": "ok",
                    "data": {
                        "userId": "195946", "account": "mockaccount",
                        "name": "selftest", "tokenName": "satoken",
                        "token": "mock-token-%d" % state.mint_count,
                    },
                })
                return

            if path == "/ac/auth/selectBindBroadband":
                state.select_calls += 1
                self._json(200, {
                    "success": True, "code": 200, "message": "ok",
                    "data": [{"id": i, "service": s}
                             for i, s in enumerate(state.bound_services)],
                })
                return

            if path == "/ac/auth/addBindBroadband":
                state.bind_calls += 1
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except ValueError:
                    self._json(400, {"code": 400, "message": "bad json"})
                    return
                if not payload.get("account") or not payload.get("password"):
                    self._json(400, {"code": 400, "message": "缺少账号或密码"})
                    return
                state.bound_services.append(int(payload.get("service")))
                self._json(200, {"success": True, "code": 200,
                                 "message": "绑定成功"})
                return

            if path == "/ac/auth/oauthRedirect":
                state.oauth_calls += 1
                query = parse_qs(parts.query)
                if state.scenario == "401" and state.oauth_calls == 1:
                    self._json(401, {"code": 401, "message": "token 失效"})
                    return
                if query.get("serviceName", [""])[0] != "chinaMobile":
                    self._json(400, {"code": 400, "message": "bad operator"})
                    return
                # 模拟真实平台：该服务商未绑定时抛业务 500（HTTP 200）
                if state.require_bind and 1 not in state.bound_services:
                    self._json(200, {"success": False, "code": 500,
                                     "message": "系统异常，请联系客服"})
                    return
                self._json(200, {
                    "code": 200,
                    "data": "%s&code=CODE%d&serviceName=chinaMobile"
                            % (ep_base, state.oauth_calls),
                })
                return

            if path == "/eportal/login_sso.jsp":
                state.authed = True
                self._send(302,
                           location="/eportal/./success.jsp?userIp=10.0.0.2")
                return

            self._send(404, "not found")

    server.RequestHandlerClass = Handler
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return base, state, server
