# 慧湖通门户认证 · 逆向技术档案（PROTOCOL_NOTES）

> 本文件是 2026-09 对 `api.215123.cn`（慧湖通 / 独墅湖人才公寓门户）**实测抓包 + 裸 curl 验证**
> 所得全部关键事实的沉淀，供日后维护、平台改版重逆、或与 AI 协作时直接作为事实上下文使用。
>
> 约定：文中所有响应样例均已脱敏（token/手机号/UID 用 `<...>` 表示）。**不要把真实凭证写进本文件**。
> 证据原文在 [`captures/`](captures/) 目录。部署操作看 [README.md](README.md)。
>
> **实现版本**：当前守护为 **Python v5**（`portal_login/` 包，纯标准库）；
> shell v4 已删除。v5 的架构、基准与与 v4 的差异见第 11 节。
>
> **状态（2026-09-18 深夜）**：**已上线运行**。路由器现场端到端实测通过
> （`once` 单拍内 11.9 秒完成 offline→online，见第 7 节）。凭证体系已从
> openId 切换到「手机号 + 认证码」，抓包证据 `captures/capture5/fiddler.har`。
> 尚存一个**未决问题**（宽带账号绑定页的触发条件，见第 4.5 节）。

---

## 0. 30 秒速览（下次接手先读这里）

**这是什么**：慧湖通（独墅湖人才公寓）Web 门户认证的无人值守守护，
纯 Python 3 标准库，跑在 OpenWrt 上，解决每日约 12:00 RADIUS 强踢后的自动重连。

**一句话原理**：用「手机号 + 认证码」从 `/ac/auth/loginByPhoneAndUid` 换 satoken，
再用 satoken 换一次性 code，最后 GET 一个 eportal 内网 URL 让网关**把路由器 WAN 口 IP 加白**。
全程不发密码、不涉及 802.1X。

**最小事实集**（记不住的只记这 6 条）：

| # | 事实 |
|---|---|
| 1 | 换 token 只能走 `POST /ac/auth/loginByPhoneAndUid`；`/web-app/` 的 certificateLogin 签发的 token 一律被 oauthRedirect 拒（第 3 节根因） |
| 2 | 配置里 `user_uid` = 用户登录时填的**认证码**，不是 JWT 里的 19 位 `loginId` |
| 3 | 判在线**只用** `generate_204`；**绝不** GET `/ac/sso/logout` 或 `/ac/auth/logout`（GET 即注销 + 踢整机网，第 8 节） |
| 4 | `redirect_uri` 必须 `quote(url, safe="")` 编码一次；portal_url 里的 `?` 已由上游编码成 `%3F`，不能再 decode |
| 5 | 现场形态两类：入口可能直接给 `login_sso.jsp`（现场 A），也可能经 `sso/login.html?redirect=` 中转（现场 B，**更常见**） |
| 6 | 未决：宽带绑定页出现的触发条件不明（第 4.5 节），**但它与 oauthRedirect 500 无关，守护不依赖它** |

**部署与验证命令**（路由器 `ssh root@192.168.1.1`）：

```sh
# 上传（电脑上，仓库根目录）
scp portal_login/*.py root@192.168.1.1:/root/portal_login/portal_login/
scp tools/mock_portal.py root@192.168.1.1:/root/portal_login/tools/

# 路由器上
cd /root/portal_login
PYTHONPATH=. python3 -m portal_login selftest      # 期望 20/20
PYTHONPATH=. python3 -m portal_login status        # 实时状态 JSON
PYTHONPATH=. python3 -m portal_login once 2>&1 | tail -20   # 断网时验证实链路
/etc/init.d/portal_login restart                   # 改代码后必须重启守护
```

**配置文件** `/etc/portal_login.conf`（0600）：`[auth]` 段填 `phone` / `user_uid` / `service_name`。
**注意键名不能叫 `UID`**——shell 里 `UID` 是只读内置变量，会污染环境变量覆盖逻辑。

**排查顺序**（详见第 10 节）：网络层 → 探测三态 → 重定向链 → 换 token → oauthRedirect → 最后一跳。

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
  运营商通过 `serviceName` 选择；自动化的身份载体是**手机号 + 登录认证码**
  （API 字段名 `uid`，见第 4 节），微信扫码仅用于人工登录。
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
| token 请求头名 | `satoken`（由扫码后 cookie `Token-Name=satoken` 指示；换发接口返回体里 `tokenName` 同名） |
| 在线探测 URL | `http://connect.rom.miui.com/generate_204`（备选 `http://www.gstatic.com/generate_204`、`http://neverssl.com`） |
| 关键页面 JS | `broadband.js`（运营商选择与上线，函数 `selectedBroadband`）、`common.js`、`login.js`（已存证据目录） |

## 3. 完整上线链路

共 5 步，全程裸 curl 实测通过，**API 侧不校验 UA / Referer / 浏览器环境**（前端页有 403，API 没有）。

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

> **浏览器实测完整跳转链**（`captures/capture5/fiddler.har`，2026-09-18，两次上线完全一致）：
>
> ```
> [66]  GET http://10.10.16.101:8080/eportal/index.jsp?<portal 参数>
>         → 302 https://api.215123.cn/ac/oauth2/authorize?response_type=code
>                 &client_id=6d6bc6f3b5f04107a5fc1c62e39dd5f4
>                 &redirect_uri=http%3A%2F%2F10.10.16.101%3A8080%2Feportal%2Flogin_sso.jsp%3F...
> [76]  GET https://broadband.215123.cn/sso/login.html?isOAuth=true&client=<client_id>
>                 &redirect=http://10.10.16.101:8080/eportal/login_sso.jsp%3Fwlanuserip=...&wlanacname=...
>         → 200 HTML（登录页）
> [97]  POST /ac/auth/loginByPhoneAndUid              ← 用户登录
> [103] GET  https://broadband.215123.cn/sso/broadband.html?client=...&redirect=<同上>   ← 选运营商页
> [110] GET  /ac/auth/oauthRedirect?...&redirect_uri=<把上面 redirect 值再编码一次>
> [112] GET  http://10.10.16.101:8080/eportal/login_sso.jsp?<原参数>&code=...&serviceName=chinaMobile
>                 &apartmentId=...&roomId=...                    ← 放行
>         → 302 http://10.10.16.101:8080/eportal/./success.jsp?userIndex=...&keepaliveInterval=0
> [113] GET  /eportal/success.jsp?...                            ← 登录成功页（GBK）
> ```
>
> 注意中间还有一跳 `/ac/oauth2/authorize`（SSO 的 OAuth 授权端点），它把 eportal 基址
> **编码一次**后作为 `redirect_uri` 传给 SSO，SSO 再跳 `login.html`。
> **`extract_portal_url` 只需最终 `login.html` 上那个 `redirect=` 值**，中间这跳可以忽略。

### 步骤 2.5：`redirect_uri` 的编码层级（改版最易踩的坑，务必对齐）

三个形态必须分清，混一层就是 500：

| # | 形态 | 样例（片段） | 出处 |
|---|---|---|---|
| ① | 原始 eportal URL | `.../login_sso.jsp?wlanuserip=...&wlanacname=...` | eportal 内部 |
| ② | `redirect=` 参数值（**一次编码**） | `.../login_sso.jsp%3Fwlanuserip=...&wlanacname=...` | `login.html` URL 上 |
| ③ | `oauthRedirect` 的 `redirect_uri`（**对 ② 再编码一次**） | `...%2Flogin_sso.jsp%253Fwlanuserip%3D...%26wlanacname%3D...` | 浏览器实际发送 |

**验证方法**（下次改版先跑这个）：把 ③ `unquote` **一次**，应当**逐字节等于** ②。

```python
assert unquote(redirect_uri_param) == redirect_param_value   # 必须成立
```

**对代码的要求**：拿到 ② 之后**直接** `quote(value, safe="")`，**绝不能先 `unquote` 再编码**
（那会得到 `%3F` 而不是 `%253F`，少一层）。`portal.py::extract_portal_url` 刻意
“不再 decode，与前端 `getParam('redirect')` 行为一致”就是为此。

> **隐含假设（脆弱点）**：`redirect` 必须是 URL 上的**最后一个参数**。
> 现在的截取实现是 `final.split("redirect=", 1)[1]`，若平台将来在 `redirect` 之后
> 再追加参数（如 `&foo=1`），`portal_url` 会被污染成 `...&foo=1` 并原样编码进去 → 500。
> 又因为 eportal 参数之间用的是**裸 `&`**，无法用“截断到第一个 `&`”来修。
>
> **为什么无法在客户端根治**：这个 URL 本身在追加参数后就**有歧义**了——谁也分不清
> `&foo=1` 属于 eportal 还是外层 SSO；站点自己的 `broadband.js::getParam('redirect')`
> 会同样把 `&foo=1` 卷进去，**浏览器登录也会一起 500**。所以这不是脚本的 bug，
> 而是平台改版的表征。脚本能做到的是**不静默**：`portal.py` 在 oauthRedirect 失败时
> 会把本次实际发出的 `redirect_uri` 整串打进日志，并在检出外层参数
> （`isOAuth` / `client` / `redirect` 出现在 portal_url 里）时直接点名原因。
> 平台改版后若报 500，**先看这条 warning，再检查 login.html 的 URL 参数顺序**。

### 步骤 3：手机号 + 认证码（uid）换 satoken + oauthRedirect 换一次性 code

```
POST https://api.215123.cn/ac/auth/loginByPhoneAndUid
Header: Content-Type: application/json
Body:   {"phone":"<手机号>","uid":"<认证码>","captchaKey":""}
         ↑ 浏览器实测原文，见 captures/capture5/fiddler.har

→ 200 {"success":true,"message":"操作成功！","code":200,"data":{
       "account":null,"name":null,
       "tokenName":"satoken","token":"<JWT>" }}
```

要点：

- `uid` = 用户登录时填的**认证码**（短数字串），**不是** token payload 里
  `loginId` 的 19 位平台内部用户 ID——后者是服务端按 `phone`+`uid` 反查出来的。
  早期文档曾把两者混为一谈，已更正。
- `account` / `name` 为 **null**（与旧 certificateLogin 不同），token 在 `data.token`。
- `captchaKey` 传**空串**即可，实测不触发验证码，也没有风控拦裸 curl。
- 与旧接口返回的 JWT **结构完全相同**：payload 都是
  `{"loginType":"login","loginId":"NONE:<用户ID>","rnStr":"<32位随机串>"}`。

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

- `redirect_uri` 必须做完整 URL 编码（`quote(portal_url, safe="")` 等价 JS `encodeURIComponent`）。
- 返回的 `code` 是**一次性、短时效**授权码；`apartmentId` / `roomId` 由服务端带出，无需自己提供。
- token 失效时此接口返回 `code:401`——脚本据此重新换发 token 并重试**一次**。

> ⚠️ **2026-09-18 确证的 oauthRedirect 业务 500 根因 = token 来自错误模块**：
> `GET /web-app/auth/certificateLogin?openId=<OPEN_ID>` 仍返回 HTTP 200 + `code:200` +
> 一个**格式完全正常的 JWT**，但它换来的会话**不被 `/ac/**` 的 OAuth 流程承认**，
> `oauthRedirect` 一律返回 HTTP 200 / body
> `{"success":false,"message":"系统异常，请联系客服","code":500,...}`——
> 带不带浏览器头都一样，客户端无法补救。改用 `POST /ac/auth/loginByPhoneAndUid`
> 的 token 后，同参数立刻 200 并带出 `&code=...&serviceName=chinaMobile&apartmentId=...&roomId=...`。
>
> **交叉验证矩阵**（同 redirect_uri，2026-09-18 22:2x）：

| token 来源 | 请求头 | oauthRedirect 结果 |
|---|---|---|
| 浏览器（扫码） | 浏览器完整头 | 200 |
| 浏览器（扫码） | 仅 `satoken` | 200 |
| certificateLogin(openId) | 浏览器完整头 | 500 |
| certificateLogin(openId) | 仅 `satoken` | 500 |

> 两个 token 的 JWT payload 除 `rnStr` 外**逐字节相同**（`loginId` 都是 `NONE:<同一用户ID>`），
> 差异只在服务端会话表 → **换发入口必须与消费方 `/ac/` 同模块**。
> **不要再去试 User-Agent / Origin / Referer / Content-Type 等客户端侧修补**，已全部排除。

### 步骤 4：访问 eportal URL 完成 IP 放行

```
GET <上一步 data 里的完整 eportal URL>
→ 302 Location: http://10.10.16.101:8080/eportal/./success.jsp?userIp=...&...
```

此跳触发 NAS 设备对**来源 IP（路由器 WAN 口 IP）** 放通。用 **GET**（HEAD 是否被记账未验证，
不要依赖）。完成后立即复查步骤 1，确认 204。

> 整个登录是“用 HTTP 请求让网关把当前源 IP 加白”，全程**不涉及密码提交**；
> 凭证的作用仅是让 api.215123.cn 签发一次性 code，证明“这个用户订购了该运营商套餐”。

## 4. 凭证体系（核心）

### 4.1 手机号 + 认证码（uid，永久，无人值守的唯一正解）

- 换发接口：`POST /ac/auth/loginByPhoneAndUid`，body `{"phone","uid","captchaKey":""}`
  （`Content-Type: application/json`）。`phone` 是手机号，`uid` 是**登录认证码**
  （配置项名 `user_uid`）；两者都是**永久值**，配置一次长期有效。
- **必须用 `/ac/` 模块的登录接口**（见步骤 3 的 500 根因说明）；`/web-app/` 的
  certificateLogin 换来的 token 结构正常但不被 `/ac/` OAuth 承认。
- 返回 token 是标准 JWT（HS256），payload：
  ```json
  {"loginType":"login","loginId":"NONE:<19位平台用户ID>","rnStr":"<32位随机串>"}
  ```
  其中 `loginId` 是服务端解析出的平台内部用户 ID，**与请求里的 `uid`（认证码）无关**。
- **每次调用都产生新 rnStr → 每次都是全新会话 token**；无 exp 字段，有效性由服务端会话表控制。
- 登录类接口，**调用安全**：只发新 token，不清旧会话、不踢在线设备
  （与 logout 类接口性质相反，见第 8 节）。
- 唯一已知获取方式：Fiddler 中间人解密微信 **PC 版**“慧湖通”小程序的 HTTPS 流量，
  从该请求的 body 取 `phone` / `uid`（手机版抓包门槛更高）。

### 4.2 历史凭证：OPEN_ID / certificateLogin（⚠️ v5 代码已移除）

> `/web-app/auth/certificateLogin?openId=<id>` 目前**仍可用**（HTTP 200 + 正常 JWT），
> 但它签发的会话**不能用于 `/ac/auth/oauthRedirect`**（一律 500「系统异常，请联系客服」）。
> 因此守护已改走 4.1 的手机号 + 认证码 路线；openId 参数仅在逆向档案中保留。
> 同样的坑：`openId` 抓包 URL 里另有 `unionId=...`、`account=...`，实测均可省略。

### 4.3 SA_TOKEN（扫码 JWT，⚠️ 历史凭证：v5 已移除支持）

> **v5 起用户侧不再配置、也不再支持静态 SA_TOKEN**。
> 以下事实作为逆向档案保留——未来若有人问“cookie 里那串 JWT 是什么”，答案在这里；
> 现版本的 token 全部由 4.1 自动换发，它只是同名、同结构的临时会话串。

- 来源：浏览器微信扫码登录 SSO 网站后，cookie 里 `Admin-Token=<JWT>`；
  同域另有 cookie `Token-Name=satoken` 指示调用 API 时用的请求头名。
- 浏览器取法（Console）：`document.cookie.match(/Admin-Token=([^;]+)/)[1]`
  （httpOnly cookie JS 删不掉但这个域下可读出；也可在 Application 面板复制）。
- payload 与 4.1 相同，**rnStr 每次扫码随机**，所以 token 串每次都不一样。
- 无 exp 但服务端可主动吊销：调用 logout 后同一 token 立即 401。
- 每日 12:00 强制下线后大概率失效 → 无法无人值守，只能救急测试。

### 4.4 已排除的错误方向

- `/web-app/auth/getOpenId`（拿 ac 侧 token 反查 openId）→ 返回 500 `操作失败:null`。
  **网站 JWT 推不出 openId**，别在这条路上浪费时间。
- 二维码落地页 URL 本身不含可用凭证，见第 6 节。
- 网站 JWT 不能通过刷新接口续命——要永久自动，必须用 4.1 的永久凭证重新换发。
- **宽带账号绑定（selectBindBroadband / addBindBroadband）与 oauthRedirect 500 无关**：
  capture5 全程抓包证明浏览器从不调用这两个接口，该账号查绑定返回 `"data":[]` 也照样 200。
  守护中曾实现的“条件性补绑定”逻辑已按此结论**整体删除**——不要复活。
  （另一个未决问题见 4.5：那个绑定页面**为什么会偶尔弹出来**，尚未定论。）

### 4.5 ⚠️ 未决问题：绑定运营商页面的触发条件

**状态：未定论，但对守护无影响。留给未来的现场。**

**现象**：用户报告浏览网页认证时“偶尔弹出”一个要求填宽带账号/密码的
「绑定运营商」表单页（`https://broadband.215123.cn/sso/static/form/bind-broadband-form.html`）。

**已确证的事实**（证据：`captures/capture1/10.10.16.101.har` 含完整前端源码）：

1. 打开该表单的唯一入口是 `broadband.js` 里的 `bindBroadband()` —— 它用
   `layer.open({title:'绑定运营商', content:'./static/form/bind-broadband-form.html'})` 弹 iframe。
2. `bindBroadband()` 的**唯一调用点**在 `sa.selectBindBroadband()` 内部，而且：

   ```js
   sa.selectBindBroadband = () => {
     sa.ajax("/auth/selectBindBroadband", {}, res => {
       if (res.code == 200) {
         ... 第一个 for 循环：只 show/hide .binded/.to-bind/.btn ...
         return          // ← 从这里就返回了
         ... 后面给 $('.china-telecom') 等绑 bindBroadband() 点击事件的整段代码 ...
       }                 // ← 全是不可达死代码
     }, 'get');
   }
   ```

3. 更关键：文件末尾的自动调用被**注释掉了** —— `// sa.selectBindBroadband();`
4. `broadband.html` 里 4 个运营商按钮**全部**是 `onclick="selectedBroadband('chinaXxx')"`，
   页面里没有任何 `bindBroadband` 字样。
5. 全仓库抓包交叉验证：`bindBroadband` 只出现在 `captures/capture1` 的 JS 源码里；
   `capture5`（两次完整上线流程）中 `selectBindBroadband` / `addBindBroadband` /
   `bind-broadband-form` 的请求数为 **0**。
6. `capture3`（`fidder.har` 第 88 条）与 `capture4` 确实加载过该表单页 —— 但
   `capture4` 的 Edge 遥测字段是 `navigationUrl=https://.../bind-broadband-form.html`，
   `capture3` 中该请求的 Referer 指向它自己、且前后**没有** `selectBindBroadband` 调用 ——
   与“**直接手动打开该 URL**”的形态一致，而非自动弹出。

**据此的推断（非结论）**：在抓到的这版前端（`broadband.js?v=9`）里，
自动流程**不可能**弹出该表单；能弹出只有两种可能：

- (a) 人工直接访问了该 URL（含浏览器自动补全/历史记录/标签页恢复）；
- (b) 平台后来发布了新版 `broadband.js`，把 `sa.selectBindBroadband();` 的注释去掉
  或删掉了那个提前 `return`。

**未来如何一击定论**（下次再弹出时按此做，10 分钟可结案）：

1. 弹窗前先抓包，只保留 `broadband.215123.cn` 与 `api.215123.cn` 两个域；
2. 若有 `GET /ac/auth/selectBindBroadband` → 是 (b)，站点改了 JS，去看返回 `data` 是否为空数组；
3. 若**没有**该请求、且请求 Referer 是自身或为空 → 是 (a)，属于人工/浏览器行为；
4. 顺手 `curl -s "https://broadband.215123.cn/sso/static/broadband.js?v=9"` 看
   `// sa.selectBindBroadband();` 这一行**是否仍被注释**——这是最省事的判定信号。

**为什么现在可以不管它**：守护走纯 API 链路（换 token → oauthRedirect → 放行），
不经过任何前端页面；且已实测：该账号 `selectBindBroadband` 返回 `"data":[]`（即“未绑定任何运营商”）
时 `oauthRedirect` 依然 200 并正常上线。另外 `addBindBroadband` 服务端自身有 bug，
任何调用都报 `Field 'open_time' doesn't have a default value` —— 即使将来真要绑，也得先由平台修。

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
- **现场实测（2026-09-18 23:13，路由器 `once`）**：单拍内 11.9 秒完成 offline→online、
  尝试 1 次。时间构成 ≈ 探测(≤3s) + 换 token(1s) + 跟随门户跳转链 + oauth + 最后一跳放行
  (≈10s)。恢复是**串行单线程**的，5s 窗口频率是“拍与拍之间”的间隔而非“每 5s 强制发请求”，
  认证过程绝不会被打断或重复。（守卫逻辑见 scheduler.py：tick 阻塞跑完才 sleep。）

## 8. ⚠️ 危险接口与铁律

**铁律：状态变更类接口绝不能当探测接口调用。**

- `GET https://api.215123.cn/ac/sso/logout`
- `GET https://api.215123.cn/ac/auth/logout`

这两个接口 **GET 即执行注销**（返回 200，没有二次确认），调用后：
① 当前 token 立即失效（后续请求 401）；② 2026-09 实测时**整机网络会话立刻被踢断**。

教训来源：调试时曾用它“测试 token 是否有效”，导致当晚约 19:03 断网（错误码 974）。
判断在线状态**只用** generate_204 探测；判断 token 有效性只用业务接口的 401 响应。

## 9. 抓包方法论

### 9.1 Fiddler 抓手机号与认证码（正式凭证）

1. 安装 Fiddler Classic，`Tools → Options → HTTPS`：勾 *Decrypt HTTPS traffic*，
   `Actions → Trust Root Certificate` 信任根证书，重启。
2. 微信 PC 版登录 → 打开慧湖通小程序 → “我的”页（触发登录）。
3. Fiddler 过滤 `api.215123.cn`，找 `POST /ac/auth/loginByPhoneAndUid`，
   从 **请求体** 抄 `phone` 与 `uid`（`uid` 即登录认证码）。
4. 顺手记录响应体里的 `data.tokenName` 等用于核对归属。

### 9.2 浏览器抓上线链路（理解协议/改版核对）

- 电脑连未认证宿舍网 → F12 Network 勾选 Preserve log → 访问任意 http 站点触发跳转 →
  微信扫码 → 观察完整 302 链与 `oauthRedirect` XHR。
- 需要离线比对时：Fiddler 或 F12 右键 **Save all as HAR**，用一段 Python
  （`json.load` + 遍历 `log.entries`）筛出关注请求即可；仓库**不再附带抓包脚本**
  （旧的 `scripts/*.ps1` 围绕 openId 编写，已删除）。
  分析时注意：`login.html` 的 `redirect` 值内部用**未编码的裸 `&`**（见步骤 2.5），
  所以必须**取到串尾**，用常规 query 解析器会把 eportal 参数截断。
- 排查二三层问题（ipconfig / 路由 / ARP / DNS / 门户响应）用系统自带命令即可。

### 9.3 路由器侧被动验证 802.1X 是否存在

见第 1 节 tcpdump 命令；只抓包不发包，无锁号风险。

## 10. 平台改版后如何重新逆向

若脚本突然全链路失败，按此顺序定位（每步都可独立验证）：

1. **网络层**：路由器 WAN 是否拿到宿舍网段 IP、能否到 `10.10.16.101:8080`。
2. **探测三态**：手动 `curl -v http://connect.rom.miui.com/generate_204`，
   确认还是 204/302/200 哪种；探测域名若被换，改 `PORTAL_CHECK_URL`。
3. **重定向链**：`curl -sIL` 跟链，确认 eportal 基址形态
   （直连 vs SSO `redirect=`）与参数名是否变化。
4. **凭证换发**：`curl -X POST .../ac/auth/loginByPhoneAndUid -d '{"phone":...,"uid":...,"captchaKey":""}'`
   是否仍 200；若 404/签名错误，需重新抓小程序看接口是否改路径/加签。
   若换发 200 但 oauthRedirect 报 500，**优先怀疑换发接口用错了模块**（第 3 节根因）。
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
| HTTPS 换 token | 251.7 ms | **78.1 ms（≈3.2 倍）** | 1158 ms |
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
auth.py       TokenManager：POST loginByPhoneAndUid 换发 + 内存/tmpfs 缓存(0600) + 401 作废重换
portal.py     extract_portal_url(现场A/B) → oauthRedirect(satoken 头,401 重试一次) → GET eportal → 204 复核
recorder.py   JSONL(offline_start/online_restored/outage_summary) + state.json + 运行时控制文件
scheduler.py  主循环：时间窗换档(30s/5s)、当拍立即登录、min(2·2^(n-1),档间隔) 退避、±20% 抖动、
              SIGTERM 退出、SIGHUP 热重载、单拍异常全捕获
tools/mock_portal.py  本机 ThreadingHTTPServer 模拟整条链路，selftest 四场景 17 断言
```

部署形态：代码 `/root/portal_login/`（`portal_login/` + `tools/`），
procd 以 `python3 -m portal_login daemon` 启动并 respawn；配置为 INI（不再是 shell env）。

### 11.3 数据落盘约定（OpenWrt 文件系统特性）

- `/tmp` 是 tmpfs（内存，重启清空）：state.json、token.json、detect 控制文件放这里——
  实时状态与 token 本就不应在重启后残留；
- `/etc` 在 overlay（flash 持久化）：outages.jsonl 放这里。每次断网仅追加约 3 行，
  日写入量极小，无磨损担忧；需要更长留存可自行 logrotate/导出。

### 11.4 v5 验证记录

**2026-09-15 首测（openId 凭证时代）**

- `selftest` 全绿；真实接口调通；但端到端在路由器现场失败（oauthRedirect 500）。

**2026-09-18 换凭证后复测（当前状态：已上线运行）**

- `selftest` **20/20**：①已在线无动作、②302→SSO→loginByPhoneAndUid→oauth→eportal 单拍恢复、
  ③401 自动重换 token 二次成功（mint×2/oauth×2）、④detect off 照常认证但不写记录、
  ⑤改凭证后磁盘 token 缓存自动失效重换（防静默沿用旧账号会话）。
- 本机（Windows）经 v5 自身 httpclient（含 TLS 校验）调 `loginByPhoneAndUid` 成功取 token；
  缓存命中返回同一 token，invalidate 后重取得到不同 token（rnStr 随机性符合预期）。
- **路由器现场端到端成功**（`PYTHONPATH=. python3 -m portal_login once`）：
  `html_js_jump(200) → index.jsp` → 换 token → oauthRedirect 200 → 放行 → **204**，
  单拍内 11.9 秒恢复，`outage_summary` 正常落盘。
- 交叉矩阵确证根因（第 3 节）：同 redirect_uri 下，只有 `/ac/` 模块签发的 token 被接受。

## 12. 第三方实现对照

### 12.1 Dustella/Huihutong-portal-login（`reference/`）

与本版**同源**（同走 certificateLogin → oauthRedirect → eportal，Credits 同指向 PairZhu），
当年独立印证了路线正确；但其 `certificateLogin` 凭证路线**现已失效**（第 3 节根因），
仅作历史对照。其自身还有以下部署坑：

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
| loginByPhoneAndUid | ✅✅ 实测 200，POST JSON `{phone,uid,captchaKey:""}`（`uid`=登录认证码）；token rnStr 每次随机；安全接口不踢会话 |
| certificateLogin(openId) | ⚠️ 实测 200 且 JWT 结构正常，但签发的会话**不被 /ac/ OAuth 承认** |
| oauthRedirect | ✅ 带「来自 /ac/ 的」satoken 头成功，返回一次性 code 的 eportal URL |
| oauthRedirect 业务 500 | HTTP 200 + `code:500`「系统异常，请联系客服」= **token 来自 /web-app/ 模块**，与请求头无关 |
| 浏览器完整链路 | `index.jsp` →302→ `/ac/oauth2/authorize` →302→ `sso/login.html?redirect=` →(登录)→ `broadband.html` → `oauthRedirect` → `login_sso.jsp?code=` →302→ `success.jsp` |
| redirect_uri 编码 | `unquote(redirect_uri)` **一次** 必须逐字节等于 `login.html` 上的 `redirect=` 值（即对 ② 再编码一次，`%3F`→`%253F`） |
| extract_portal_url 隐含假设 | `redirect` 必须是 URL **最后一个参数**；后面追加参数会污染 portal_url（第 3 节步骤 2.5） |
| 宽带绑定接口 | ❌ 与上线无关（浏览器全程不调用）；addBindBroadband 服务端有 open_time 字段 bug |
| 绑定页弹出原因 | ⚠️ **未决**：现版 JS 里 `selectBindBroadband()` 的自动调用被注释、且内部有提前 return，理论上不可能自动弹；见第 4.5 节 |
| 最后一跳 | `GET http://10.10.16.101:8080/eportal/login_sso.jsp?code=...` 放行源 IP，302 到 `success.jsp` |
| 现场端到端 | ✅ 2026-09-18 23:13 路由器 `once` 单拍 11.9 秒恢复，尝试 1 次 |
| 扫码 SA_TOKEN | JWT 无 exp、rnStr 每次随机、服务端可吊销，每日踢下线后大概率失效 |
| getOpenId 反查 | 500 `操作失败:null`，死路 |
| logout 类 GET | ⚠️ 立即注销 + 可能断整机网，严禁探测 |
| 二维码根 URL | 非 MicroMessenger UA → 403，正常现象 |
| 运营商 | chinaMobile（页面第 3 按钮） |
| 强制下线 | 每日约 12:00，手机号+认证码 模式 30s 内自动恢复 |
| client_id | `6d6bc6f3b5f04107a5fc1c62e39dd5f4`（改版前固定） |
| 守护实现 | Python v5（纯标准库，2026-09-18 换凭证）：selftest 20/20、路由器现场端到端通过；shell v4 已删除 |

## 14. 证据目录导览

[`captures/`](captures/) 下五组快照（**全部含真实凭证，已在 .gitignore 中排除，切勿外发**）：

- `capture1/`：早期网络环境快照。其中 `10.10.16.101.har` 的
  `broadband.js?v=9` 与 `broadband.html` **正文**是前端行为的第一手证据
  （第 4.5 节绑定页判定即基于此）。
- `capture2/`、`capture3/`、`capture4/`：中间过程的试探抓包，价值有限；
  `capture3` 第 88 条是唯一一次真正加载 `bind-broadband-form.html` 的记录。
- `capture5/fiddler.har`：**当前最重要的一组**（215 个请求，2026-09-18）。
  含两次完整上线流程，确证：
  ① 浏览器实际请求体 `{"phone":...,"uid":"<认证码>","captchaKey":""}`；
  ② 完整跳转链（含 `/ac/oauth2/authorize` 中转）；
  ③ `redirect_uri` 的双层编码形态；
  ④ 全程 **零** 次 `selectBindBroadband` / `addBindBroadband` / `bind-broadband-form`。

> ⚠️ 早期还有两组目录（`capture_20260915_*`、`sso_capture_20260915_152903`）记录了
> openId 时代的诊断过程，但**它们与抓包脚本一同已不在仓库中**（`scripts/` 已删除）。
> 需要那批前端源码时，改从 `capture1/10.10.16.101.har` 里抠
> `broadband.js` / `common.js` / `login.js` 的正文——那是目前唯一仍在库内的第一手来源。

- 各目录**只有 HAR 文件，没有 `capture_log.txt`**（那是已删除的抓包脚本的产物）。

## 15. 安全与注意事项

- 手机号 / 认证码 与 `/etc/portal_login.conf` 按上网密码级别保护：`chmod 600`，不入 git、不截图；
  token 缓存只在 tmpfs（`/tmp/portal_login/`），重启即弃。
- 本文件与所有归档样例保持脱敏；真实凭证只存在路由器本地配置。
- **`captures/` 里的 HAR 全部含真实凭证**（手机号、认证码、JWT）。虽然 `.gitignore`
  已排除，但复制/打包/上传仓库时务必确认它没被带上。要长期归档建议先脱敏。
- 脚本默认禁用代理环境变量（`http_proxy/https_proxy/ALL_PROXY`），认证流量走 WAN 直连；
  开代理可能导致 eportal 内网地址不可达或源 IP 不匹配。
- 自动认证只解决“WAN 口放行”，多设备共享的隐蔽性由上级 README 的 UA2F + rkp-ipid 负责，二者缺一不可。

### 15.1 运维注意（三个要点）

1. **改 `phone` / `user_uid` 无需手动清 token 缓存**：缓存文件里存了凭证指纹
   （`sha256(phone\0uid)` 前 16 位，不存明文），与当前配置不符时
   `TokenManager._load_disk()` 会告警并丢弃、重新换发。`restart` / `reload` 后直接生效。
   只改 `service_name` / 时间窗等无凭证项则完全无关。
   （历史：早期版本无指纹，缓存落在 tmpfs 且 `restart` 不清空，会**静默沿用旧账号会话**。）
2. **认证码填错时的重试频率**：换 token 失败不会写缓存，于是**每一拍都会重试一次登录接口**
   （时间窗内 5s 一次，常态 30s 一次）。这是设计使然，但若日志持续出现
   `loginByPhoneAndUid 未返回 token`，应**先停守护再排查凭证**，避免高频试探登录接口。
   排查：`PYTHONPATH=. python3 -m portal_login status` 看 `last_error`，
   或直接裸 curl 一次（第 4.1 节）。
3. **两个“portal_login”路径别混淆**：配置文件是 **文件** `/etc/portal_login.conf`，
   断网日志在 **目录** `/etc/portal_login/` 下（`outages.jsonl`）。二者互不干扰，
   但删文件时容易误删目录。

### 15.2 已知脆弱点（改版时优先怀疑）

| 脆弱点 | 位置 | 触发条件 | 现象 |
|---|---|---|---|
| `redirect` 必须是最后一个参数 | `portal.py::extract_portal_url` | 平台在 `redirect` 后追加参数 | oauthRedirect 500；**日志会打出完整 redirect_uri 并点名外层参数**（客户端无法根治，站点自身也会失效） |
| 编码层级假设（不 decode） | `portal.py::extract_portal_url` + `_oauth_once` | 平台改成两次编码的 `redirect` 值 | oauthRedirect 500 |
| 现场 B 判定依赖字面 `redirect=` | 同上 | 参数名改成 `redirectUri=` 等 | 日志报 `无法提取 eportal URL`（含原文） |
| 换 token 必须同模块 | `auth.py::_mint` | 平台调整 `/ac/` 与 `/web-app/` 会话表关系 | oauthRedirect 500 |
| 探测三态形态 | `detector.py` | 网关改成 200 无 JS 跳转的页面 | 日志报 `probe_unknown(200)` |

以上任一失效时，按第 10 节的顺序定位，并回第 3 节的交叉矩阵验证。
