# 慧湖通门户认证 · 逆向技术档案（PROTOCOL_NOTES）

> 本文件是 2026-09 对 `api.215123.cn`（慧湖通 / 独墅湖人才公寓门户）**实测抓包 + 裸 curl 验证**
> 所得全部关键事实的沉淀，供日后维护、平台改版重逆、或与 AI 协作时直接作为事实上下文使用。
>
> 约定：文中所有响应样例均已脱敏（token/openId 用 `<...>` 表示）。**不要把真实 OPEN_ID 写进本文件**。
> 证据原文在 [`captures/`](captures/) 目录。部署操作看 [README.md](README.md)。
>
> **实现版本**：当前守护为 **Python v5**（`portal_login/` 包，纯标准库）；
> shell v4 已删除。v5 的架构、基准与与 v4 的差异见第 11 节。

---

## 目录

1. [系统画像：这是什么网络](#1-系统画像这是什么网络)
2. [主机与固定参数](#2-主机与固定参数)
3. [完整上线链路](#3-完整上线链路)
4. [凭证体系（核心）](#4-凭证体系核心)
5. [运营商参数](#5-运营商参数)
6. [二维码 / 403 / 微信 UA](#6-二维码--403--微信-ua)
7. [强制下线规律](#7-强制下线规律)
8. [⚠️ 危险接口与铁律](#8-️-危险接口与铁律)
9. [抓包方法论](#9-抓包方法论)
10. [平台改版后如何重新逆向](#10-平台改版后如何重新逆向)
11. [实现版本：Python v5（选型、架构、基准）](#11-实现版本python-v5选型架构基准)
12. [第三方实现对照](#12-第三方实现对照)
13. [已验证事实速查表](#13-已验证事实速查表)
14. [证据目录导览](#14-证据目录导览)
15. [安全与注意事项](#15-安全与注意事项)

---

## 1. 系统画像：这是什么网络

- **运营方**：慧湖通（独墅湖人才公寓），域名体系 `215123.cn`。
- **后台**：锐捷 **RG-SAM**（RADIUS 认证计费）+ **eportal**（门户页，内网 `10.10.16.101:8080`）。
  RG-SAM 是多接入方式平台（802.1X / Web / PPPoE / IPoE / VPN），“看到 RG-SAM”不代表开了 802.1X。
- **本站实际接入方式 = Web 门户认证（HTTP 重定向型）**，并在锐捷 eportal 之上套了一层
  慧湖通自研的**微信 SSO**（`broadband.215123.cn/sso/`）。用户没有独立拨号用户名/密码，
  身份载体是微信 openId / 扫码会话，运营商通过 `serviceName` 选择。
- **mentohust / minieap 不适用**：它们是 802.1X（EAPOL, ethertype `0x888E`）supplicant，
  模拟锐捷 SU 客户端 V2/V3 算法，与 HTTP 门户完全是两条协议栈；且 mentohust 2011 年起停更、
  minieap 主要面向新版 802.1X。被动验证方法（只抓不发，零风险）：
  ```sh
  # 路由器 WAN 口（替换为实际接口名）在未认证状态下抓 30~60 秒
  opkg install tcpdump
  tcpdump -i eth1 -nn -e ether proto 0x888e
  ```
  无输出 = 没开 802.1X；即使有 EAP-Request Identity，也还需有拨号账号密码且算法兼容，
  **切勿拿猜的密码反复试 mentohust**（锐捷常见失败次数锁定 MAC 策略）。
- 用户运营商：**中国移动（chinaMobile）**——页面上从左到右第 3 个按钮。

## 2. 主机与固定参数

| 用途 | 值 |
|---|---|
| SSO/API 主机 | `https://api.215123.cn` |
| 门户前端主机 | `https://broadband.215123.cn/sso/`（`login.html`、`broadband.html`） |
| eportal 内网地址 | `http://10.10.16.101:8080/eportal/`（**仅宿舍网二层可达**，放行动作发生在这里） |
| 固定 client_id | `6d6bc6f3b5f04107a5fc1c62e39dd5f4` |
| token 请求头名 | `satoken`（由扫码后 cookie `Token-Name=satoken` 指示；openId 模式返回体里 `tokenName` 同名） |
| 在线探测 URL | `http://connect.rom.miui.com/generate_204`（备选 `http://www.gstatic.com/generate_204`、`http://neverssl.com`） |
| 关键页面 JS | `broadband.js`（运营商选择与上线，函数 `selectedBroadband`）、`common.js`、`login.js`（已存证据目录） |

## 3. 完整上线链路

共 5 步（3.5 为条件性步骤），全程裸 curl 实测通过，**API 侧不校验 UA / Referer / 浏览器环境**（前端页有 403，API 没有）。

### 步骤 1：探测 captive portal 三态

```
GET http://connect.rom.miui.com/generate_204
```

| HTTP 状态 | 含义 | 处理 |
|---|---|---|
| `204 No Content` | 已在线 | 什么都不做 |
| `302`（Location 指向 eportal 或 SSO） | 未认证 | 取 `Location` 进入步骤 3 |
| `200`（HTML 内含 JS 跳转） | 部分网关形态 | 正则抠 `location.href='...'`，进入步骤 3 |

脚本里对应 `get_redir_url()`。注意不要用 HEAD（`-I`）探测三态——个别网关只对 GET 正确响应；
脚本用 GET + `-o /dev/null -w "%{http_code}"`。

### 步骤 2：取 eportal 登录基址（重定向链两种现场）

拿到入口 URL 后跟随跳转链，最终要得到 eportal 的 `login_sso.jsp` 基址（作为 `redirect_uri`）：

- **现场 A（直连）**：入口直接 302 到
  `http://10.10.16.101:8080/eportal/login_sso.jsp?<一堆 portal 参数>`
- **现场 B（经 SSO）**：跳到
  `https://broadband.215123.cn/sso/login.html?...&redirect=<一次编码的 eportal URL>`
  注意 `redirect` 值是**一次 URL 编码**串（`?` 呈 `%3F`，但参数间 `&` 是裸字符），
  与前端 `broadband.js` 的 `getParam('redirect')` 行为一致——**直接截取原文，不要再 decode**。

> **现场 B 的频率比想象高**：原以为只在某些网关形态出现，实测 2026-09-18 17:00 非踢下线时段
> 路由器重启后即走现场 B（probe 拿到 `index.jsp` → 跟随到 `broadband.215123.cn/sso/login.html?redirect=...`）。
> 现场 A 主要在 12:00 RADIUS 强踢瞬间出现。**v5 代码现场 B 截取曾存在 bug**：判断条件
> `"redirect=" in final and "/eportal/" not in final` 永远为 False（redirect= 的值就是 eportal URL，必含 /eportal/），
> 导致 SSO URL 整串被当 redirect_uri 传给 oauthRedirect → 服务端业务 500。已于 2026-09-18 修复，
> 现逻辑为 `if "redirect=" in final: final = final.split("redirect=", 1)[1]`。

脚本里对应 `extract_portal_url()`：跟随重定向链取 `resp.url`，按 `redirect=` 截取。

### 步骤 3：OPEN_ID 换 satoken + oauthRedirect 换一次性 code

```
GET https://api.215123.cn/web-app/auth/certificateLogin?openId=<OPEN_ID>
→ 200 {"success":true,"code":200,"data":{
       "userId":"<19位雪花ID>","account":"<32位hex>","name":"<姓名>",
       "tokenName":"satoken","token":"<JWT>" }}
```

然后（与 `broadband.js` 的 `selectedBroadband` 完全同构）：

```
GET https://api.215123.cn/ac/auth/oauthRedirect
    ?response_type=code
    &client_id=6d6bc6f3b5f04107a5fc1c62e39dd5f4
    &redirect_uri=<encodeURIComponent(步骤2的eportal基址)>
    &serviceName=chinaMobile
Header: satoken: <JWT>

→ 200 {"code":200,"data":"http://10.10.16.101:8080/eportal/login_sso.jsp?<原参数>&code=<一次性code>&serviceName=chinaMobile&apartmentId=<公寓ID>&roomId=<房间ID>"}
```

要点：

- `redirect_uri` 必须做完整 URL 编码（脚本 `url_encode()` 等价 JS `encodeURIComponent`，百分号先替换）。
- 返回的 `code` 是**一次性、短时效**授权码；`apartmentId` / `roomId` 由服务端带出，无需自己提供。
- token 失效时此接口返回 `code:401`——脚本 OPEN_ID 模式据此重新换发 token 并重试**一次**。
- 也可以用扫码得到的 SA_TOKEN 调本接口，但它会在每日踢下线后失效。

> ⚠️ **2026-09-18 oauthRedirect 业务 500 的根因已确证 = 服务商未绑定宽带账号**：
> 路由器重启/长时间离线后，eportal "忘记"该设备的 MAC/会话状态，此时 oauthRedirect 即使用
> 正确 token + 正确 redirect_uri 仍返回 HTTP 200 / body
> `{"success":false,"message":"系统异常，请联系客服","code":500,...}`。
> 用户描述的"有时自动跳过"的条件性账号密码步骤即下方**步骤 3.5**（已确证接口形态）。
> 假设成立：12:00 RADIUS 强踢时 MAC 仍在 eportal 缓存 → 跳过 3.5 直接放行；
> 重启/长时间离线后 MAC 缓存失效 → 触发 3.5 → 未补这一步则 oauthRedirect 业务 500。

### 步骤 3.5：条件性宽带账号密码提交（已确证）

**来源**：线上静态页 `https://broadband.215123.cn/sso/static/form/bind-broadband-form.html`
的内联 JS（`sa.addBindBroadband` / 运营商下拉框），2026-09-18 读取源码确证；非抓包推测。

**触发条件**：当前账号在**该服务商**下没有绑定宽带账号 → `oauthRedirect` 抛业务 500
（HTTP 200 + `code:500` + `message`「系统异常，请联系客服」）。已绑定时自动跳过，这就是
用户所说"有时自动跳过"的原因。

**查询已绑定**（判断是否需要补绑定，无副作用）：

```
GET https://api.215123.cn/ac/auth/selectBindBroadband
Header: satoken: <JWT>

→ 200 {"success":true,"code":200,"data":[{"id":...,"service":1,...}, ...]}
```

**新增绑定**（仅在缺失时提交）：

```
POST https://api.215123.cn/ac/auth/addBindBroadband
Header: satoken: <JWT>
Header: Content-Type: application/json
Body:   {"service":"1","account":"<宽带账号>","password":"<宽带密码>"}

→ 200 {"success":true,"code":200,"message":"绑定成功"}
```

要点：

- 三个字段**全部是字符串**——前端 layui 用 `JSON.stringify(data.field)` 提交，`service` 也是 `"1"`。
- `service` ↔ `serviceName` 映射：`0→chinaTelecom`、`1→chinaMobile`、`2→chinaUnicom`、`3→local`
  （注意与第 5 节的 `serviceName` 字符串是两套取值，不要混用）。
- 绑定成功后**不需要**额外动作，直接重试 `oauthRedirect` 即可拿到一次性 code。
- 该接口是**幂等新增**，重复提交同一服务商不会破坏已有配置；但守护仍按"先查后绑"实现，
  避免每次上线都写库。

**实现位置**：`portal_login/portal.py` —— `_select_bind()` / `_add_bind()` /
`_ensure_binding()`，在 `perform_login` 换到 token 之后、`_oauth_once` 之前调用。
凭证来自 `/etc/portal_login.conf` 的 `broadband_account` / `broadband_password`；
**未配置这两项时该步骤整体跳过**（老账号已绑定，无需此步）。

### 步骤 4：访问 eportal URL 完成 IP 放行

```
GET <上一步 data 里的完整 eportal URL>
→ 302 Location: http://10.10.16.101:8080/eportal/./success.jsp?userIp=...&...
```

此跳触发 NAS 设备对**来源 IP（路由器 WAN 口 IP）** 放通。用 **GET**（HEAD 是否被记账未验证，
不要依赖）。完成后立即复查步骤 1，确认 204。

> 整个登录是“用 HTTP 请求让网关把当前源 IP 加白”，**多数场景**不涉及密码提交；
> 凭证的作用仅是让 api.215123.cn 签发一次性 code，证明“这个微信用户订购了该运营商套餐”。
> 例外见步骤 3.5：eportal 忘 MAC 且该服务商未绑定时，必须先补交宽带账号密码（已实现）。

## 4. 凭证体系（核心）

### 4.1 OPEN_ID（永久，无人值守的唯一正解）

- 本质：微信用户在该小程序 appid 下的**永久固定标识**（`o...` / `or...` 开头约 28 字符）。
- 换发接口：`GET /web-app/auth/certificateLogin?openId=<id>`，**只需 openId**；
  抓包 URL 里虽有 `unionId=...`、小程序还带 `account=...`，实测均可省略。
- 返回 token 是标准 JWT（HS256），payload：
  ```json
  {"loginType":"login","loginId":"NONE:<19位用户ID>","rnStr":"<32位随机串>"}
  ```
- **每次调用都产生新 rnStr → 每次都是全新会话 token**；无 exp 字段，有效性由服务端会话表控制。
  已实测同一 openId 连续两次调用得到两个不同 rnStr 的 token，均可用。
- certificateLogin 是**登录类接口，调用安全**：它只发新 token，不清旧会话、不踢在线设备
  （与 logout 类接口性质相反，见第 8 节）。
- 唯一已知获取方式：Fiddler 中间人解密微信 **PC 版**“慧湖通”小程序的 HTTPS 流量，
  从该请求 URL 取参（手机版抓包门槛更高；参考 PairZhu/HuiHuTong）。

### 4.2 SA_TOKEN（扫码 JWT，⚠️ 历史凭证：v5 代码已移除支持）

> **v5 起用户侧不再配置、也不再支持静态 SA_TOKEN**（按需求连同注释一起移除）。
> 以下事实作为逆向档案保留——未来若有人问“cookie 里那串 JWT 是什么”，答案在这里；
> 现版本的 token 全部由 OPEN_ID 自动换发（4.1），它只是同名、同结构的临时会话串。

- 来源：浏览器微信扫码登录 SSO 网站后，cookie 里 `Admin-Token=<JWT>`；
  同域另有 cookie `Token-Name=satoken` 指示调用 API 时用的请求头名。
- 浏览器取法（Console）：`document.cookie.match(/Admin-Token=([^;]+)/)[1]`
  （httpOnly cookie JS 删不掉但这个域下可读出；也可在 Application 面板复制）。
- payload 与 4.1 相同，**rnStr 每次扫码随机**，所以 token 串每次都不一样。
- 无 exp 但服务端可主动吊销：调用 logout 后同一 token 立即 401。
- 每日 12:00 强制下线后大概率失效 → 无法无人值守，只能救急测试。

### 4.3 已排除的错误方向

- `/web-app/auth/getOpenId`（拿 ac 侧 token 反查 openId）→ 返回 500 `操作失败:null`。
  **网站 JWT 推不出 openId**，别在这条路上浪费时间（早期 extract_openid.ps1 已因此删除）。
- 二维码落地页 URL 本身不含可用凭证，见第 6 节。
- 网站 JWT 不能通过刷新接口续命——要永久自动，必须回到 openId 换发。

## 5. 运营商参数

`serviceName` 取值：

| 值 | 运营商 | broadband.html 按钮位置（从左到右） |
|---|---|---|
| `chinaUnicom` | 中国联通 | 第 1 个 |
| `chinaTelecom` | 中国电信 | 第 2 个 |
| `chinaMobile` | **中国移动（本用户）** | **第 3 个** |
| `local` | 本地内网（公寓内网） | 第 4 个 |

⚠️ 第三方 Dustella 脚本把 `chinaTelecom` **硬编码**在 sh 和 py 两处，移动用户照抄会走错通道。

## 6. 二维码 / 403 / 微信 UA

- 扫码登录页展示的二维码内容形如：
  `https://broadband.215123.cn/?clientId=<uuid>&uuid=<uuid>&type=pc`
- 该根 URL 在普通浏览器/裸 curl 打开 → nginx **403**；这是**正常现象**，
  服务器要求 `User-Agent` 含 `MicroMessenger`（微信内置浏览器/扫码容器）。
- 因此认证必须由微信扫码进入前端页；但**真正上线用的 api.215123.cn 接口不拦 UA**，
  拿到凭证后裸 curl 即可。不要把 403 误判成“封 IP / 凭证失效”。

## 7. 强制下线规律

- 宿舍网**每日约 12:00 由 SAM/RADIUS 侧主动踢掉所有会话**（RADIUS Disconnect-Message 类机制），
  需要重新走一遍上线链路。这正是守护脚本存在的理由。
- 锐捷 Web 认证另有门户保活机制（默认心跳 15 分钟、连续 5 次无心跳即删会话，约 75 分钟），
  本站因上层 SSO 架构，日常掉线以 12:00 定时踢为主。
- 脚本 30s 探测一次，踢下线后最坏约 30 秒内自动恢复；恢复动作 = 重新换 token + 全链路一次。

## 8. ⚠️ 危险接口与铁律

**铁律：状态变更类接口绝不能当探测接口调用。**

- `GET https://api.215123.cn/ac/sso/logout`
- `GET https://api.215123.cn/ac/auth/logout`

这两个接口 **GET 即执行注销**（返回 200，没有二次确认），调用后：
① 当前 token 立即失效（后续请求 401）；② 2026-09 实测时**整机网络会话立刻被踢断**。

教训来源：调试时曾用它“测试 token 是否有效”，导致当晚约 19:03 断网（错误码 974）。
判断在线状态**只用** generate_204 探测；判断 token 有效性只用业务接口的 401 响应。

## 9. 抓包方法论

### 9.1 Fiddler 抓 OPEN_ID（正式凭证）

1. 安装 Fiddler Classic，`Tools → Options → HTTPS`：勾 *Decrypt HTTPS traffic*，
   `Actions → Trust Root Certificate` 信任根证书，重启。
2. 微信 PC 版登录 → 打开慧湖通小程序 → “我的”页（触发 certificateLogin）。
3. Fiddler 过滤 `api.215123.cn`，找 `/web-app/auth/certificateLogin?openId=...`，抄 URL 参数。
4. 顺手记录响应体里的 `userId/account/name` 用于核对归属。

### 9.2 浏览器抓上线链路（理解协议/改版核对）

- 电脑连未认证宿舍网 → F12 Network 勾选 Preserve log → 访问任意 http 站点触发跳转 →
  微信扫码 → 观察完整 302 链与 `oauthRedirect` XHR。
- 或用 Fiddler/浏览器导出 HAR，用 `parse_har.ps1` 提取关键请求。
- `capture_sso.ps1`：一键保存探测三态、重定向链、SSO 页面与 JS（在电脑端连宿舍网跑）。
- `capture.ps1`：更早的网络环境快照（ipconfig / 路由 / ARP / DNS / 门户响应），用于排查二三层问题。
- PowerShell 注意：用 `curl.exe`（不是 `curl` 别名 `Invoke-WebRequest`）；
  含中文的 `.ps1` 用 UTF-8 BOM 保存；PSSecurityException 是执行策略噪声，不影响 curl.exe。

### 9.3 路由器侧被动验证 802.1X 是否存在

见第 1 节 tcpdump 命令；只抓包不发包，无锁号风险。

## 10. 平台改版后如何重新逆向

若脚本突然全链路失败，按此顺序定位（每步都可独立验证）：

1. **网络层**：路由器 WAN 是否拿到宿舍网段 IP、能否到 `10.10.16.101:8080`。
2. **探测三态**：手动 `curl -v http://connect.rom.miui.com/generate_204`，
   确认还是 204/302/200 哪种；探测域名若被换，改 `PORTAL_CHECK_URL`。
3. **重定向链**：`curl -sIL` 或 `capture_sso.ps1` 跟链，确认 eportal 基址形态
   （直连 vs SSO `redirect=`）与参数名是否变化。
4. **凭证换发**：`curl "https://api.215123.cn/web-app/auth/certificateLogin?openId=<id>"`
   是否仍 200；若 404/签名错误，需重新抓小程序看接口是否改路径/加签。
5. **上线接口**：浏览器抓一次真实成功的扫码上线，对比 `oauthRedirect` 的参数集合
   （client_id、参数名、serviceName 值、是否新增 timestamp/sign），同步改脚本。
6. **前端 JS 是事实源头**：重新下载 `broadband.js/login.js/common.js`（证据目录有旧版可 diff），
   从 `selectedBroadband` 等函数还原真实请求。
7. **别动 logout 类接口**（第 8 节）。

## 11. 实现版本：Python v5（选型、架构、基准）

### 11.1 选型结论与实测基准

shell v4（2026-09-15 早些时候的部署版本）在增加“断网记录、时间窗频率、token 缓存”
等**有状态**需求后已到维护性拐点，v5 改为 **Python 3 常驻守护（纯标准库）**。
2026-09-15 在 Windows 开发机（Python 3.13，手机热点）对**真实 api.215123.cn** 实测，
8 轮中位数：

| 请求 | shell v4（curl 每次冷启动，全量 TLS） | Python 常驻 + keep-alive | Python 全新连接（对照） |
|---|---|---|---|
| HTTPS certificateLogin | 251.7 ms | **78.1 ms（≈3.2 倍）** | 1158 ms |
| HTTP generate_204 探测 | 211.4 ms | **60.9 ms（≈3.5 倍）** | 195.6 ms |
| 解释器冷启动 | curl 每轮建进程 | 37 ms（常驻摊薄为 0） | — |
| 常驻 RSS | sh≈1 MB + curl 瞬时 | 开发机 36 MB（Anaconda 胖包）；OpenWrt python3-light 预计 8–15 MB（部署实测回填） | — |

结论要点：

1. 本负载是**顺序轮询**，无并发/CPU 瓶颈；收益来自 TCP/TLS 连接复用而非“Python 更快”。
   对照组（Python 全新连接 1158 ms）证明差距主要是 TLS 握手，不是进程创建。
2. Linux 上 fork 比 Windows 便宜，绝对数字会不同，但 TLS 复用收益在 J4125 上同样成立。
3. 高延迟/不稳定连接下，常驻进程才能做：连接复用、统一超时、连接级错误自动重建一次、
   指数退避、状态机与结构化记录；curl 子进程无状态，每拍全部重来。
4. 10 MB 级内存对 4 GB 设备 <0.4%。若未来设备换成极小内存机型（<128 MB），可回退 shell
   思路（本档案第 3 节的链路描述与语言无关）。
5. OpenWrt **24.10 起默认包管理器是 apk 不是 opkg**。

### 11.2 v5 模块架构

```
__main__.py   CLI: daemon | once | status | detect on/off | selftest
config.py     INI(/etc/portal_login.conf,0600) + 同名环境变量覆盖 + 时间窗判定(支持跨午夜)
httpclient.py http.client 长连接：按主机缓存连接、跟随 3xx、连接异常重建重试一次、天然禁代理
detector.py   三态探测 → ProbeResult(online/code/redirect/reason)
auth.py       TokenManager：内存 token + tmpfs 磁盘缓存(0600) + 401 作废重换
portal.py     extract_portal_url(现场A/B) → oauthRedirect(satoken 头,401 重试一次) → GET eportal → 204 复核
recorder.py   JSONL(offline_start/online_restored/outage_summary) + state.json + 运行时控制文件
scheduler.py  主循环：时间窗换档(30s/5s)、当拍立即登录、min(2·2^(n-1),档间隔) 退避、±20% 抖动、
              SIGTERM 退出、SIGHUP 热重载、单拍异常全捕获
tools/mock_portal.py  本机 ThreadingHTTPServer 模拟整条链路，selftest 四场景 16 断言
```

部署形态：代码 `/root/portal_login/`（`portal_login/` + `tools/`），
procd 以 `python3 -m portal_login daemon` 启动并 respawn；配置为 INI（不再是 shell env）。

### 11.3 数据落盘约定（OpenWrt 文件系统特性）

- `/tmp` 是 tmpfs（内存，重启清空）：state.json、token.json、detect 控制文件放这里——
  实时状态与 token 本就不应在重启后残留；
- `/etc` 在 overlay（flash 持久化）：outages.jsonl 放这里。每次断网仅追加约 3 行，
  日写入量极小，无磨损担忧；需要更长留存可自行 logrotate/导出。

### 11.4 v5 验证记录（2026-09-15）

- `selftest` 16/16：已在线无动作、302→SSO→oauth→eportal 单拍恢复、
  401 自动重换 token 二次成功（mint×2/oauth×2）、detect off 不产生记录。
- 真实接口：通过 v5 自身 httpclient（含 TLS 证书校验）调 certificateLogin 成功取 token；
  缓存命中返回同一 token，invalidate 后重取得到不同 token（rnStr 随机性符合预期）。
- 最后一跳 eportal 仅宿舍网可达，端到端留待路由器现场终验（README 第 4 节）。

## 12. 第三方实现对照

### 12.1 Dustella/Huihutong-portal-login（`reference/`）

与本版**同源**（同走 certificateLogin → oauthRedirect → eportal，Credits 同指向 PairZhu），
独立印证了路线正确，但直接部署有坑：

| 问题 | 详情 |
|---|---|
| 运营商写死 | `login.sh` 与 `login.py` 硬编码 `serviceName=chinaTelecom`（本用户是移动） |
| Python 版当前不可用 | URL 跨行续行写法使实际路径变成 `/web-app/auth/   certificateLogin`（吞得掉换行、吞不掉缩进空格）→ 404 |
| Shell 依赖 bash | OpenWrt 默认只有 ash，需额外 `opkg install bash` |
| 守护粗糙 | daemon.py 默认 5s 一轮、无开机自启/崩溃重启；`logging.info("Got sa token", satoken)` 多参每轮刷 Logging error |
| 健壮性 | 无 401 重试、无空 token 拦截、无登录后复核；最后一跳用 HEAD 触发放行（未验证可靠性） |

### 12.2 mentohust / minieap / xrgsu / ruijie-xclient

均为 802.1X/锐捷 SU 协议方向，**本站 Web 门户认证用不上**（第 1 节）。
若未来公寓改造为 802.1X（tcpdump 看到 EAPOL 且拿到了拨号账号密码），优先评估 minieap
（mentohust 已停更多年），需按学校算法选 fork 并交叉编译 ipk。

## 13. 已验证事实速查表

| 事实 | 结论 / 时间 |
|---|---|
| 接入方式 | Web 门户（eportal + 微信 SSO），非 802.1X |
| api.215123.cn 接口 | 裸 curl 可用，无 WAF/UA 拦截 |
| certificateLogin(openId) | ✅ 实测 200，只认 openId；token rnStr 每次随机；安全接口不踢会话 |
| oauthRedirect | ✅ 带 satoken 头成功，返回一次性 code 的 eportal URL |
| selectBindBroadband | ✅ GET + satoken 头，返回已绑定列表（元素含 `service` int）；判断是否需要补绑定 |
| addBindBroadband | ✅ POST + satoken 头 + JSON body `{service,account,password}`（全字符串），`code:200` 为成功 |
| oauthRedirect 业务 500 | HTTP 200 + `code:500`「系统异常，请联系客服」= 该服务商未绑定宽带账号（步骤 3.5） |
| 最后一跳 | `GET http://10.10.16.101:8080/eportal/login_sso.jsp?code=...` 放行源 IP，302 到 success.jsp |
| 扫码 SA_TOKEN | JWT 无 exp、rnStr 每次随机、服务端可吊销，每日踢下线后大概率失效 |
| getOpenId 反查 | 500 `操作失败:null`，死路 |
| logout 类 GET | ⚠️ 立即注销 + 可能断整机网，严禁探测 |
| 二维码根 URL | 非 MicroMessenger UA → 403，正常现象 |
| 运营商 | chinaMobile（页面第 3 按钮） |
| 强制下线 | 每日约 12:00，OPEN_ID 模式 30s 内自动恢复 |
| client_id | `6d6bc6f3b5f04107a5fc1c62e39dd5f4`（改版前固定） |
| 守护实现 | Python v5（纯标准库，2026-09-18）：selftest 25/25、真实 certificateLogin 验证通过；shell v4 已删除 |

## 14. 证据目录导览

[`captures/`](captures/) 下三组快照：

- `capture_20260915_145141/`、`capture_20260915_145235/`：最初网络环境诊断——
  ipconfig、路由、ARP、DNS、门户探测、重定向链、门户 HTML、早期 api 测试、openId 获取说明。
- `sso_capture_20260915_152903/`：**最有价值的一组**——
  三个探测域名响应（`02_portal_*`）、SSO 页面（`03_*`）、api 系列试探（`05_api_test_1~4`）、
  全量重定向链（`06_full_redirect_chain.txt`）、前端三件套源码
  （`broadband.js` / `common.js` / `login.js`，接口参数的最终事实来源）。
- 每个目录的 `capture_log.txt` 是抓取顺序与命令记录。

## 15. 安全与注意事项

- OPEN_ID 与 `/etc/portal_login.conf` 按上网密码级别保护：`chmod 600`，不入 git、不截图；
  token 缓存只在 tmpfs（`/tmp/portal_login/`），重启即弃。
- 本文件与所有归档样例保持脱敏；真实 openId 只存在路由器本地配置。
- 脚本默认禁用代理环境变量（`http_proxy/https_proxy/ALL_PROXY`），认证流量走 WAN 直连；
  开代理可能导致 eportal 内网地址不可达或源 IP 不匹配。
- 自动认证只解决“WAN 口放行”，多设备共享的隐蔽性由上级 README 的 UA2F + rkp-ipid 负责，二者缺一不可。
