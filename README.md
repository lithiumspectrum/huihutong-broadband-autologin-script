# 慧湖通门户自动认证（portal-login v5 · Python）

> 适用网络：独墅湖人才公寓 / 慧湖通 Web 门户认证（`api.215123.cn`，锐捷 eportal/RG-SAM 后台）
> 部署目标：OpenWrt 软路由（x86_64），**纯 Python 3 标准库实现，零 pip 依赖**
> 核心能力：OPEN_ID 唯一凭证自动换发 token、401 自愈；**断网事件记录与分析**；
> **易断网时间窗智能加速探测**；覆盖每日 12:00 强制踢下线的完全无人值守。

> 📄 接口原理、抓包事实、踩坑记录（给未来维护者/AI 的完整逆向档案）：
> **[PROTOCOL_NOTES.md](PROTOCOL_NOTES.md)**

---

## 目录

1. [v5 相对 shell v4 新增了什么](#1-v5-相对-shell-v4-新增了什么)
2. [文件清单与模块职责](#2-文件清单与模块职责)
3. [获取 OPEN_ID（唯一需要手动获取的凭证）](#3-获取-open_id唯一需要手动获取的凭证)
4. [部署到 OpenWrt](#4-部署到-openwrt)
5. [配置文件详解](#5-配置文件详解)
6. [CLI 命令](#6-cli-命令)
7. [断网记录与状态（数据分析格式）](#7-断网记录与状态数据分析格式)
8. [智能频率控制](#8-智能频率控制)
9. [satoken 安全存储与自动刷新](#9-satoken-安全存储与自动刷新)
10. [性能：为什么选 Python（实测数据）](#10-为什么选-python实测数据)
11. [在 Windows/Ubuntu 上手动运行](#11-在-windowsubuntu-上手动运行)
12. [常见问题](#12-常见问题)
13. [参考项目](#13-参考项目)

---

## 1. v5 相对 shell v4 新增了什么

| 能力 | shell v4（已删除） | **Python v5（当前）** |
|---|---|---|
| 凭证 | OPEN_ID 优先、SA_TOKEN 后备 | **OPEN_ID 唯一凭证**，SA_TOKEN 输入路径已全部移除 |
| 断网记录 | 无 | JSONL 事件流（起/止时间、持续时长、尝试次数）+ 实时状态文件 |
| 检测开关 | 无 | `detect on/off` 随时开关，**不影响自动认证**（关记录仍会自动上线） |
| 频率策略 | 固定 30s | 时间窗 5s / 常态 30s + 离线指数退避 + ±20% 抖动 |
| 快速恢复 | 下一拍才登录 | 发现离线**当拍立即登录**，同拍复核 |
| 连接效率 | 每轮 fork curl，全量 TLS 握手 | 常驻进程 keep-alive，HTTPS 请求快约 3 倍（实测） |
| 模块/测试 | 单文件 sh，不能跑测试 | 8 个职责单一的模块 + mock 链路自测（16 项断言） |

## 2. 文件清单与模块职责

```
portal-login/
├── portal_login/              # Python 包（纯标准库，Python 3.8+）
│   ├── __main__.py            # CLI：daemon/once/status/detect/selftest
│   ├── config.py              # INI 配置 + 环境变量覆盖 + 时间窗判断
│   ├── httpclient.py          # http.client 长连接封装、超时、断线重建、禁代理
│   ├── detector.py            # captive portal 三态检测（204/302/200-JS）
│   ├── portal.py              # 重定向链解析 + oauthRedirect + eportal 放行
│   ├── auth.py                # OPEN_ID→satoken 换发、内存/磁盘缓存、401 刷新
│   ├── recorder.py            # 断网 JSONL 记录 + 状态文件 + 检测开关
│   └── scheduler.py           # 主循环：时间窗 + 指数退避 + 信号(SIGHUP/SIGTERM)
├── tools/
│   └── mock_portal.py         # 本机 mock 整个门户链路，selftest 用（无需宿舍网）
├── openwrt_portal_login.init  # procd 服务文件（开机自启/崩溃重启/热重载）
├── README.md                  # 本文件
├── PROTOCOL_NOTES.md          # 逆向技术档案
├── captures/                  # 2026-09 抓包证据（接口事实来源）
├── reference/                 # 第三方脚本（仅参考）
└── capture*.ps1 / parse_har.ps1   # Windows 抓包诊断工具，平台改版重逆时用
```

## 3. 获取 OPEN_ID（唯一需要手动获取的凭证）

1. 电脑安装 Fiddler（[官网](https://www.telerik.com/fiddler)），启用 HTTPS 解密：
   `Tools → Options → HTTPS` → 勾选 *Decrypt HTTPS traffic* →
   `Actions → Trust Root Certificate` 信任根证书，重启 Fiddler
2. 登录**微信 PC 版**，打开 **慧湖通小程序**，进入“我的”确认已登录
3. 在 Fiddler 请求列表找到：
   ```
   https://api.215123.cn/web-app/auth/certificateLogin?openId=xxxx&unionId=xxxx
   ```
4. 复制 `openId=` 到 `&unionId` 之间的内容（`o` 开头约 28 字符）。
   接口只认 `openId`，`unionId`/`account` 都不需要。
5. 验证（任意网络都可，该接口安全，只发新 token 不踢会话）：
   ```powershell
   curl.exe -s "https://api.215123.cn/web-app/auth/certificateLogin?openId=你的OpenID"
   ```
   返回 `"code":200` 且含 `"token":"eyJ..."` 即有效。

> 🔒 OPEN_ID 等同上网密码：配置文件 `chmod 600`，勿截图、勿提交 git。

## 4. 部署到 OpenWrt

### 4.1 安装 Python 运行时

```sh
# OpenWrt 23.05（opkg）
opkg update && opkg install python3-light ca-bundle
# OpenWrt 24.10+ / 新版 ImmortalWrt（apk）
# apk update && apk add python3 ca-bundle

# 自检标准库是否齐全（缺什么 opkg install 对应的 python3-xxx 即可）
python3 -c "import http.client, ssl, json, logging, configparser"
```

### 4.2 上传代码（电脑上，在本目录执行）

```bash
ssh root@192.168.1.1 'mkdir -p /usr/local/lib/portal_login'
scp -r portal_login tools root@192.168.1.1:/usr/local/lib/portal_login/
scp openwrt_portal_login.init root@192.168.1.1:/etc/init.d/portal_login
```

### 4.3 创建配置并注册服务（路由器上）

```sh
chmod +x /etc/init.d/portal_login

cat > /etc/portal_login.conf << 'EOF'
[auth]
open_id = 你的OpenID

[daemon]
watch_windows = 11:55-12:10
EOF
chmod 600 /etc/portal_login.conf

/etc/init.d/portal_login enable
/etc/init.d/portal_login start
```

### 4.4 部署后自测（强烈建议，免踩坑）

```sh
cd /usr/local/lib/portal_login
PYTHONPATH=. python3 -m portal_login selftest
# 应输出 16/16 通过（全程用本机 mock，不接触真实门户）
```

## 5. 配置文件详解

INI 两段；**同名大写环境变量可覆盖任意项**（便于临时调试）。

| 键 | 段 | 默认值 | 说明 |
|---|---|---|---|
| `open_id` | auth | —（必填） | 微信永久标识，无人值守基础 |
| `service_name` | auth | `chinaMobile` | `chinaMobile`/`chinaTelecom`/`chinaUnicom`/`local` |
| `client_id` | auth | `6d6bc6f3b5f04107a5fc1c62e39dd5f4` | 抓包实测固定值，**不用改**，不写也走默认 |
| `api_base` | auth | `https://api.215123.cn` | 改版重逆时可指向 mock |
| `interval` | daemon | `30` | 常态探测间隔（秒） |
| `watch_interval` | daemon | `5` | 易断网时间窗内间隔（秒） |
| `watch_windows` | daemon | `11:55-12:10` | 时间窗，多个逗号分隔，**支持跨午夜**（如 `23:50-00:10`） |
| `retry_base` | daemon | `2` | 离线重试退避基数（秒） |
| `retry_cap` | daemon | `30` | 退避上限（秒），且不超过当前档间隔 |
| `jitter` | daemon | `0.2` | ±20% 随机抖动，防整点齐刷 |
| `probe_timeout` / `api_timeout` | daemon | `10` / `15` | 超时秒数 |
| `probe_url` | daemon | miui 204 地址 | 被劫持时换 `http://www.gstatic.com/generate_204` |
| `detect_enabled` | daemon | `yes` | 断网**记录**总开关（不影响自动认证） |
| `state_file` | daemon | `/tmp/portal_login/state.json` | 实时状态（tmpfs，重启清空） |
| `outage_log` | daemon | `/etc/portal_login/outages.jsonl` | 断网历史（overlay **持久化**） |
| `token_cache` | daemon | `/tmp/portal_login/token.json` | token 缓存（tmpfs，重启重换） |

修改配置后：`/etc/init.d/portal_login reload`（SIGHUP 热重载，无需重启进程）。

## 6. CLI 命令

```sh
PYTHONPATH=/usr/local/lib/portal_login python3 -m portal_login <命令>
```

| 命令 | 作用 |
|---|---|
| `daemon` | 守护主循环（procd 调用，也可前台跑看输出） |
| `once` | 只跑一拍：在线则退出 0；离线则立即登录并复核，成功 0/失败 1 |
| `status` | 输出实时状态 JSON（当前状态、离线已持续秒数、尝试次数、最近在线时间…） |
| `detect on` / `detect off` | 开启/关闭**断网记录**（写控制文件，即时生效，优先级高于配置；重启后回到配置值） |
| `selftest` | 本机 mock 全链路自测：已在线/完整链路/401 自愈/检测关闭 4 场景 16 项断言 |

路由器上的快捷封装：

```sh
# 单跑一次验证
PYTHONPATH=/usr/local/lib/portal_login python3 -m portal_login once
# 实时状态
/etc/init.d/portal_login status
# 临时关闭断网记录（比如要做网络割接演练）
PYTHONPATH=/usr/local/lib/portal_login python3 -m portal_login detect off
# 看运行日志
logread -f | grep portal_login
```

退出码：`once` 的 0/1 可直接用于外部告警/脚本编排。

## 7. 断网记录与状态（数据分析格式）

### 7.1 历史事件：`/etc/portal_login/outages.jsonl`

UTF-8 JSONL，**每行一个 JSON**，一次完整断网产生 3 行：

```json
{"event": "offline_start", "ts": "2026-09-16T12:00:03+08:00", "probe": "redirect(302) -> http://10.10.16.101:8080/eportal/...", "http_code": 302}
{"event": "online_restored", "ts": "2026-09-16T12:00:09+08:00", "after_attempts": 1}
{"event": "outage_summary", "start": "2026-09-16T12:00:03+08:00", "end": "2026-09-16T12:00:09+08:00", "duration_s": 6.12, "attempts": 1, "recovery": "portal_login"}
```

字段含义：

| 字段 | 含义 |
|---|---|
| `offline_start.ts` | 断网被发现的精确时刻（ISO8601 带时区） |
| `offline_start.probe` / `http_code` | 探测结果（302 门户 / 200 JS 跳转 / probe_error 等） |
| `outage_summary.start/end` | 断网起始/恢复时刻 |
| `outage_summary.duration_s` | 持续时长（秒，2 位小数） |
| `outage_summary.attempts` | 本次断网内发起认证的次数 |
| `outage_summary.recovery` | 恢复方式 |

分析示例：

```sh
# 所有断网时长（jq）
jq -r 'select(.event=="outage_summary") | "\(.start)  \(.duration_s)s  attempts=\(.attempts)"' \
  /etc/portal_login/outages.jsonl
# pandas：df = pd.read_json("/etc/portal_login/outages.jsonl", lines=True)
```

### 7.2 实时状态：`/tmp/portal_login/state.json`

每拍刷新，`status` 命令直接输出：

```json
{
  "state": "offline",
  "updated": "2026-09-16T12:00:07+08:00",
  "detection_enabled": true,
  "offline_since": "2026-09-16T12:00:03+08:00",
  "offline_elapsed_s": 4.2,
  "attempts": 1,
  "last_online": "2026-09-16T11:59:50+08:00",
  "last_error": null
}
```

### 7.3 开关语义

`detect off` 期间：守护**照常探测、照常自动认证**（保证上网），但不写任何断网事件，
state.json 中 `detection_enabled` 为 `false`。适合网络割接、主动拔线测试等不想污染统计的场景。

## 8. 智能频率控制

```
常态（默认）               时间窗 11:55-12:10（默认）
30s ± 抖动  ──────────►   5s ± 抖动
                          （12:00 踢下线最坏 5 秒内被发现）
发现离线当拍：立即认证（不等间隔）
离线持续：间隔 = min(2 × 2^(n-1), 当前档间隔)
          即约 2s, 4s, 5s(封顶窗口档)… 恢复后立即回常态档
```

- 时间窗可多个、可跨午夜；按**路由器本地时间**判断（OpenWrt 装好后确认 NTP 已校时）。
- 所有间隔叠加 ±20% 抖动；退避有 2 秒硬下限——既快速重连又不刷爆门户。
- 时间窗边界无需重启：每拍即时换档。

## 9. satoken 安全存储与自动刷新

v5 起**没有任何需要用户填写的 satoken/SA_TOKEN**（扫码 JWT 路径已删除）：

1. 守护按需调用 `certificateLogin?openId=...` 换发新 satoken，**主存内存**；
2. 磁盘缓存 `/tmp/portal_login/token.json`（自动 0600、tmpfs 重启即弃），
   仅为进程重启后省一次换发；
3. oauthRedirect 收到 401 → 立即作废缓存 → 重新换发 → 重试一次（每日踢下线核心自愈路径）；
4. 换发接口本身是安全登录接口：多次调用不踢在线会话，不影响其他设备。

## 10. 为什么选 Python（实测数据）

2026-09-15 在开发机（Python 3.13，手机热点）对**真实接口**实测 8 轮中位数：

| 请求 | shell v4：curl 每次冷启动 | v5：常驻进程连接复用 |
|---|---|---|
| HTTPS `certificateLogin` | 251.7 ms | **78.1 ms（快 3.2 倍）** |
| HTTP 探测 generate_204 | 211.4 ms | **60.9 ms（快 3.5 倍）** |
| 解释器冷启动 | curl 每次新建进程 | 37 ms（常驻后摊薄为 0） |
| 常驻内存 | sh≈1 MB + curl 瞬时 | 开发机 36 MB（Anaconda 偏胖）；**OpenWrt python3-light 预计 8–15 MB，部署后回填** |

- 轮询场景没有并发压力，差异主要来自 **TCP/TLS 连接复用**（curl 每次全新握手；
  全新 TLS 的 Python 请求 1158 ms 证明开销主要是握手而非进程）；
- 高延迟/不稳定网络下，统一的超时、退避、断线重建策略只在常驻进程里可能实现；
- 约 10 MB 级内存对 J4125/4GB 不足 0.4%；x86_64 上更不用纠结 CPU。
- 注：绝对耗时 Linux/OpenWrt 与 Windows 不同（Linux 建进程更便宜），但 TLS 复用的收益不变。
- 本工作负载**不需要并发**：顺序探测 + 认证，刻意保持简单与可判定。

## 11. 在 Windows/Ubuntu 上手动运行

需 Python 3.8+，无需安装任何第三方库：

```bash
# 在本目录（portal-login/）
export OPEN_ID="你的OpenID"
export WATCH_WINDOWS=""          # 调试时可临时清空时间窗
python3 -m portal_login selftest # 先跑 mock 自测
python3 -m portal_login once     # 真实单跑（最后一跳 eportal 仅宿舍网可达）
python3 -m portal_login daemon   # 前台守护，Ctrl+C 退出
```

Windows PowerShell 用 `$env:OPEN_ID="..."` 设置变量。

## 12. 常见问题

| 现象 | 原因与处理 |
|---|---|
| 启动报 `未配置 OPEN_ID` | 检查 `/etc/portal_login.conf` 的 `[auth]` 段；`chmod 600` 不影响 root 读取 |
| `import ssl` 失败 | `opkg install python3-light` 后仍缺则补装 `libopenssl` 或完整 `python3` |
| `certificateLogin 网络失败` | 先在路由器上 `curl -I https://api.215123.cn`；检查 DNS/时间（TLS 对时间敏感）与 ca-bundle |
| 12 点后没有恢复记录 | 确认时间窗与路由器时区/NTP：`date`；selftest 先过一遍排除代码问题 |
| 12 点恢复慢 | 调小 `watch_interval`（最小建议 3）、扩大窗口（如 `11:50-12:20`），reload 生效 |
| 记录文件没有内容 | `detect_enabled` 或 `detect off` 控制文件；`status` 看 `detection_enabled` 实际值 |
| eportal 步骤失败 | 最后一跳 `10.10.16.101:8080` 仅宿舍网内网可达，确认 WAN 口拿到宿舍网段 IP |
| 日志在哪 | `logread \| grep portal_login`；前台运行直接看 stderr |
| 想重置统计 | 直接清空：`: > /etc/portal_login/outages.jsonl` |
| 平台改版全链路失效 | 用 `capture_sso.ps1` 重抓，对照 [PROTOCOL_NOTES.md](PROTOCOL_NOTES.md) 的改版排查清单，可先用 `api_base` 指向 mock 定位 |

## 13. 参考项目

- [PairZhu/HuiHuTong](https://github.com/PairZhu/HuiHuTong) — OPEN_ID 抓包方法来源
- `reference/Huihutong-portal-login-master/` — 第三方同源链路实现（shell/python/daemon），
  其坑点（硬编码电信、Python 续行 bug、bash 依赖、HEAD 放行）见 PROTOCOL_NOTES 对照
- mentohust/minieap 为 802.1X 客户端，**本站 Web 门户不适用**，论证见 PROTOCOL_NOTES 第 1 节
