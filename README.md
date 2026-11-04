# vesper-mod

从网页或手机，往一个正在跑的 Claude Code 会话里发一句话。

一个 Claude Code mod，加一个很小的文件协议：你的服务把话写成文件，mod 每秒看一次，取走后送进会话，再写一份不带正文的回执。发话的人能知道这句话的结局：送到了、被拒了，还是说不清。

它从 Vesper（一个给 AI 搭的私人前端）里抽出来，那边每天都在用，这里只留通用的部分。

在 Claude Code 2.1.318 的真会话上测过。mod 接口可能随 Claude Code 版本变化，不保证跟版。

## 跑起来

需要 Python 3，只用标准库。先不装 Claude Code 也能跑通整套协议，`demo/fake_mod.py` 是 mod 的替身：

```sh
python3 demo/check.py         # 协议检查，瞬间跑完
python3 demo/run_demo.py      # 完整流程和各种异常，约 15 秒
python3 demo/run_web_demo.py  # 网页服务：鉴权、超时、没有心跳
```

退出码 0 就是全过。

接真会话要开两个终端。

终端 1，安装 mod，起服务：

```sh
STATE_DIR=$(mktemp -d)
SESSION_ID=$(python3 -c "import uuid; print(uuid.uuid4())")
python3 tools/install.py --plugin-dir plugin --state-dir "$STATE_DIR"
echo "$SESSION_ID"
python3 demo/web_server.py --state-dir "$STATE_DIR" --port 8901 --session "$SESSION_ID"
```

终端 2，用安装时打印的路径和上面的 session id 起 Claude Code：

```sh
claude --session-id <session id> --plugin-dir <installed to 后面的路径>
```

打开 `http://127.0.0.1:8901/` 发一句。会话里会出现这句话，页面上是不带正文的回执。

`state_dir` 不能在 git 工作区里，安装会拒绝：Claude Code 对工作区里的插件每次保存都热重载，所以插件要拷一份到 `state_dir/mod/<sha12>/` 再用。改了插件重新安装，会得到一个新目录，跑着的旧拷贝不受影响。

## 一句话的结局

`POST /send` 的返回：

| HTTP | 结果 | 意思 | 怎么办 |
| --- | --- | --- | --- |
| 200 | `acked` | 进了会话。`door` 是 `prompt` 表示闲时提交，是 `note` 表示忙时追加 | 完成 |
| 200 | `refused` | Claude Code 拒收，不会再试 | 告诉用户 |
| 503 | `mod_not_alive` | 5 秒内没有心跳，文件没写 | 等会话起来再发 |
| 504 | `not_delivered` | 5 秒没人取，文件已删 | 可以重发 |
| 504 | `unknown` | mod 取走了但没有回执，这句可能已经进了会话 | 不要自动重发，查 transcript，由人决定 |

## 协议

```
网页 ── POST /send ──▶ 服务 ── 写 ──▶ mod-inbox/<session>/<sha256(msg_id)>.json
                                              │
                                              │  mod 每秒一次：写 .alive，
                                              │  先写 .taking 占位，再读文件
                                              ▼
服务 ◀── 读 ── mod-acks/<sha256(msg_id)>.json ◀── 闲时提交 / 忙时追加进会话
```

- **心跳**：mod 每秒写 `.alive`，服务只认 5 秒内的。
- **占位**：mod 先写 `.taking` 再读文件，后来的 poller 看到标记就跳过，热重载不会把同一句送两次。
- **回执**：`{msg_id, uuid, door}`，或被拒时的 `{msg_id, refused, door}`，都不带正文。
- **删文件**：mod 不删 inbox 文件（`$.fs` 没有删除），服务拿到结果后立刻删。超时先删再看 `.taking`，这样晚起来的 mod 不会把旧话送进去。服务启动时清掉残留文件，清不干净就拒绝启动。
- **`/clear`**：session id 会变，mod 每秒重读并跟过去。demo 服务的 id 是启动时定的，要用新 id 重启，否则返回 503。

## 它不保证什么

- **最多送一次，不是绝不丢。** 结果说不清时报 `unknown`，不猜也不重发。
- **两个 poller 同一瞬间抢，仍可能重复。** `.taking` 是先查再写，mod 环境没有原子创建。先后两个不会。demo 替身用的是 `O_EXCL`，比真 mod 强，别拿替身的强度当保证。
- **`acked` 不等于模型读到了。** 忙时追加的话，如果落在这一轮最后一次模型请求之后，模型读不到。mod 会在回合正常结束时提交一句固定的 nudge，开一轮新的让模型读到它；回合被打断或报错结束时不补，那句可能就没被读到。
- **只有协议自己的文件不留正文。** Claude Code 的 transcript 里全文都在。

复用这套协议，nudge 要一起拿走，不然会得到一个回了成功却没人读的通道。

nudge 这一行你的前端要自己过滤，按结构判断，不要比对文字：`origin.kind` 是 `plugin`，`origin.name` 是本插件名，没有 `asUser`，也不是 `isMeta`。忙时追加的用户那句同样没有 `asUser`，靠 `isMeta` 区分。完整规则在 `docs/advanced.md` 第 5 节。

## 安全

能往会话里打字，就等于能用那台机器的 shell。单用户也要鉴权，因为本机随便一个网页都能 POST 本机端口。

`demo/web_server.py` 的做法：

- 只绑 `127.0.0.1`。
- 所有路由查 `Host`，挡 DNS 重绑定。
- 带 `Origin` 或 `Referer` 的请求，主机名必须是本机。
- `POST /send` 要带 `X-Vesper-Key`，值来自 `VESPER_MOD_TOKEN`；没设置时启动生成一个并打印，页面由服务下发时自动带上。

这只够本机用。要跨机器或上公网，自己加 TLS 和按会话签发的口令。

## 实时预览（可选）

想在回答写完之前就看到内容，在安装拷贝的 `config.json` 里加一行：

```json
"preview_url": "http://127.0.0.1:<port>/api/preview"
```

mod 每 250 毫秒把这一轮的文本块和 thinking 块整份 POST 过去，thinking 块带 `kind: "thinking"`，每个回合结束发一帧 `done: true`。发失败就算了，不排队，也不影响会话。重新安装时这一行会自动带到新拷贝。

接收端要你自己写，demo 服务不接。帧格式在 `docs/advanced.md` 第 7 节。预览帧不带口令，里面是全文，接收端只绑本机。

## 文件

- `plugin/`：真 mod，主体是 `hooks/register.ts`
- `tools/install.py`：按内容哈希安装一份拷贝
- `demo/`：协议实现、mod 替身、迷你服务和页面
- `docs/advanced.md`：`/clear` 重绑、模型和压缩控制、transcript 行归属、预览帧格式

真会话测过的：闲时提交、忙时追加、nudge、`/clear` 和 mod 退出后的 503、回执后文件已删、预览的各种帧。5 秒超时只在 demo 里测过。

## License

MIT
