# codex-shared

一个本机小网页,接到**已经在跑的** Codex app-server 常驻 daemon:列线程、接上某个线程、发话、看实时事件。终端里的 Codex CLI 用 `--remote` 接同一个 daemon 时,看到的是同一个线程。

只用 Python 标准库。`client.py`(WebSocket + JSON-RPC 客户端,约 150 行)、`server.py`(HTTP 服务,约 150 行)、`index.html`(网页,一个文件)。

## 为什么接常驻 daemon,而不是自己起一个

很多封装自己拉起一个 `codex app-server`(stdio)当子进程。那样会话属于这个子进程:封装一退出,会话就没了,终端里的 CLI 也看不到它。

接常驻 daemon 时,线程、工具、沙箱和审批策略都归 daemon;网页、CLI 只是它的客户端。关掉网页,线程还在;CLI 再接上来是同一个线程;一个客户端正在跑的回合,另一个客户端能看到事件。代价:网页不拥有 daemon,不能代为重启它,也不能替它决定审批。

## 怎么跑

需要已装好的 `codex` 和 Python 3.10+。

1. 起 daemon(前台,常驻命令,单独一个终端,保持开着;socket 放在自己的私有目录,不要放 `/tmp`):

```
codex app-server --listen unix://$HOME/.local/state/codex-shared/app.sock
```

`codex app-server daemon start` 是官方的后台托管方式(见 `codex app-server daemon --help`),本包没有用它测过,默认 socket 路径请按你的版本确认。

2. 起网页(常驻命令,另一个终端):

```
CODEX_SHARED_PASSWORD='换成你自己的口令' python3 server.py --endpoint unix://$HOME/.local/state/codex-shared/app.sock --port 8765
```

不设口令则随机生成并打印一次。

3. 浏览器打开 `http://127.0.0.1:8765/`,输入口令,左边选线程,下面输入框发话。

4. 终端里接同一个 daemon(再开一个终端):

```
codex resume <线程 id> --remote unix://$HOME/.local/state/codex-shared/app.sock
```

`--endpoint` 也接受 `ws://127.0.0.1:端口`(daemon 用 `--listen ws://127.0.0.1:端口` 起);daemon 若要求 bearer token,用 `--token-env 环境变量名` 传入,不要写进命令行。非本机的明文 `ws://` 会被拒绝。

## 审批请求

本页**不做审批界面,也不会放行**。daemon 向本页发来审批类请求(命令、文件改动等)时:

- 回合是本页发起的:本页立刻回复拒绝(`decline`),页面上显示一条红色提示,回合不会卡住;
- 回合是别的客户端(如终端 CLI)发起的:本页不回复,只在页面显示提示,由发起的那个客户端去答。否则网页会抢在你之前替你拒绝。

其他需要用户回答的请求(提问、MCP 征询、动态工具)同理:自己的回合回错误,别人的回合不理。

## 安全

- **能往 Codex 的 socket 里打字 = 能让它执行 shell 命令**(取决于该线程的沙箱和审批策略)。本页等于给浏览器开了这个能力,所以:
- 只绑 `127.0.0.1`;必须口令登录(cookie `HttpOnly`、`SameSite=Strict`);每个请求校验 `Host`(防 DNS 重绑定);POST 必须带匹配的 `Origin`。
- 口令存在内存里,会话 cookie 重启即失效;没有多用户,没有限流以外的防暴力措施(错误口令延迟 1 秒)。
- 页面是明文 HTTP,只在本机回环上用;要从别处访问,请自己用 SSH 端口转发,不要改成监听其他地址。
- daemon 的 socket 文件权限决定谁能连它(`--listen unix://路径` 在 0.165.0 上建的是符号链接,实际文件在 `/tmp/codex-daemon-<uid>/`);不要把它放进世界可读写的目录。本机同用户的任何进程都能连。
- 本包不做 TG、不做审批 UI、不做多用户、不存状态库。

## 测试

```
python3 -m unittest discover -s tests -v
```

测试用 `fake_daemon.py`(假的 app-server,只回罐装 JSON-RPC)覆盖:握手与帧、Host/Origin/口令校验、列线程、接上、发话、实时事件、自己回合的审批被拒、别人回合的审批不答。不需要 Codex,不碰任何真实 daemon。

## 测过的版本

Codex CLI `0.165.0`,真 daemon 和真模型:网页接上线程、发话、回合完成;CLI 用 `--remote` 接同一个 daemon 能看到网页发的话;关掉 CLI 再接,线程还在;CLI 和网页同时开着时,两边发的话对方都实时看得到。

没测过的:真 Codex 的审批请求(这部分只在假 daemon 上测过)、`codex app-server daemon start`。

## 接口可能会变

app-server 协议标着 experimental,字段和方法名随 Codex 版本变化(本包用到 `initialize`、`thread/list`、`thread/resume`、`turn/start`、`turn/interrupt` 和几个 `item/*`、`turn/*` 通知)。升级 Codex 后如果列不出线程或收不到事件,先对照 `codex app-server generate-json-schema` 的输出。空线程(还没有第一个回合)不能从另一条连接 resume,这是 Codex 的行为;本包不创建线程,只接已有的。
