# huihutong-broadband-autologin-script

慧湖通（独墅湖人才公寓）Web 门户认证的无人值守自动登录守护。
纯 Python 3 标准库实现（3.8+，零第三方依赖），面向 OpenWrt 软路由常驻运行。

仓库名 huihutong-broadband-autologin-script；Python 包 / 命令 / 服务名均为 `portal_login`，代码本体见 [portal_login/](./portal_login)。

适用网络：`api.215123.cn` 慧湖通门户（锐捷 eportal/RG-SAM 后台，微信 SSO 认证，
**非 802.1X**，mentohust/minieap 不适用）。

## 特性

- **无人值守**：以微信 openId 为唯一永久凭证，自动换发 satoken，401 自动作废重换并重试
- **每日强制下线自愈**：覆盖每日约 12:00 的 RADIUS 踢下线，最坏数秒内自动恢复
- **智能频率**：常态 30s 探测，易断网时间窗内（默认 11:55-12:10）自动加速到 5s；
  离线时指数退避 `min(2·2^(n-1), retry_cap, 档间隔)`（1s 下限）+ ±20% 抖动
- **断网记录**：JSONL 事件流（断网起止/时长/认证尝试次数）+ 实时状态文件，
  可直接喂给 jq / pandas 做统计分析；记录可随时开关且不影响自动认证
- **连接复用**：http.client keep-alive 长连接，对真实 API 实测比 curl 冷启动快约 3 倍
- **健壮**：单拍异常全捕获不退出、连接断线自动重建重试一次、SIGHUP 热重载配置、
  SIGTERM 优雅退出、配套 mock 全链路自测

## 工作原理

整个登录是"用 HTTP 请求让网关把当前源 IP 加白"，不涉及密码提交：

```
① GET http://connect.rom.miui.com/generate_204   三态探测（204 在线 / 302 或 200-JS跳转 未认证 / 网络异常离线）
② 跟随重定向链，提取 eportal login_sso.jsp 基址（作为 redirect_uri）
③ GET api.215123.cn/web-app/auth/certificateLogin?openId=<OPEN_ID>   换发 satoken(JWT)
④ GET api.215123.cn/ac/auth/oauthRedirect（satoken 头）              换一次性 code
⑤ GET <带 code 的 eportal URL>                                       放行源 IP
⑥ 复查 generate_204 = 204                                            确认上线
```

`oauthRedirect` 返回 401 时，自动用 openId 重新换发 token 并重试一次。
凭证的唯一作用是让 API 签发一次性 code，证明"该微信用户订购了对应运营商套餐"。

> 📄 接口原理、抓包事实、踩坑记录（给未来维护者/AI 的完整逆向档案）：
> **[PROTOCOL_NOTES.md](PROTOCOL_NOTES.md)**

## 依赖与安装

仅需 Python 3.8+ 标准库。OpenWrt 上 `python3-light` + `ca-bundle` 即可：

```sh
# OpenWrt 23.05（opkg）
opkg update && opkg install python3-light ca-bundle
# OpenWrt 24.10+（apk）
apk update && apk add python3 ca-bundle

# 自检标准库齐全
python3 -c "import http.client, ssl, json, logging, configparser"
```

把 `portal_login/` 目录放到任意位置，以其**上级目录**为 `PYTHONPATH` 运行：

```sh
export PYTHONPATH=/root/portal_login   # 假设包位于 /root/portal_login/portal_login/
python3 -m portal_login <命令>
```

## 获取 OPEN_ID（唯一需要手动获取的凭证）

openId 是微信用户在该小程序下的永久标识，获取一次永久有效：

1. PC 安装 Fiddler，开启 HTTPS 解密并信任根证书
2. 微信 PC 版打开"慧湖通"小程序，进入"我的"页
3. 在抓包列表找 `api.215123.cn/web-app/auth/certificateLogin?openId=xxxx`，复制 `openId` 参数
   （`o` 开头约 28 字符；`unionId`/`account` 参数均不需要）
4. 验证（该接口安全，只发新 token 不踢会话）：

   ```sh
   curl -s "https://api.215123.cn/web-app/auth/certificateLogin?openId=<你的OpenID>"
   # 返回 "code":200 且含 "token":"eyJ..." 即有效
   ```

> 🛠 仓库 `scripts/` 下有 PowerShell 抓包辅助脚本（`capture.ps1` / `capture_sso.ps1` / `parse_har.ps1`），平台改版重逆时可参考。

> 🔒 openId 等同上网密码：配置文件 `chmod 600`，勿提交 git、勿截图外发。

## 配置

默认读取 `/etc/portal_login.conf`（可用 `--config` 或环境变量 `PORTAL_LOGIN_CONF` 指定），
同名大写环境变量可覆盖任意配置项。完整模板见 [`examples/portal_login.conf.example`](examples/portal_login.conf.example)，部署时本地复制填入 `OPEN_ID` 后上传（流程见下文「OpenWrt 部署」）：

```ini
[auth]
open_id = 你的OpenID
service_name = chinaMobile      ; chinaMobile/chinaTelecom/chinaUnicom/local

[daemon]
interval = 30                   ; 常态探测间隔（秒）
watch_windows = 11:55-12:10     ; 易断网时间窗，逗号分隔多个，支持跨午夜
watch_interval = 5              ; 时间窗内探测间隔（秒）
```

| 键 | 段 | 默认值 | 说明 |
|---|---|---|---|
| `open_id` | auth | —（必填） | 微信永久标识 |
| `service_name` | auth | `chinaMobile` | 运营商 |
| `client_id` | auth | `6d6bc6f3b5f04107a5fc1c62e39dd5f4` | 实测固定值，不用改 |
| `api_base` | auth | `https://api.215123.cn` | 改版重逆时可指向 mock |
| `interval` / `watch_interval` | daemon | `30` / `5` | 探测间隔（常态/时间窗内） |
| `watch_windows` | daemon | `11:55-12:10` | 易断网时间窗 |
| `retry_base` / `retry_cap` | daemon | `2` / `30` | 离线重试退避基数/上限 |
| `jitter` | daemon | `0.2` | ±20% 随机抖动 |
| `probe_timeout` / `api_timeout` | daemon | `10` / `15` | 超时秒数 |
| `probe_url` | daemon | miui 204 地址 | 备选 `http://www.gstatic.com/generate_204` |
| `detect_enabled` | daemon | `yes` | 断网**记录**总开关（不影响自动认证） |
| `state_file` | daemon | `/tmp/portal_login/state.json` | 实时状态（tmpfs） |
| `outage_log` | daemon | `/etc/portal_login/outages.jsonl` | 断网历史（持久化） |
| `token_cache` | daemon | `/tmp/portal_login/token.json` | token 缓存（tmpfs，0600） |

## 命令

```sh
python3 -m portal_login daemon      # 守护主循环（前台运行，适合 systemd/procd 托管）
python3 -m portal_login once        # 只跑一拍：在线退 0，离线立即登录，成功 0/失败 1
python3 -m portal_login status      # 实时状态 JSON
python3 -m portal_login detect off  # 关闭断网记录（照常认证，适合割接演练）
python3 -m portal_login selftest    # 本机 mock 全链路自测，16 项断言
```

> `selftest` 依赖仓库中的 `tools/mock_portal.py`（模拟整条门户链路），
> 单独拷贝 `portal_login/` 包时该命令不可用，其余命令不受影响。

## 断网记录

`/etc/portal_login/outages.jsonl`，UTF-8 JSONL，一次完整断网产生 3 行：

```json
{"event": "offline_start", "ts": "2026-09-16T12:00:03+08:00", "probe": "redirect(302) -> http://...", "http_code": 302}
{"event": "online_restored", "ts": "2026-09-16T12:00:09+08:00", "after_attempts": 1}
{"event": "outage_summary", "start": "...", "end": "...", "duration_s": 6.12, "attempts": 1, "recovery": "portal_login"}
```

分析示例：`jq -r 'select(.event=="outage_summary") | "\(.start)  \(.duration_s)s"' /etc/portal_login/outages.jsonl`

实时状态在 `state.json`（当前状态、离线已持续秒数、认证尝试次数、最近在线时间），
`status` 命令直接输出。

## 架构

```
__main__.py    CLI: daemon | once | status | detect on/off | selftest
config.py      INI + 环境变量覆盖 + 时间窗判定（支持跨午夜）
httpclient.py  http.client 长连接：按主机缓存、跟随 3xx、断线重建重试一次、天然禁代理
detector.py    三态探测 → ProbeResult(online/code/redirect/reason)
auth.py        TokenManager：内存 + tmpfs 磁盘缓存(0600) + 401 作废重换
portal.py      提取 eportal 基址 → oauthRedirect(401 重试) → GET 放行 → 204 复核
recorder.py    JSONL 事件 + state.json + 运行时控制文件（原子写入）
scheduler.py   主循环：时间窗换档、当拍立即登录、指数退避、抖动、信号处理
```

设计约束：顺序轮询无并发；守护绝不为单拍异常退出；token 永不出现在日志中；
禁用代理环境变量（认证流量必须 WAN 直连，否则 eportal 内网地址不可达/源 IP 不匹配）。

## OpenWrt 部署（procd 托管）

```sh
# ① 上传代码与 init 服务脚本（电脑上，仓库根目录执行）
ssh root@192.168.1.1 'mkdir -p /root/portal_login'
scp -r portal_login tools root@192.168.1.1:/root/portal_login/
scp openwrt_portal_login.init root@192.168.1.1:/etc/init.d/portal_login
```

**部署后预期布局**（`scp -r` 到已存在的 `/root/portal_login/` 会自动形成子目录，注意 `portal_login/portal_login/` 是两层）：

```
/root/portal_login/
├── portal_login/      ← Python 包（PYTHONPATH 指向其父目录，即 /root/portal_login）
│   ├── __init__.py
│   ├── __main__.py
│   └── ...（共 9 个模块）
└── tools/            ← selftest 依赖的 mock_portal.py
```

> 若启动报 `No module named portal_login`，几乎都是少了一层。自检：
>
> ```sh
> ls /root/portal_login/portal_login/__init__.py
> ```

```sh
# ② 本地复制模板 → 填入 open_id → 上传（含凭证的本地副本用完即删）
cp examples/portal_login.conf.example portal_login.conf
vi portal_login.conf
scp portal_login.conf root@192.168.1.1:/etc/portal_login.conf
rm portal_login.conf

# ③ 路由器上：设权限、注册开机自启并启动
ssh root@192.168.1.1 'chmod 600 /etc/portal_login.conf && \
  chmod +x /etc/init.d/portal_login && \
  /etc/init.d/portal_login enable && \
  /etc/init.d/portal_login start'
```

部署后可选自测（全程本机 mock，不接触真实门户）：

```sh
ssh root@192.168.1.1 'cd /root/portal_login && PYTHONPATH=. python3 -m portal_login selftest'
```

procd 服务要点：`command /usr/bin/python3 -m portal_login daemon`、
`env PYTHONPATH=/root/portal_login`、`respawn` 崩溃重启、
reload 用 `procd_send_signal <服务名> '*' HUP` 发 SIGHUP 热重载。
完整 init 脚本见仓库根目录 `openwrt_portal_login.init`。

数据落盘约定：`/tmp`（tmpfs）放 state/token/控制文件，重启即弃；
`/etc`（overlay）放 outages.jsonl，每次断网仅追加约 3 行，无 flash 磨损担忧。

## ⚠️ 安全注意

- **绝不调用** `api.215123.cn/ac/sso/logout` 或 `/ac/auth/logout` 做探测——
  GET 即注销，会吊销 token 并可能立刻踢断整机网络。判在线只用 generate_204。
- `certificateLogin` 是安全登录接口（只发新 token，不踢会话），可放心重试。
- 配置文件与 token 缓存均 0600；token 缓存只落 tmpfs。

## 致谢

- [PairZhu/HuiHuTong](https://github.com/PairZhu/HuiHuTong) — openId 抓包方法来源
- [Dustella/Huihutong-portal-login](https://github.com/Dustella/Huihutong-portal-login) — 同源链路实现（注意其硬编码 `chinaTelecom`）

## License

MIT，见 [LICENSE](LICENSE)。