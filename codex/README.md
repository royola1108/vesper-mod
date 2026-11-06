# codex-shared

一个本机小网页，接到**已经在跑的** Codex app-server 常驻 daemon：列线程、接上某个线程、发话、看实时事件。终端里的 Codex CLI 用 `--remote` 接同一个 daemon 时，看到的是同一个线程。

只用 Python 标准库。`client.py`（WebSocket + JSON-RPC 客户端，约 150 行）、`server.py`（HTTP 服务，约 150 行）、`index.html`（网页，一个文件）。

## 为什么接常驻 daemon，而不是自己起一个

很多封装自己拉起一个 `codex app-server`（stdio）当子进程。那样运行中的会话属于这个子进程：封装一退出，正在跑的回合就断了；终端里的 CLI 也看不到这个回合，最多事后从历史恢复。

接常驻 daemon 时，线程、工具、沙箱和审批策略都归 daemon；网页、CLI 只是它的客户端。关掉网页，线程还在；CLI 再接上来是同一个线程；一个客户端正在跑的回合，另一个客户端能看到事件。代价：网页不拥有 daemon，不能代为重启它，也不改审批策略。

## 怎么跑

需要已装好的 `codex` 和 Python 3.10+。

1. 起 daemon（前台常驻，单独一个终端，保持开着）：

```
mkdir -p ~/.local/state/codex-shared
codex app-server --listen unix://$HOME/.local/state/codex-shared/app.sock
```

`codex app-server daemon start` 是官方的后台托管方式（见 `codex app-server daemon --help`），本包没有用它测过，默认 socket 路径请按你的版本确认。

2. 在终端用 Codex 接上 daemon。以前的本地线程直接就能在网页上看到；一个线程都没有的话，在这里先说一句，本包不创建线程：

```
codex --remote unix://$HOME/.local/state/codex-shared/app.sock
```

3. 起网页（常驻，再开一个终端）：

```
CODEX_SHARED_PASSWORD='换成你自己的口令' python3 server.py --endpoint unix://$HOME/.local/state/codex-shared/app.sock --port 8765
```

不设口令则随机生成并打印一次。

4. 浏览器打开 `http://127.0.0.1:8765/`，输入口令，左边选线程，下面输入框发话。这时第 2 步的终端能看到网页发的话，终端发的话网页也能看到。新线程点「刷新」。

关掉终端后再接同一个线程：

```
codex resume <线程 id> --remote unix://$HOME/.local/state/codex-shared/app.sock
```

如果用官方的 remote control 起 daemon（`codex remote-control start`，或 `codex app-server daemon enable-remote-control`），之后在终端直接打开 `codex` 就在 daemon 里，不用再加 `--remote`。这种方式本包没有单独测过，`--endpoint` 要填的 socket 路径请按你的版本确认。

`--endpoint` 也接受 `ws://127.0.0.1:端口`（daemon 用 `--listen ws://127.0.0.1:端口` 起）；daemon 若要求 bearer token，用 `--token-env 环境变量名` 传入，不要写进命令行。非本机的明文 `ws://` 会被拒绝。

`server.py` 只连一次 daemon，daemon 重启后要重启 `server.py`。页面的 stop 会停掉当前回合，不管是哪边发起的。

## 审批请求

协议上任何接到 daemon 的客户端都能回答审批请求，所以前端完全可以做审批按钮，谁先答谁算。这个小包为了简单没做，所以本页**不做审批界面，也不会放行**；要做的话，在 `server.py` 的 `on_request` 里把请求转给页面，再把用户的选择作为回复返回。daemon 向本页发来审批类请求（命令、文件改动等）时：

- 回合是本页发起的：本页立刻回复拒绝（`decline`），页面上显示一条红色提示，回合不会卡住；
- 回合是别的客户端（如终端 CLI）发起的：本页不回复，只在页面显示提示，由发起的那个客户端去答。否则网页会抢在你之前替你拒绝。

其他需要用户回答的请求（提问、MCP 征询、动态工具）同理：自己的回合回错误，别人的回合不理。

## 安全

- **能往 Codex 的 socket 里打字 = 能让它执行 shell 命令**（取决于该线程的沙箱和审批策略）。本页等于给浏览器开了这个能力，所以：
- 只绑 `127.0.0.1`；必须口令登录（cookie `HttpOnly`、`SameSite=Strict`）；每个请求校验 `Host`（防 DNS 重绑定）；POST 必须带匹配的 `Origin`。
- 登录状态只在内存里，重启就要重新登录；单用户；没有限流，错误口令只延迟 1 秒，所以口令别太短。
- 页面是明文 HTTP，只在本机回环上用；要从别处访问，请自己用 SSH 端口转发，不要改成监听其他地址。
- socket 的文件权限决定谁能连它，本机同用户的任何进程都能连。`--listen unix://路径` 在 0.165.0 上建的是符号链接，实际文件在 `/tmp/codex-daemon-<uid>/`，不用自己挪，也别放宽它的权限。

## 测试

```
python3 -m unittest discover -s tests -v
```

测试用 `fake_daemon.py`（假的 app-server，只回罐装 JSON-RPC）覆盖：握手与帧、Host/Origin/口令校验、列线程、接上、发话、实时事件、自己回合的审批被拒、别人回合的审批不答。不需要 Codex，不碰任何真实 daemon。

## 测过的版本

Codex CLI `0.165.0`，真 daemon 和真模型：网页接上线程、发话、回合完成；CLI 用 `--remote` 接同一个 daemon 能看到网页发的话；关掉 CLI 再接，线程还在；CLI 和网页同时开着时，两边发的话对方都实时看得到。

没测过的：真 Codex 的审批请求（这部分只在假 daemon 上测过）、`codex app-server daemon start`。

## 接口可能会变

app-server 协议标着 experimental，字段和方法名随 Codex 版本变化（本包用到 `initialize`、`thread/list`、`thread/resume`、`turn/start`、`turn/interrupt` 和几个 `item/*`、`turn/*` 通知）。升级 Codex 后如果列不出线程或收不到事件，先对照 `codex app-server generate-json-schema` 的输出。空线程（还没有第一个回合）不能从另一条连接 resume，所以本包只接已有线程。
