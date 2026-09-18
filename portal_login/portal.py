"""上线链路（v4 已实测协议，v5 原样迁移）：

  1. 跟随门户入口重定向链，提取 eportal login_sso.jsp 基址（redirect_uri）
     - 现场 A：直接 302 到 http://<eportal>/eportal/login_sso.jsp?...
     - 现场 B：302 到 SSO login.html?...&redirect=<一次编码的 eportal 原文>
  2. OPEN_ID 换 satoken（auth.TokenManager）
  3. 步骤 3.5（条件性）：确保当前服务商已绑定宽带账号
     - eportal "忘记"该设备时，未绑定服务商会让 oauthRedirect 抛业务 500
     - 未配置 broadband_account/password 时整体跳过
  4. GET /ac/auth/oauthRedirect（satoken 头）→ 返回带一次性 code 的最终 eportal URL
  5. GET 该 URL（坚持 GET，不用 HEAD）→ 交换机放行 WAN 口源 IP
"""

import json
from urllib.parse import quote

from .auth import AuthError
from .detector import probe
from .httpclient import HttpClientError


# serviceName → service(int)：取自线上 bind-broadband-form.html 的运营商下拉框
_SERVICE_ID = {
    "chinaTelecom": 0,
    "chinaMobile": 1,
    "chinaUnicom": 2,
    "local": 3,
}


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
    # 注意：redirect= 的值本身就是 eportal URL，必然含 /eportal/，
    # 所以不能用 "/eportal/ not in final" 作为分支条件（那会让截取永不触发）
    if "redirect=" in final:
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


def _select_bind(client, cfg, token, timeout):
    """查当前账号已绑定的宽带账号。返回 (services, code, detail)。

    services 为 service(int) 列表；失败时为 None，code 为业务码
    （401 表示 token 失效）或 None（网络/解析失败）。
    """
    url = "%s/ac/auth/selectBindBroadband" % cfg.API_BASE.rstrip("/")
    try:
        resp = client.get(url, headers={"satoken": token}, timeout=timeout)
    except HttpClientError as exc:
        return None, None, "网络失败: %s" % exc
    try:
        data = json.loads(resp.text)
    except ValueError:
        return None, None, "非 JSON 响应: %s" % resp.text[:200]
    if not isinstance(data, dict) or data.get("code") not in (200, "200"):
        code = data.get("code") if isinstance(data, dict) else None
        return None, code, "code=%s message=%s" % (
            code, data.get("message") if isinstance(data, dict) else resp.text[:200])
    rows = data.get("data")
    services = []
    if isinstance(rows, list):
        services = [r.get("service") for r in rows if isinstance(r, dict)]
    return services, 200, ""


def _add_bind(client, cfg, token, service, timeout):
    """提交宽带账号绑定。返回 (ok, detail)。"""
    url = "%s/ac/auth/addBindBroadband" % cfg.API_BASE.rstrip("/")
    # 与前端 layui 的 JSON.stringify(data.field) 一致：所有字段均为字符串
    body = json.dumps({"service": str(service),
                       "account": cfg.BROADBAND_ACCOUNT,
                       "password": cfg.BROADBAND_PASSWORD})
    try:
        resp = client.post(url, body, timeout=timeout,
                           headers={"satoken": token,
                                    "Content-Type": "application/json"})
    except HttpClientError as exc:
        return False, "网络失败: %s" % exc
    try:
        data = json.loads(resp.text)
    except ValueError:
        return False, "非 JSON 响应: %s" % resp.text[:200]
    if isinstance(data, dict) and data.get("code") in (200, "200"):
        return True, ""
    return False, "code=%s message=%s" % (
        data.get("code") if isinstance(data, dict) else None, resp.text[:200])


def _ensure_binding(client, cfg, tokens, token, logger, timeout):
    """步骤 3.5：确保当前服务商已绑定宽带账号，返回（可能已换发的）token。

    eportal 侧"忘记"该设备时，未绑定服务商会让 oauthRedirect 抛业务 500
    （HTTP 200 + code:500「系统异常，请联系客服」）。此处先查绑定，未绑定
    才补提交（已绑定时只有一次 GET，无副作用）。未配置宽带账号密码则整体跳过。
    """
    if not (cfg.BROADBAND_ACCOUNT and cfg.BROADBAND_PASSWORD):
        return token
    service = _SERVICE_ID.get(cfg.SERVICE_NAME)
    if cfg.BIND_SERVICE:
        service = cfg.BIND_SERVICE
    if service is None:
        logger.warning("未知运营商 %s，跳过宽带账号绑定检查", cfg.SERVICE_NAME)
        return token

    services, code, detail = _select_bind(client, cfg, token, timeout)
    if services is None and code in (401, "401"):
        logger.warning("查询宽带绑定遇 401，重新换发 token 后重试")
        tokens.invalidate()
        try:
            token = tokens.get_token(force_refresh=True)
        except AuthError as exc:
            logger.warning("重新换发失败，跳过绑定检查: %s", exc)
            return token
        services, code, detail = _select_bind(client, cfg, token, timeout)
    if services is None:
        logger.warning("查询宽带绑定失败（继续上线）: %s", detail)
        return token
    if service in services:
        return token

    ok, detail = _add_bind(client, cfg, token, service, timeout)
    if ok:
        logger.info("服务商未绑定，已提交宽带账号（service=%s）", service)
    else:
        logger.warning("宽带账号绑定失败（继续上线）: %s", detail)
    return token


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

    # 步骤 3.5：确保服务商已绑定宽带账号（未配置凭证时内部直接跳过）
    token = _ensure_binding(client, cfg, tokens, token, logger,
                            cfg.API_TIMEOUT)

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
