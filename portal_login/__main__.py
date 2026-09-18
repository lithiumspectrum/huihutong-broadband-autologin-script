"""命令行入口：python -m portal_login {daemon|once|status|detect|selftest}"""

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile

# 保证从任意目录运行都能 import 到同级 tools/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .config import Config
from .recorder import Recorder, read_state
from .scheduler import Daemon


def _basic_logger():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    return logging.getLogger("portal_login")


def cmd_daemon(args):
    cfg = Config(args.config)
    return Daemon(cfg, once=False).run()


def cmd_once(args):
    cfg = Config(args.config)
    return Daemon(cfg, once=True).run()


def cmd_status(args):
    cfg = Config(args.config)
    state = read_state(cfg)
    if not state:
        print("暂无状态数据（守护可能尚未运行过）：%s" % cfg.STATE_FILE)
        return 1
    # 叠加运行时控制文件的实际开关
    rec = Recorder(cfg, _basic_logger())
    state["detection_enabled"] = rec.refresh_control()
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def cmd_detect(args):
    cfg = Config(args.config)
    enabled = args.state == "on"
    rec = Recorder(cfg, _basic_logger())
    rec.set_control(enabled)
    print("断网检测已%s（控制文件：%s）"
          % ("开启" if enabled else "关闭", cfg.STATE_FILE + ".control.json"))
    return 0


# --------------------------------------------------------------------- selftest

def _selftest_config(tmpdir, base, detect_enabled=True, alert_after=10):
    """写临时 INI 并返回 Config（同时屏蔽机器上的环境变量覆盖）。"""
    saved = {}
    for key in ("PHONE", "USER_UID", "SERVICE_NAME", "CLIENT_ID", "API_BASE",
                "INTERVAL", "WATCH_INTERVAL", "WATCH_WINDOWS", "PROBE_URL",
                "STATE_FILE", "OUTAGE_LOG", "TOKEN_CACHE", "DETECT_ENABLED",
                "ALERT_AFTER", "ALERT_LOG"):
        if key in os.environ:
            saved[key] = os.environ.pop(key)

    path = os.path.join(tmpdir, "portal_login.conf")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "[auth]\n"
            "phone = mock-phone\n"
            "user_uid = mock-uid\n"
            "service_name = chinaMobile\n"
            "api_base = %s\n"
            "[daemon]\n"
            "interval = 1\n"
            "watch_windows =\n"
            "probe_url = %s/generate_204\n"
            "state_file = %s/state.json\n"
            "outage_log = %s/outages.jsonl\n"
            "token_cache = %s/token.json\n"
            "alert_after = %d\n"
            "alert_log = %s/alerts.jsonl\n"
            "detect_enabled = %s\n"
            % (base, base, tmpdir, tmpdir, tmpdir, alert_after, tmpdir,
               "yes" if detect_enabled else "no"))
    return Config(path), saved


def _restore_env(saved):
    for key, value in saved.items():
        os.environ[key] = value


def _read_jsonl(path):
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def cmd_selftest(args):
    from tools.mock_portal import start_mock

    results = []

    def check(name, cond, detail=""):
        results.append((name, cond, detail))
        print(("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, detail)))

    saved_env = {}
    try:
        # 场景 1：已在线 → 无动作、无记录
        tmp = tempfile.mkdtemp(prefix="pl_test_online_")
        base, state, server = start_mock("online")
        try:
            cfg, saved_env = _selftest_config(tmp, base)
            d = Daemon(cfg)
            d._log.setLevel(logging.ERROR)
            ok = d.tick()
            check("①已在线：tick 判定在线", ok is True)
            check("①已在线：不产生断网记录",
                  not os.path.isfile(cfg.OUTAGE_LOG))
        finally:
            server.shutdown()
            _restore_env(saved_env)
            shutil.rmtree(tmp, ignore_errors=True)

        # 场景 2：完整链路 302→SSO→换token→oauth→eportal→204
        tmp = tempfile.mkdtemp(prefix="pl_test_chain_")
        base, state, server = start_mock("chain")
        try:
            cfg, saved_env = _selftest_config(tmp, base)
            d = Daemon(cfg)
            d._log.setLevel(logging.ERROR)
            ok = d.tick()
            events = _read_jsonl(cfg.OUTAGE_LOG)
            names = [e["event"] for e in events]
            check("②全链路：单拍内上线", ok is True)
            check("②全链路：loginByPhoneAndUid 调用 1 次",
                  state.mint_count == 1)
            check("②全链路：换 token 请求体含 phone/uid",
                  state.last_auth_body == {"phone": "mock-phone",
                                           "uid": "mock-uid",
                                           "captchaKey": ""})
            check("②全链路：oauthRedirect 调用 1 次", state.oauth_calls == 1)
            check("②全链路：记录 offline_start", "offline_start" in names)
            check("②全链路：记录 outage_summary 且时长>=0",
                  any(e.get("event") == "outage_summary"
                      and e.get("duration_s", -1) >= 0 for e in events))
            check("②全链路：outage_summary 尝试次数=1",
                  any(e.get("event") == "outage_summary"
                      and e.get("attempts") == 1 for e in events))
            if os.name == "posix":
                check("②全链路：token 磁盘缓存已落盘(0600)",
                      os.path.isfile(cfg.TOKEN_CACHE)
                      and (os.stat(cfg.TOKEN_CACHE).st_mode & 0o777) == 0o600)
            else:
                # Windows 无 Unix 权限位，只验证落盘；0600 在 OpenWrt 部署时生效
                check("②全链路：token 磁盘缓存已落盘",
                      os.path.isfile(cfg.TOKEN_CACHE))
            ok2 = d.tick()
            check("②全链路：第二拍仍在线", ok2 is True)
        finally:
            server.shutdown()
            _restore_env(saved_env)
            shutil.rmtree(tmp, ignore_errors=True)

        # 场景 3：oauth 首次 401 → 重新换发 token 重试成功
        tmp = tempfile.mkdtemp(prefix="pl_test_401_")
        base, state, server = start_mock("401")
        try:
            cfg, saved_env = _selftest_config(tmp, base)
            d = Daemon(cfg)
            d._log.setLevel(logging.ERROR)
            ok = d.tick()
            check("③401自愈：重试后上线", ok is True)
            check("③401自愈：token 换发 2 次", state.mint_count == 2)
            check("③401自愈：oauth 调用 2 次", state.oauth_calls == 2)
        finally:
            server.shutdown()
            _restore_env(saved_env)
            shutil.rmtree(tmp, ignore_errors=True)

        # 场景 4：运行时控制文件关闭检测 → 照常认证但不写断网记录
        tmp = tempfile.mkdtemp(prefix="pl_test_off_")
        base, state, server = start_mock("chain")
        try:
            cfg, saved_env = _selftest_config(tmp, base, detect_enabled=True)
            d = Daemon(cfg)
            d._log.setLevel(logging.ERROR)
            d.recorder.set_control(False)  # 等同 `detect off`
            ok = d.tick()
            snap = read_state(cfg)
            check("④检测关闭：认证动作仍成功", ok is True)
            check("④检测关闭：无断网记录文件",
                  not os.path.isfile(cfg.OUTAGE_LOG))
            check("④检测关闭：状态文件标注 detection_enabled=false",
                  snap and snap.get("detection_enabled") is False)
        finally:
            server.shutdown()
            _restore_env(saved_env)
            shutil.rmtree(tmp, ignore_errors=True)

        # 场景 5：改凭证后内存与磁盘 token 缓存都必须失效
        # （守护改配置走 SIGHUP reload，进程内 token 若不作废会静默用旧账号）
        tmp = tempfile.mkdtemp(prefix="pl_test_cred_")
        base, state, server = start_mock("online")
        try:
            from .auth import TokenManager
            cfg, saved_env = _selftest_config(tmp, base)
            d = Daemon(cfg)
            d._log.setLevel(logging.ERROR)
            tm = TokenManager(cfg, d.client, d._log)
            tm.get_token()                                     # 旧凭证换一次
            check("⑤改凭证：磁盘缓存已落盘", os.path.isfile(cfg.TOKEN_CACHE))
            check("⑤改凭证：同凭证二次取用命中缓存", state.mint_count == 1)

            os.environ["USER_UID"] = "mock-uid-changed"        # 模拟改认证码
            cfg.reload()
            tm.get_token()
            check("⑤改凭证：内存 token 作废并重新换发", state.mint_count == 2)

            # 新建实例只读磁盘，也必须拒绝旧指纹
            os.environ["USER_UID"] = "mock-uid-changed-again"
            cfg.reload()
            TokenManager(cfg, d.client, d._log).get_token()
            check("⑤改凭证：磁盘缓存旧指纹被拒并重换", state.mint_count == 3)
            with open(cfg.TOKEN_CACHE, encoding="utf-8") as fh:
                check("⑤改凭证：缓存已按新凭证重写",
                      json.load(fh).get("token") == "mock-token-3")
        finally:
            os.environ.pop("USER_UID", None)
            server.shutdown()
            _restore_env(saved_env)
            shutil.rmtree(tmp, ignore_errors=True)

        # 场景 6：连续认证失败达阈值 → 往持久化 ALERT_LOG 落一条（且只落一条）
        tmp = tempfile.mkdtemp(prefix="pl_test_alert_")
        base, state, server = start_mock("authfail")
        try:
            cfg, saved_env = _selftest_config(tmp, base, alert_after=2)
            d = Daemon(cfg)
            d._log.setLevel(logging.CRITICAL)
            d.tick()
            check("⑥连续失败：未达阈值不写告警", not os.path.isfile(cfg.ALERT_LOG))
            d.tick()
            d.tick()
            alerts = _read_jsonl(cfg.ALERT_LOG)
            check("⑥连续失败：达到阈值后写入持久化告警", len(alerts) == 1)
            check("⑥连续失败：告警含失败阶段与尝试次数（且只落一条）",
                  alerts and alerts[0].get("stage") == "token"
                  and alerts[0].get("attempts") == 2)
        finally:
            server.shutdown()
            _restore_env(saved_env)
            shutil.rmtree(tmp, ignore_errors=True)

    finally:
        _restore_env(saved_env)

    failed = [n for n, ok, _ in results if not ok]
    print("\nselftest %s：%d/%d 通过"
          % ("全部通过" if not failed else "存在失败",
             len(results) - len(failed), len(results)))
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m portal_login",
        description="慧湖通门户自动认证守护 v5（手机号+认证码 无人值守）")
    parser.add_argument("--config", help="配置文件路径（默认 /etc/portal_login.conf）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("daemon", help="启动守护进程")
    sub.add_parser("once", help="只执行一次检测/认证")
    sub.add_parser("status", help="查看实时状态（JSON）")

    p_detect = sub.add_parser("detect", help="开启/关闭断网检测记录")
    p_detect.add_argument("state", choices=["on", "off"])

    sub.add_parser("selftest", help="本机模拟链路自测（无需宿舍网）")

    args = parser.parse_args(argv)
    return {
        "daemon": cmd_daemon,
        "once": cmd_once,
        "status": cmd_status,
        "detect": cmd_detect,
        "selftest": cmd_selftest,
    }[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
