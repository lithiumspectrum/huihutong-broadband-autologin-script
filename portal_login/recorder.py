"""断网检测记录器。

产出两类数据：
  1. JSONL 事件流（OUTAGE_LOG）：offline_start / online_restored / outage_summary，
     每行一个 JSON，便于 jq / pandas 直接做数据分析；
  2. 实时状态文件（STATE_FILE）：当前在线状态、本次离线起始时间与持续秒数、
     最近成功时间、检测开关状态——`status` 命令直接读取展示。

检测总开关：
  - 配置 DETECT_ENABLED；
  - 运行时由 CLI `detect on|off` 写控制文件 <STATE_FILE>.control.json，
    守护进程每拍检查（stat 开销可忽略），优先级高于配置。
"""

import json
import os
import tempfile
from datetime import datetime

STATE_ONLINE = "online"
STATE_OFFLINE = "offline"
STATE_STARTING = "starting"

CONTROL_SUFFIX = ".control.json"


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_iso(value):
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


class Recorder:
    def __init__(self, cfg, logger):
        self._cfg = cfg
        self._log = logger
        self.state = STATE_STARTING
        self.offline_since = None       # datetime
        self.attempts = 0
        self.last_online = None         # iso str
        self.last_error = None
        self.detection_enabled = cfg.DETECT_ENABLED
        self._jsonl_failed = False      # 日志路径不可写时只降级一次告警

    # ------------------------------------------------------------- 开关
    def refresh_control(self):
        """读取运行时控制文件，返回当前检测是否启用。"""
        path = self._cfg.STATE_FILE + CONTROL_SUFFIX
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.detection_enabled = bool(data.get("enabled", True))
        except (OSError, ValueError):
            self.detection_enabled = self._cfg.DETECT_ENABLED
        return self.detection_enabled

    def set_control(self, enabled):
        path = self._cfg.STATE_FILE + CONTROL_SUFFIX
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        payload = json.dumps(
            {"enabled": enabled, "changed_at": now_iso()},
            ensure_ascii=False)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(payload)

    # ------------------------------------------------------------- 事件
    def mark_online(self):
        """本拍在线。返回 True 表示发生了 offline→online 的恢复转换。"""
        restored = False
        if self.state == STATE_OFFLINE and self.offline_since:
            restored = True
            end = datetime.now().astimezone()
            duration = (end - self.offline_since).total_seconds()
            if self.detection_enabled:
                self._append({
                    "event": "online_restored",
                    "ts": now_iso(),
                    "after_attempts": self.attempts,
                })
                self._append({
                    "event": "outage_summary",
                    "start": self.offline_since.isoformat(timespec="seconds"),
                    "end": end.isoformat(timespec="seconds"),
                    "duration_s": round(duration, 2),
                    "attempts": self.attempts,
                    "recovery": "portal_login",
                })
            self._log.info("网络已恢复（离线 %.1f 秒，尝试 %d 次）",
                           duration, self.attempts)
        self.state = STATE_ONLINE
        self.offline_since = None
        self.attempts = 0
        self.last_online = now_iso()
        self.last_error = None
        self._write_state()
        return restored

    def mark_offline(self, reason, http_code=None):
        """本拍离线（首次转换时记录 offline_start）。"""
        if self.state != STATE_OFFLINE:
            self.state = STATE_OFFLINE
            self.offline_since = datetime.now().astimezone()
            self.attempts = 0
            if self.detection_enabled:
                self._append({
                    "event": "offline_start",
                    "ts": now_iso(),
                    "probe": reason,
                    "http_code": http_code,
                })
            self._log.warning("检测到断网：%s", reason)
        self.last_error = reason
        self._write_state()

    def note_attempt(self, result):
        """记录一次上线尝试结果（日志 + 状态文件 last_error）。

        attempts 在每个离线周期内按登录尝试次数累计（每拍一次）。
        """
        self.attempts += 1
        if result.success:
            self._log.info("上线动作成功：%s", result.message)
        else:
            self.last_error = "%s: %s" % (result.stage, result.message)
            self._log.warning("上线尝试失败[%s]：%s", result.stage, result.message)
            self._write_state()

    # ------------------------------------------------------------- 输出
    def _append(self, record):
        if self._jsonl_failed:
            return
        try:
            path = self._cfg.OUTAGE_LOG
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            self._jsonl_failed = True
            self._log.warning("断网日志无法写入 %s（后续仅输出 syslog）：%s",
                              self._cfg.OUTAGE_LOG, exc)

    def _write_state(self):
        elapsed = None
        if self.state == STATE_OFFLINE and self.offline_since:
            elapsed = (datetime.now().astimezone() - self.offline_since).total_seconds()
        snapshot = {
            "state": self.state,
            "updated": now_iso(),
            "detection_enabled": self.detection_enabled,
            "offline_since": (self.offline_since.isoformat(timespec="seconds")
                              if self.offline_since else None),
            "offline_elapsed_s": round(elapsed, 1) if elapsed is not None else None,
            "attempts": self.attempts,
            "last_online": self.last_online,
            "last_error": self.last_error,
        }
        try:
            path = self._cfg.STATE_FILE
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".state.", dir=directory or ".")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(snapshot, ensure_ascii=False, indent=2))
            os.replace(tmp, path)
        except OSError as exc:
            self._log.debug("状态文件写入失败（忽略）：%s", exc)


def read_state(cfg):
    """读取状态文件供 status 命令使用。"""
    try:
        with open(cfg.STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None
