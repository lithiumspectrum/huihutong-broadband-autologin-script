"""手机号 + 认证码（API 字段名 uid）→ satoken 的换发与安全缓存。

satoken 不是用户配置项（扫码 JWT 路径已移除）：它只是
`POST /ac/auth/loginByPhoneAndUid` 签发的临时会话凭证，由守护进程
自动换发、缓存、失效时自动刷新。

缓存层级：
  1. 进程内存（主）
  2. 磁盘缓存（仅为进程重启后省一次换发）：目录 700 / 文件 600
"""

import json
import os
import tempfile

from .httpclient import HttpClientError


class AuthError(Exception):
    pass


class TokenManager:
    def __init__(self, cfg, client, logger):
        self._cfg = cfg
        self._client = client
        self._log = logger
        self._token = None

    # ------------------------------------------------------------------ 内部
    def _mint(self):
        url = "%s/ac/auth/loginByPhoneAndUid" % self._cfg.API_BASE.rstrip("/")
        # 与前端同构：三个字段全字符串；captchaKey 传空串即可（实测无验证码）
        body = json.dumps({"phone": self._cfg.PHONE,
                           "uid": self._cfg.USER_UID,
                           "captchaKey": ""})
        try:
            resp = self._client.post(url, body, timeout=self._cfg.API_TIMEOUT,
                                     headers={"Content-Type": "application/json"})
        except HttpClientError as exc:
            raise AuthError("loginByPhoneAndUid 网络失败: %s" % exc)

        token, error = _parse_token(resp)
        if not token:
            raise AuthError("loginByPhoneAndUid 未返回 token: %s" % error)
        return token

    def _load_disk(self):
        try:
            with open(self._cfg.TOKEN_CACHE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            token = data.get("token")
            if isinstance(token, str) and token:
                return token
        except (OSError, ValueError):
            pass
        return None

    def _save_disk(self, token):
        path = self._cfg.TOKEN_CACHE
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, mode=0o700, exist_ok=True)
            payload = json.dumps({"token": token}, ensure_ascii=False)
            # 同目录临时文件 + 原子替换，避免半写入
            fd, tmp = tempfile.mkstemp(prefix=".token.", dir=directory or ".")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        except OSError as exc:
            # 磁盘缓存失败不影响运行（内存仍可用）
            self._log.warning("token 磁盘缓存写入失败（忽略）: %s", exc)

    # ------------------------------------------------------------------ 对外
    def get_token(self, force_refresh=False):
        """获取可用 token；force_refresh 时强制调 loginByPhoneAndUid 换新。"""
        if not force_refresh:
            if self._token:
                return self._token
            disk = self._load_disk()
            if disk:
                self._token = disk
                return disk
        token = self._mint()
        self._token = token
        self._save_disk(token)
        self._log.info("已通过 手机号+认证码 换取新 satoken")
        return token

    def invalidate(self):
        """丢弃内存与磁盘缓存（401 后调用）。"""
        self._token = None
        try:
            os.remove(self._cfg.TOKEN_CACHE)
        except OSError:
            pass


def _parse_token(resp):
    """从 loginByPhoneAndUid 响应取 data.token。返回 (token, error)。"""
    if resp.status != 200:
        return None, "HTTP %s" % resp.status
    try:
        data = json.loads(resp.text)
    except ValueError:
        return None, "非 JSON 响应: %s" % resp.text[:200]
    if data.get("code") not in (200, "200"):
        return None, "code=%s message=%s" % (data.get("code"), data.get("message"))
    token = (data.get("data") or {}).get("token")
    if not token:
        return None, "data.token 缺失: %s" % resp.text[:200]
    return token, None
