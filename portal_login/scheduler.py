"""守护主循环：智能频率状态机。

频率策略：
  - 当前时间落在 WATCH_WINDOWS（默认 11:55-12:10）→ WATCH_INTERVAL（默认 5s）
  - 其余时间 → INTERVAL（默认 30s）
  - 离线时重试间隔 = min(RETRY_BASE * 2**attempts, 当前档间隔)，叠加 ±JITTER
  - 检测到离线的当拍立即发起认证，不等到下一拍
  - 抖动避免多设备/整点齐刷；RETRY_BASE 为请求下限，避免风暴

信号：
  SIGTERM/SIGINT 优雅退出（procd stop）
  SIGHUP（POSIX）热重载配置（procd reload）
"""

import logging
import random
import signal
import time

from .auth import TokenManager
from .detector import probe
from .httpclient import HttpClient
from .portal import perform_login
from .recorder import Recorder


class Daemon:
    def __init__(self, cfg, once=False):
        self.cfg = cfg
        self.once = once
        self._stop = False
        self._reload = False
        self._log = self._build_logger()
        self.client = HttpClient()
        self.recorder = Recorder(cfg, self._log)
        self.tokens = TokenManager(cfg, self.client, self._log)

    @staticmethod
    def _build_logger():
        logger = logging.getLogger("portal_login")
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(
                "[%(asctime)s] %(levelname)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"))
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        return logger

    # ------------------------------------------------------------- 信号
    def _install_signals(self):
        signal.signal(signal.SIGTERM, self._on_stop)
        signal.signal(signal.SIGINT, self._on_stop)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, self._on_reload)

    def _on_stop(self, signum, frame):
        self._stop = True

    def _on_reload(self, signum, frame):
        self._reload = True

    # ------------------------------------------------------------- 频率
    def _jittered(self, seconds):
        ratio = self.cfg.JITTER
        if ratio > 0:
            seconds *= 1.0 + random.uniform(-ratio, ratio)
        return max(1.0, seconds)

    def _next_sleep(self, offline_attempts):
        base = (self.cfg.WATCH_INTERVAL
                if self.cfg.in_watch_window() else self.cfg.INTERVAL)
        if offline_attempts <= 0:
            return self._jittered(base)
        backoff = min(self.cfg.RETRY_BASE * (2 ** max(0, offline_attempts - 1)),
                      self.cfg.RETRY_CAP, base)
        return self._jittered(backoff)

    def _interruptible_sleep(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self._stop and not self._reload:
            time.sleep(min(0.5, deadline - time.monotonic()))

    # ------------------------------------------------------------- 单拍
    def tick(self):
        """一个探测-认证周期。返回 True=在线，False=离线。"""
        self.recorder.refresh_control()
        result = probe(self.client, self.cfg.PROBE_URL, self.cfg.PROBE_TIMEOUT)

        if result.online:
            self.recorder.mark_online()
            return True

        self.recorder.mark_offline(result.detail(), result.code)
        login = perform_login(self.cfg, self.client, self.tokens, self._log)
        self.recorder.note_attempt(login)
        # 立即复核，成功则在同一拍内完成 offline→online 转换并写出 outage_summary
        verify = probe(self.client, self.cfg.PROBE_URL, self.cfg.PROBE_TIMEOUT)
        if verify.online:
            self.recorder.mark_online()
            return True
        return False

    # ------------------------------------------------------------- 运行
    def run(self):
        if not self.cfg.OPEN_ID:
            self._log.error("未配置 OPEN_ID，请在 %s 的 [auth] 段设置", self.cfg.path)
            return 2

        self._install_signals()
        window_note = ("，易断网时间窗 %s 内 %ds/拍"
                       % (self.cfg.WATCH_WINDOWS, self.cfg.WATCH_INTERVAL)
                       if self.cfg.WATCH_WINDOWS else "")
        self._log.info(
            "守护启动 v5（常态 %ds/拍%s，凭证=%s）",
            self.cfg.INTERVAL, window_note, self.cfg.describe_credential())

        failures = 0
        while not self._stop:
            try:
                online = self.tick()
            except Exception as exc:  # 守护绝不为单拍异常退出，procd respawn 兜底
                self._log.exception("周期异常（已捕获）：%s", exc)
                online = False

            if self.once:
                return 0 if online else 1

            failures = 0 if online else failures + 1
            self._interruptible_sleep(self._next_sleep(failures))

            if self._reload:
                self._reload = False
                try:
                    self.cfg.reload()
                    self.recorder.detection_enabled = self.cfg.DETECT_ENABLED
                    self._log.info("配置已热重载")
                except Exception as exc:
                    self._log.error("配置重载失败（沿用旧配置）：%s", exc)

        self._log.info("收到退出信号，守护停止")
        self.client.close()
        return 0
