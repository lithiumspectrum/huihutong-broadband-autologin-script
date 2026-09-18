"""配置加载：INI 文件 + 环境变量覆盖。

配置文件默认 /etc/portal_login.conf（Windows 开发时可用 --config 指定），
同名环境变量（大写）可覆盖任意键，方便临时调试与 procd 注入。
"""

import os
import configparser


# 键 → (section, 默认值, 类型)
_DEFAULTS = {
    # [auth]
    # 凭证：手机号 + 登录认证码（loginByPhoneAndUid 换 satoken，API 字段名为 uid）。
    # 注意键名不能叫 UID——shell 里 UID 是只读内置变量，会污染环境变量覆盖。
    "PHONE":          ("auth", "", str),
    "USER_UID":       ("auth", "", str),
    "SERVICE_NAME":   ("auth", "chinaMobile", str),
    "CLIENT_ID":      ("auth", "6d6bc6f3b5f04107a5fc1c62e39dd5f4", str),
    "API_BASE":       ("auth", "https://api.215123.cn", str),
    # [daemon]
    "INTERVAL":       ("daemon", 30, int),            # 常态探测间隔（秒）
    "WATCH_INTERVAL": ("daemon", 5, int),             # 易断网时间窗内间隔
    "WATCH_WINDOWS":  ("daemon", "11:55-12:10", str), # 多个用逗号分隔
    "RETRY_BASE":     ("daemon", 2, int),             # 离线重试退避基数
    "RETRY_CAP":      ("daemon", 30, int),            # 退避上限
    "JITTER":         ("daemon", 0.2, float),         # ±20% 随机抖动
    "PROBE_TIMEOUT":  ("daemon", 10, int),
    "API_TIMEOUT":    ("daemon", 15, int),
    "PROBE_URL":      ("daemon", "http://connect.rom.miui.com/generate_204", str),
    "STATE_FILE":     ("daemon", "/tmp/portal_login/state.json", str),
    # OpenWrt 的 /var 是 tmpfs（重启清空）：断网历史放 /etc（overlay 持久化），
    # 每次断网只追加数行，flash 写入量可忽略；状态与 token 留 tmpfs，重启即弃更安全
    "OUTAGE_LOG":     ("daemon", "/etc/portal_login/outages.jsonl", str),
    "TOKEN_CACHE":    ("daemon", "/tmp/portal_login/token.json", str),
    "DETECT_ENABLED": ("daemon", True, bool),
}

_BOOL_TRUE = {"1", "yes", "true", "on", "y"}


def default_config_path():
    """返回默认配置路径。"""
    env = os.environ.get("PORTAL_LOGIN_CONF")
    if env:
        return env
    if os.name == "posix":
        return "/etc/portal_login.conf"
    return os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                        "portal_login", "portal_login.conf")


def _to_bool(value):
    return str(value).strip().lower() in _BOOL_TRUE


def parse_windows(text):
    """解析 'HH:MM-HH:MM,HH:MM-HH:MM' 为 [(datetime.time, datetime.time), ...]。"""
    from datetime import time
    windows = []
    for part in str(text or "").split(","):
        part = part.strip()
        if not part or "-" not in part:
            continue
        start_s, end_s = (x.strip() for x in part.split("-", 1))
        try:
            hh, mm = start_s.split(":")
            start = time(int(hh), int(mm))
            hh, mm = end_s.split(":")
            end = time(int(hh), int(mm))
        except (ValueError, TypeError):
            continue
        windows.append((start, end))
    return windows


class Config:
    """运行时配置（属性即大写键名）。"""

    def __init__(self, path=None):
        self.path = path or default_config_path()
        self._values = {}
        self.reload()

    def reload(self):
        cp = configparser.ConfigParser(interpolation=None)
        if self.path and os.path.isfile(self.path):
            cp.read(self.path, encoding="utf-8")

        values = {}
        for key, (section, default, caster) in _DEFAULTS.items():
            raw = default
            if cp.has_option(section, key):
                raw = cp.get(section, key)
            env_key = key
            if env_key in os.environ and os.environ[env_key] != "":
                raw = os.environ[env_key]
            try:
                if caster is bool:
                    values[key] = _to_bool(raw)
                else:
                    values[key] = caster(raw)
            except (ValueError, TypeError):
                values[key] = default
        self._values = values

    def __getattr__(self, name):
        if name.startswith("_") or name not in _DEFAULTS:
            raise AttributeError(name)
        return self._values[name]

    def in_watch_window(self, now=None):
        """判断给定时刻（默认本地现在）是否落在任一易断网时间窗。

        支持跨午夜窗口（如 23:50-00:10）。
        """
        now = (now or datetime_now()).time()
        windows = parse_windows(self._values["WATCH_WINDOWS"])
        if not windows:
            return False
        for start, end in windows:
            if start <= end:
                if start <= now <= end:
                    return True
            else:  # 跨午夜
                if now >= start or now <= end:
                    return True
        return False

    def describe_credential(self):
        if self._values["PHONE"] and self._values["USER_UID"]:
            return "手机号+UID(自动换新,无人值守)"
        return "未配置"


def datetime_now():
    """延迟导入 datetime，保持模块顶部轻量。"""
    from datetime import datetime
    return datetime.now()
