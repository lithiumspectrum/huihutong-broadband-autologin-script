"""Captive portal 在线检测（三态探测，逻辑等价 v4 shell）。

对探测 URL 发 GET（不跟随首跳）：
  204           → 已在线
  301/302/...   → 未认证，取 Location 作为门户入口
  200 + JS 跳转 → 未认证，从 HTML 抠 location.href
  网络异常/其他  → 视为离线（原因记录为 probe_error / probe_<code>）
"""

import re

_JUMP_RE = re.compile(rb"""location\.href\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE)
_REDIRECT_STATUS = {301, 302, 303, 307, 308}


class ProbeResult:
    def __init__(self, online, code=None, redirect=None, reason=""):
        self.online = online          # True=在线 False=离线
        self.code = code              # HTTP 码（异常时为 None）
        self.redirect = redirect      # 离线时的门户入口 URL
        self.reason = reason          # 离线原因（短标识）

    def detail(self):
        if self.online:
            return "online(204)"
        if self.redirect:
            return "%s -> %s" % (self.reason, self.redirect)
        return self.reason


def probe(client, url, timeout=10):
    """返回 ProbeResult；网络异常同样判定为离线（ uplink 断了也要触发恢复）。"""
    try:
        resp = client.get(url, timeout=timeout, allow_redirects=False)
    except Exception as exc:
        return ProbeResult(False, None, None, "probe_error: %s" % exc)

    if resp.status == 204:
        return ProbeResult(True, 204)

    if resp.status in _REDIRECT_STATUS and resp.location():
        from urllib.parse import urljoin
        target = urljoin(url, resp.location())
        return ProbeResult(False, resp.status, target, "redirect(%d)" % resp.status)

    if resp.status == 200:
        match = _JUMP_RE.search(resp.body)
        if match:
            from urllib.parse import urljoin
            target = urljoin(url, match.group(1).decode("utf-8", errors="replace"))
            return ProbeResult(False, 200, target, "html_js_jump(200)")

    return ProbeResult(False, resp.status, None, "probe_unknown(%s)" % resp.status)
