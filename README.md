# huihutong-broadband-autologin-script

慧湖通（独墅湖人才公寓）Web 门户认证的无人值守自动登录守护。
纯 Python 3 标准库实现（3.8+，零第三方依赖），面向 OpenWrt 软路由常驻运行。

仓库名 huihutong-broadband-autologin-script；Python 包 / 命令 / 服务名均为 `portal_login`，代码本体见 [portal_login/](./portal_login)。

适用网络：`api.215123.cn` 慧湖通门户（锐捷 eportal/RG-SAM 后台，微信 SSO 认证，
**非 802.1X**，mentohust/minieap 不适用）。

## 特性

- **无人值守**：以手机号 + 登录认证码为唯一永久凭证，自动换发 satoken，
  401 自动作废重换并重试
- **每日强制下线自愈**：覆盖每日约 12:00 的 RADIUS 踢下线，最坏数秒内自动恢复
- **智能频率**：常态 30s 探测，易断网时间窗内（默认 11:55-12:10）自动加速到 5s；
  离线时指数退避 `min(2·2^(n-1), retry_cap, 档间隔)`（1s 下限）+ ±20% 抖动
- **断网记录**：JSONL 事件流（断网起止/时长/认证尝试次数）+ 实时状态文件，
  可直接喂给 jq / pandas 做统计分析；记录可随时开关且不影响自动认证
- **连接复用**：http.client keep-alive 长连接，对真实 API 实测比 curl 冷启动快约 3 倍
- **健壮**：单拍异常全捕获不退出、连接断线自动重建重试一次、SIGHUP 热重载配置、
  SIGTERM 优雅退出、配套 mock 全链路自测

## 工作原理

整个登录是"用 HTTP 请求让网关把当前源 IP 加白"，多数场景不涉及密码提交：

```
① GET http://connect.rom.miui.com/generate_204   三态探测（204 在线 / 302 或 200-JS跳转 未认证 / 网络异常离线）
② 跟随重定向链，提取 eportal login_sso.jsp 基址（作为 redirect_uri）
   （实测链路会经 /ac/oauth2/authorize → sso/login.html?redirect=… 中转）
③ POST api.215123.cn/ac/auth/loginByPhoneAndUid  {phone, uid, captchaKey:""}  换发 satoken(JWT)
④ GET api.215123.cn/ac/auth/oauthRedirect（satoken 头）              换一次性 code
⑤ GET <带 code 的 eportal URL>                                       放行源 IP
⑥ 复查 generate_204 = 204                                            确认上线
```

`oauthRedirect` 返回 401 时，自动用手机号 + 认证码重新换发 token 并重试一次。
凭证的唯一作用是让 API 签发一次性 code，证明"该用户订购了对应运营商套餐"。

> ⚠️ 换 token **必须走 `/ac/auth/loginByPhoneAndUid`**。旧的
> `/web-app/auth/certificateLogin?openId=` 仍会返回 200 和一个结构完全正常的 JWT，
> 但 `/ac/auth/oauthRedirect` 会以 `code:500`「系统异常，请联系客服」拒绝它——
> 两个模块的会话表不互通（详见 [PROTOCOL_NOTES.md](PROTOCOL_NOTES.md) 第 3、4 节）。

> 📄 接口原理、抓包事实、踩坑记录（给未来维护者/AI 的完整逆向档案）：
> **[PROTOCOL_NOTES.md](PROTOCOL_NOTES.md)**
> —— 含「30 秒速览」、`redirect_uri` 编码层级对照、已知脆弱点表、
> 以及一个未决问题（运营商绑定页为何偶尔弹出，第 4.5 节）。

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

## 获取手机号与认证码（唯一需要手动获取的凭证）

两者都是永久值，获取一次长期有效：

1. PC 安装 Fiddler，开启 HTTPS 解密并信任根证书
2. 触发一次登录，两种途径都行（推荐第一种，不用装微信）：
   - **浏览器**：连上未认证的宿舍网 → 访问任意 http 站点 → 门户跳到慧湖通登录页 →
     输入手机号与认证码登录；
   - **微信 PC 版**打开"慧湖通"小程序，进入"我的"页。
3. 在抓包列表找 `POST api.215123.cn/ac/auth/loginByPhoneAndUid`，从请求体里复制
   `phone` 与 `uid`——**`uid` 就是你登录时填的那串「认证码」**（API 字段名叫 uid，
   配置项因此叫 `user_uid`）；`captchaKey` 实测可传空串
4. 验证（该接口是登录类接口，只发新 token，不踢会话）：

   ```sh
   curl -s -X POST "https://api.215123.cn/ac/auth/loginByPhoneAndUid" \
     -H "Content-Type: application/json" \
     -d '{"phone":"<手机号>","uid":"<认证码>","captchaKey":""}'
   # 返回 "code":200 且 data.token 为 "eyJ..." 即有效
   ```

> ℹ️ 响应里 `data.account` / `data.name` 都是 `null`，token 在 `data.token`；
> token 是 JWT，payload 里的 `loginId`（`NONE:<19位数字>`）是**服务端按 uid 反查出的
> 平台内部用户 ID**，与你要配置的 `uid`（认证码）不是同一个值，别混。

> 🛠 抓包用 Fiddler Classic 或浏览器 F12（步骤见 [PROTOCOL_NOTES.md](PROTOCOL_NOTES.md) 第 9 节）；
> 仓库不附带抓包脚本——旧的 `scripts/*.ps1` 是 openId 时代产物，已删除。

> 🔒 手机号 + 认证码 等同上网密码：配置文件 `chmod 600`，勿提交 git、勿截图外发。

## 配置

默认读取 `/etc/portal_login.conf`（可用 `--config` 或环境变量 `PORTAL_LOGIN_CONF` 指定），
同名大写环境变量可覆盖任意配置项（注意凭证键名是 `PHONE` / `USER_UID`，
不要写成 `UID`——shell 里 `UID` 是只读内置变量）。完整模板见
[`examples/portal_login.conf.example`](examples/portal_login.conf.example)，
部署时本地复制填入 `phone` / `user_uid` 后上传（流程见下文「OpenWrt 部署」）：

```ini
[auth]
phone = 你的手机号
user_uid = 你的认证码          ; API 字段名为 uid，就是登录时填的那串数字
service_name = chinaMobile      ; chinaMobile/chinaTelecom/chinaUnicom/local

[daemon]
interval = 30                   ; 常态探测间隔（秒）
watch_windows = 11:55-12:10     ; 易断网时间窗，逗号分隔多个，支持跨午夜
watch_interval = 5              ; 时间窗内探测间隔（秒）
```

| 键 | 段 | 默认值 | 说明 |
|---|---|---|---|
| `phone` | auth | —（必填） | 手机号 |
| `user_uid` | auth | —（必填） | 登录用的**认证码**（API 字段名为 `uid`） |
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
| `alert_after` | daemon | `10` | 连续**认证**失败多少拍后落一条持久化告警 |
| `alert_log` | daemon | `/etc/portal_login/alerts.jsonl` | 持久化告警（每事件一条，不刷 flash） |

> ✅ **改过 `phone` / `user_uid` 后无需手动清缓存**：token 缓存文件里记了凭证指纹，
> 与当前配置不符时会自动丢弃并重新换发（自测场景⑤覆盖）。改完 `restart` 或 `reload` 即可。
> 只改 `service_name`、时间窗等无凭证项时同样无影响。细节见
> [PROTOCOL_NOTES.md](PROTOCOL_NOTES.md) 第 15.1 节。

## 命令

```sh
python3 -m portal_login daemon      # 守护主循环（前台运行，适合 systemd/procd 托管）
python3 -m portal_login once        # 只跑一拍：在线退 0，离线立即登录，成功 0/失败 1
python3 -m portal_login status      # 实时状态 JSON
python3 -m portal_login detect off  # 关闭断网记录（照常认证，适合割接演练）
python3 -m portal_login selftest    # 本机 mock 全链路自测，25 项断言
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

分析示例（需 `opkg install jq`）：

```sh
# 每次断网的起始时间与耗时
jq -r 'select(.event=="outage_summary") | "\(.start)  \(.duration_s)s  attempts=\(.attempts)"' \
   /etc/portal_login/outages.jsonl

# 只看到断网原因（探测返回了什么）
jq -r 'select(.event=="offline_start") | "\(.ts)  \(.probe)"' \
   /etc/portal_login/outages.jsonl

# 统计次数与累计时长
jq -s '[.[]|select(.event=="outage_summary")] | "共 \(length) 次，累计 \(map(.duration_s)|add)s"' \
   /etc/portal_login/outages.jsonl
```

没装 jq 就用 `grep outage_summary /etc/portal_login/outages.jsonl | tail -20`。

实时状态在 `state.json`（当前状态、离线已持续秒数、认证尝试次数、最近在线时间），
`status` 命令直接输出。

## 查看日志

守护的运行日志走 syslog（procd 把 stdout/stderr 交给 logd）。有两种过滤方式：

| 过滤串 | 匹配依据 | 说明 |
|---|---|---|
| `-e portal_login` | 日志**正文**里的 logger 名 | **推荐**，只命中本服务，不受其他 Python 服务干扰 |
| `-e python` | logd 加的 **syslog tag**（`python3[pid]:`） | 兜底，tag 来自启动命令 `/usr/bin/python3` |

```sh
logread -e portal_login          # 推荐：只看本服务
logread -f -e portal_login       # 实时跟踪
logread -e portal_login | tail -50

logread -e python                # 兜底：按 tag 匹配（会混入其他 python 服务）
```

输出形如（`python3[pid]:` 前缀由 logd 添加，日期与 pid 因机而异）：

```
Fri Sep 19 12:00:03 2026 daemon.info python3[12345]: [2026-09-19 12:00:03] WARNING portal_login 检测到断网：redirect(302) -> http://10.10.16.101:8080/...
Fri Sep 19 12:00:05 2026 daemon.info python3[12345]: [2026-09-19 12:00:05] INFO portal_login 已通过 手机号+认证码 换取新 satoken
Fri Sep 19 12:00:09 2026 daemon.info python3[12345]: [2026-09-19 12:00:09] INFO portal_login 网络已恢复（离线 6.1 秒，尝试 1 次）
```

> ⚠️ `logread` 是**环形缓冲且重启即失**（缓冲大小见 `/etc/config/system` 的 `log_size`）。
> 需要跨重启追溯的长期故障，靠下面的 `alerts.jsonl`。

各文件位置与持久性：

| 内容 | 路径 | 重启后 |
|---|---|---|
| 运行日志 | `logread`（logd 环形缓冲） | **丢失** |
| 断网历史 | `/etc/portal_login/outages.jsonl` | 保留 |
| 持久化告警 | `/etc/portal_login/alerts.jsonl` | 保留 |
| 实时状态 | `/tmp/portal_login/state.json` | 丢失 |
| detect 开关 | `/tmp/portal_login/state.json.control.json` | **丢失，复位为 `detect_enabled`** |

服务与实时状态：

```sh
/etc/init.d/portal_login status                    # running 判定 + 状态 JSON
ubus call service list '{"name":"portal_login"}'   # procd 视角：pid / 重启次数
PYTHONPATH=. python3 -m portal_login status        # 同 state.json 内容
```

### 持久化告警 alerts.jsonl

连续**认证**失败达 `alert_after`（默认 10）拍时落一条，每次离线事件只落一条：

```sh
cat /etc/portal_login/alerts.jsonl     # 不存在 = 从未发生过长期认证失败
```

> ⚠️ **网络全断不产生告警**：`probe` 阶段（无门户入口 / WAN 断）压根没发认证请求，
> 不计入 `attempts`，那类故障看 `outages.jsonl` 的 `offline_start`。
> 完整分工表见 PROTOCOL_NOTES 15.1 第 5 点。

## 架构

```
__main__.py    CLI: daemon | once | status | detect on/off | selftest
config.py      INI + 环境变量覆盖 + 时间窗判定（支持跨午夜）
httpclient.py  http.client 长连接：按主机缓存、跟随 3xx、断线重建重试一次、天然禁代理
detector.py    三态探测 → ProbeResult(online/code/redirect/reason)
auth.py        TokenManager：loginByPhoneAndUid 换发 + 内存/tmpfs 缓存(0600) + 401 作废重换
portal.py      提取 eportal 基址 → oauthRedirect(401 重试) → GET 放行 → 204 复核
recorder.py    JSONL 事件 + state.json + 持久化告警 + 运行时控制文件（原子写入）
scheduler.py   主循环：时间窗换档、当拍立即登录、指数退避、抖动、信号处理
```

设计约束：顺序轮询无并发；守护绝不为单拍异常退出；token 永不出现在日志中；
禁用代理环境变量（认证流量必须 WAN 直连，否则 eportal 内网地址不可达/源 IP 不匹配）。

## OpenWrt 部署（procd 托管）

两种方式选一种即可。**方式 A** 全程在路由器上完成，不需要电脑，也不需要 scp。

### 方式 A：路由器直接从仓库下载（推荐）

路由器必须**先能上网**（任意设备完成一次门户认证即可）。经 `gh-proxy` 镜像下载
仓库压缩包，解出 `portal_login/` 与 `tools/`：

```sh
mkdir -p /root/portal_login && cd /root/portal_login

wget -O main.zip "https://v4.gh-proxy.org/https://github.com/lithiumspectrum/huihutong-broadband-autologin-script/archive/refs/heads/main.zip"
unzip -o main.zip

# ⚠️ 必须先删旧包再拷贝：cp 到「已存在」的同名目录会嵌成 portal_login/portal_login/portal_login
rm -rf portal_login tools
cp -r huihutong-broadband-autologin-script-main/portal_login \
      huihutong-broadband-autologin-script-main/tools .

# 顺带装好 init 服务脚本与配置模板
cp huihutong-broadband-autologin-script-main/openwrt_portal_login.init /etc/init.d/portal_login
cp huihutong-broadband-autologin-script-main/examples/portal_login.conf.example /etc/portal_login.conf
rm -rf main.zip huihutong-broadband-autologin-script-main

chmod +x /etc/init.d/portal_login && chmod 600 /etc/portal_login.conf
vi /etc/portal_login.conf                    # 填 phone / user_uid
/etc/init.d/portal_login enable && /etc/init.d/portal_login start
```

> **首次部署**可以省掉 `rm -rf portal_login tools`（目录还不存在，不会嵌套）；
> **更新时不能省**，否则会出现三层 `portal_login/portal_login/portal_login/`，
> 表现为启动报 `No module named portal_login`。
>
> **依赖**：`opkg install python3-light ca-bundle unzip`。
> 若装不上 `unzip`（busybox 默认不含），改用 tar.gz 免去该依赖：
>
> ```sh
> wget -O main.tar.gz "https://v4.gh-proxy.org/https://github.com/lithiumspectrum/huihutong-broadband-autologin-script/archive/refs/heads/main.tar.gz"
> tar xzf main.tar.gz        # busybox tar 自带 gzip，无需额外包
> ```
>
> 若 busybox `wget` 报 HTTPS 相关错误，用 `wget --no-check-certificate`，
> 或 `opkg install wget-ssl` 换用完整版 wget。

### 方式 B：电脑上 scp 上传

```sh
# ① 上传代码与 init 服务脚本（电脑上，仓库根目录执行）
ssh root@192.168.1.1 'mkdir -p /root/portal_login'
scp -r portal_login tools root@192.168.1.1:/root/portal_login/
scp openwrt_portal_login.init root@192.168.1.1:/etc/init.d/portal_login
```

**部署后预期布局（两种方式相同）**（`scp -r` / `cp` 到已存在的 `/root/portal_login/` 都会自动形成子目录，注意 `portal_login/portal_login/` 是两层）：

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

**方式 B 的后续步骤**（方式 A 已包含 ②③，可直接跳到自测）：

```sh
# ② 本地复制模板 → 填入 phone / user_uid → 上传（含凭证的本地副本用完即删）
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
`/etc`（overlay）放 outages.jsonl 与 alerts.jsonl，每次断网仅追加约 3 行、
每次长期故障仅追加 1 行，无 flash 磨损担忧。

## ⚠️ 安全注意

- **绝不调用** `api.215123.cn/ac/sso/logout` 或 `/ac/auth/logout` 做探测——
  GET 即注销，会吊销 token 并可能立刻踢断整机网络。判在线只用 generate_204。
- `loginByPhoneAndUid` 是安全登录接口（只发新 token，不踢会话），可放心重试。
- 配置文件与 token 缓存均 0600；token 缓存只落 tmpfs。

## 致谢

- [PairZhu/HuiHuTong](https://github.com/PairZhu/HuiHuTong) — 微信小程序抓包方法来源
- [Dustella/Huihutong-portal-login](https://github.com/Dustella/Huihutong-portal-login) — 同源链路实现（注意其硬编码 `chinaTelecom`；其 openId 凭证路线现已失效，这样的误导让我浪费了很长一段时间试错）

## License

MIT，见 [LICENSE](LICENSE)。