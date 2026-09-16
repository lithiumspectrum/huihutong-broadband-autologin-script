"""上线链路（v4 已实测协议，v5 原样迁移）：

  1. 跟随门户入口重定向链，提取 eportal login_sso.jsp 基址（redirect_uri）
     - 现场 A：直接 302 到 http://<eportal>/eportal/login_sso.jsp?...
     - 现场 B：302 到 SSO login.html?...&redirect=<一次编码的 eportal 原文>
  2. GET /ac/auth/oauthRedirect（satoken 头）→ 返回带一次性 code 的最终 eportal URL
  3. GET 该 URL（坚持 GET，不用 HEAD）→ 交换机放行 WAN 口源 IP
"""

import json
from urllib.parse import quote

from .auth import AuthError
from .detector import probe
from .httpclient import HttpClientError


class LoginResult:
    def __init__(self, success, stage="", message="", http_code=None):
        self.success = success
        self.stage = stage        # 失败阶段：portal_extract/oauth/eportal/verify
        self.message = message
        self.http_code = http_code


def extract_portal_url(client, entry, timeout=15):
    """跟随完整跳转链提取 eportal 基址。"""
    final = entry
    try:
        resp = client.get(entry, timeout=timeout, allow_redirects=True,
                          max_redirects=10)
        if resp.url:
            final = resp.url
    except HttpClientError:
        # 回退：只取一跳 Location
        try:
            one = client.get(entry, timeout=timeout, allow_redirects=False)
            if one.location():
                final = one.location()
        except HttpClientError:
            pass

    # 现场 B：从 SSO URL 截取 redirect= 之后的原文（不再 decode，
    # 与前端 broadband.js 的 getParam('redirect') 行为一致）
    if "redirect=" in final and "/eportal/" not in final:
        final = final.split("redirect=", 1)[1]
    return final


def _oauth_once(client, cfg, token, portal_url, timeout):
    """调一次 oauthRedirect。返回 (status_code, json_dict, raw_text)。"""
    # quote(s, safe="") 与前端 encodeURIComponent 对本组参数编码结果一致
    # （urlencode 默认的 quote_via=quote 会把 '/' 留为裸字符，与前端不同）
    params = ("response_type=code&client_id=%s&redirect_uri=%s&serviceName=%s"
              % (quote(cfg.CLIENT_ID, safe=""),
                 quote(portal_url, safe=""),
                 quote(cfg.SERVICE_NAME, safe="")))
    url = "%s/ac/auth/oauthRedirect?%s" % (cfg.API_BASE.rstrip("/"), params)
    resp = client.get(url, headers={"satoken": token}, timeout=timeout)
    try:
        return resp.status, json.loads(resp.text), resp.text
    except ValueError:
        return resp.status, None, resp.text


def perform_login(cfg, client, tokens, logger, probe_result=None):
    """执行一次完整上线；401 时用 OPEN_ID 重新换发 token 并重试一次。

    probe_result：调用方（守护 tick）本拍已完成的探测结果，传入则复用，
    避免一拍内对同一探测 URL 发两次请求。
    """
    if probe_result is None:
        probe_result = probe(client, cfg.PROBE_URL, cfg.PROBE_TIMEOUT)
    if probe_result.online or not probe_result.redirect:
        return LoginResult(False, "probe",
                           "无需登录或无门户入口: %s" % probe_result.detail(),
                           probe_result.code)

    entry = probe_result.redirect
    logger.info("检测到未登录，门户重定向: %s", entry)

    portal_url = extract_portal_url(client, entry, cfg.API_TIMEOUT)
    if not portal_url or "/eportal/" not in portal_url:
        return LoginResult(False, "portal_extract",
                           "无法提取 eportal URL: %s" % portal_url)

    try:
        token = tokens.get_token()
    except AuthError as exc:
        return LoginResult(False, "token", str(exc))

    try:
        status, data, raw = _oauth_once(client, cfg, token, portal_url,
                                        cfg.API_TIMEOUT)
    except HttpClientError as exc:
        return LoginResult(False, "oauth", "网络失败: %s" % exc)

    # 401：丢弃 token，用 OPEN_ID 重换后重试一次（覆盖每日强制下线场景）
    if status == 401 or (isinstance(data, dict) and data.get("code") in (401, "401")):
        logger.warning("satoken 401，使用 OPEN_ID 重新换发后重试一次")
        tokens.invalidate()
        try:
            token = tokens.get_token(force_refresh=True)
        except AuthError as exc:
            return LoginResult(False, "token", "重新换发失败: %s" % exc, 401)
        try:
            status, data, raw = _oauth_once(client, cfg, token, portal_url,
                                            cfg.API_TIMEOUT)
        except HttpClientError as exc:
            return LoginResult(False, "oauth", "重试网络失败: %s" % exc, 401)

    if not isinstance(data, dict) or data.get("code") not in (200, "200"):
        return LoginResult(False, "oauth",
                           "oauthRedirect 异常: %s" % (raw or "")[:300], status)

    final_url = data.get("data")
    if not final_url:
        return LoginResult(False, "oauth", "响应缺少 data: %s" % raw[:300], status)

    # 最后一跳：GET 触发 eportal 放行源 IP
    eportal_location = ""
    try:
        resp = client.get(final_url, timeout=cfg.API_TIMEOUT,
                          allow_redirects=False)
        eportal_location = resp.location() or ""
    except HttpClientError as exc:
        return LoginResult(False, "eportal", "访问 eportal 失败: %s" % exc)

    # 复核：204 为最终依据，success.jsp 为辅助依据
    verify = probe(client, cfg.PROBE_URL, cfg.PROBE_TIMEOUT)
    if verify.online:
        logger.info("登录成功（运营商=%s）", cfg.SERVICE_NAME)
        return LoginResult(True, "verify", "已上线(204)")
    if "success" in eportal_location.lower():
        logger.info("登录成功（eportal success 页面）")
        return LoginResult(True, "eportal", "success.jsp")

    return LoginResult(False, "verify",
                       "放行请求已发送但未确认上线（eportal=%s）"
                       % eportal_location[:200])
