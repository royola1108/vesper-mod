# vesper-mod 进阶层：文档与伪代码

> v1 的代码库聚焦于最小可用核心（file inbox 协议、真 hook、安装脚本与迷你验证服务）。
> 下面记录完整前端集成所需的进阶层思路与关键契约，用伪代码与协议规范表示。

## 1. 安装：按内容寻址的插件拷贝（已实现于 `tools/install.py`）

目标：装进会话的插件拷贝，必须是完整、不可半途而废、可验证来源的一份。

```
files = {相对路径: 字节 for 插件目录下每个文件}
sha   = sha256(对每个文件按路径排序: 路径 + "\0" + 字节 + "\0")
dest  = state_dir / "mod" / sha[:12]

if state_dir 在 git work tree 里: 拒绝
    # 原因：拷贝落在 work tree 里会被 Claude Code 反复热重载
if dest 已存在:
    幂等返回 dest（名字本身就是内容哈希）
else:
    写到 dest 同级的临时目录
    写 config.json = {state_dir, source_sha: sha, ...(继承上一次的 preview_url)}
    原子 rename 临时目录 -> dest   # 外面永远看不到半成品
```

关键点：安装只是“造出一份不可变拷贝”，**不等于加载进会话**；加载、重启、打开会话仍然是分开的操作步骤。

## 2. `/clear` 后的 session 重绑（已实现于 `plugin/hooks/register.ts`）

问题：`/clear` 之后会话 id 会变，但 `session.start` 事件不一定触发。如果 poller 还绑着旧 id，消息就会写进一个没人读的 inbox。

```
每个 tick（不是只在启动时）:
    id = session.id()
    if id 有效 and id != bound_id:
        bound_id = id
        inbox 目录、.alive 全部切到新 id（preview 块缓存清空、seq 归零）
        # 旧 id 下还没 ack 的消息：不重发
        # .taking 标记还在，说明它已经被取走过一次；
        # 服务端对它只能判 "unknown"，不能猜它成功了
```

## 3. 只留一种 inbox：`user`

v1 只有一种 inbox：`{msg_id, text}`，即“网页发来的话”。不带 `kind` 默认按 `user` 处理，带了其他 `kind` 一律忽略（真 hook 里 `parseInbox` 已写明）。其他 kind 不在 v1 范围内。

## 4. Controls：模型 / effort / compact / clear

和消息 inbox 平行的另一类控制文件，名字形如 `ctl-<sha>.json`。如果前端需要支持切换模型或压缩上下文，mod 可以在同一个 1 秒轮询里顺带处理，但**永远不在 poll 里 await 它**：

```
poll tick:
    写 .alive 心跳
    写 .controls（当前模型 / effort / 上下文余量），放在 .alive 旁边
    扫描 inbox：
        <64 hex>.json      -> 按第 3 节处理用户消息
        ctl-<64 hex>.json  -> 按控制类型处理，不 await：
            模型切换         -> config.set
            effort          -> 档位只能是 [low, medium, high, xhigh, max]
            compact / clear -> command.run
    控制结算后才写 ack：mod-acks/ctl-<sha>.json
    # 控制本身不在 transcript 里另起一行，
    # 只有 Claude Code 自己为这条命令记的行
```

## 5. Transcript 行归属判定（给“读 transcript 的前端”用的规则）

当你的前端从 Claude Code 的 transcript JSONL 里渲染对话、分辨“这行是谁的话”时，**只看结构，不看正文**：

```
一行是“网页用户的话”，当且仅当：
    type == "user"
    origin.kind == "plugin" 且 origin.name == 本插件名
    并且满足以下之一：
      - origin.asUser 是 JSON 布尔 true（空闲时提交的）
      - busy 形式：isMeta 是 true，且 content 是非空 text 块列表

一行是 nudge，当且仅当：
    origin.kind == "plugin"，origin.name == 本插件名，且没有 asUser，且不是 isMeta。
    你的前端应当按这条规则过滤掉 nudge 行，不在聊天界面展示。

注意：
    1. 只靠“没有 asUser”过滤会误杀——忙时追加的用户消息也不带 asUser，
       它靠 isMeta 区分；nudge 是既不带 asUser、也不是 isMeta 的那一行。
    2. 不要用整行字符串比对过滤：transcript 里的行外面还带 Claude Code 的外框，
       整行比对容易失效。
```

这条规则存在的意义：用户正文可以是任何内容（甚至用户自己打出一句和 `NUDGE_TEXT` 一模一样的话），只有结构（`asUser` / `isMeta` / `origin`）才是可信的区分信号。

## 6. v1 故意不做的事

- **多用户隔离、按会话的鉴权**：demo 只有单口令 + `Host` / 来源检查（见根 README 安全一节）。
- **独立数据库**：前端直接读 Claude Code 的 transcript JSONL 就够了，不需要再维护一套消息库。
- **其他消息平台对接**：不是 `vesper-mod` 的一部分。
- **实时文本 preview 的接收端与 `user` 以外的 inbox kind**：hook 一侧的 `preview_url` 插座已在代码里，接收端协议见下方第 7 节。

## 7. 实时输出预览（接收端协议）

v1 代码里已内置 hook 一侧的插座：在安装副本的 `config.json` 里填上 `preview_url`，hook 就会每 250ms 发送快照；不填就不发；文本块和 thinking 块都发，thinking 块带 `kind: 'thinking'` 标记，接收端可以自己决定显不显示。demo 服务没有内置接收端。下面是你自己的前端服务要接这套预览流时需实现的接收端契约。

目的只有一个：不用等一整轮回答写完，网页前端就能实时看到 Claude 正在写出的文本。

做法不是逐 token 推流，而是**定频轮询快照**：hook 每 250ms 把本轮已经写出的文本块和 thinking 块整份 POST 给接收端，接收端再用 SSE 或 WebSocket 推给浏览器。因为每一帧快照本身就是当前回合的完整最新状态，丢一帧完全不用补，下一 tick（250ms 后）重新抓最新快照即可。

```
示意接收路由（占位示例，由你自己的服务实现）：
    POST http://127.0.0.1:<port>/api/preview

配置方式：
    将上述地址填入安装副本 config.json 的 "preview_url" 字段。
    hook 每 tick 检查当前 session id；/clear 后 id 变了会自动清掉旧回合的块、
    seq 归零；每个回合结束（turn.complete）时都发一帧 done: true，随后清空块缓存。

帧格式（JSON Body，与 register.ts 里的 Preview.frame 一致）：
{
  "session_id": "<当前 session id>",
  "seq": 1,           // 每帧严格递增；/clear 重绑后归零
  "turn_id": "<当前回合 id 或 null>",
  "blocks": [
    { "step": 0, "attempt": 1, "index": 0, "text": "正在写出的文本…" },
    { "step": 0, "attempt": 1, "index": 1, "kind": "thinking", "text": "正在思考的内容…" }
  ],
  "done": false       // 回合结束那帧为 true，发完后块被清空
}
```

**块与熔断规则：**
- 块的复合键是 `(step, attempt, index)`：同一块的文本随着流式输出只会往后追加；如果某一步报错重试（`failStep`），对应 `attempt` 的块会被整批丢弃。
- 快照永远是当前回合的全量块列表，不是增量 diff。
- **容量熔断**：单回合总字数（文本块与 thinking 块合计）超过 32,768 字时，本回合的预览撤回（防止超长输出占用内存）：之后的帧 `blocks` 为空，回合结束照发 `done: true`，下一回合恢复。

**接收端只需遵守三条规则：**
1. `seq` 不递增的乱序旧帧直接丢弃；
2. 收到 `done: true`（或从 transcript JSONL 读到正式落盘的回复行）后结束预览态，此前收到的快照均作为临时预览渲染；
3. `blocks` 为空的帧表示撤回，清掉正在显示的预览。
