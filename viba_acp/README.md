# viba_acp：用 viba 代码调用 ACP agent

## 1. 概念

一次调用涉及三方：

- **viba 程序**调用 `viba_acp.new_session` 与 `viba_acp.send_prompt`；agent 的启动命令与通信协议不在程序内，
  程序只用 `AcpBackend` 的变体指明要哪一种后端。
- **服务**实现这些操作（[`agent_service.py`](agent_service.py)），为每个用到的后端维持一个 agent 进程与
  它的 ACP 会话。
- **agent** 是外部进程，由 [`agents.py`](agents.py) 给出的命令启动，通过 ACP 通信。

ACP 是客户端与 agent 之间的会话协议。服务启动 agent 进程，`initialize` 协商版本与能力；`session/new`
建立一个会话，此后每次 `session/prompt` 是该会话上的一轮。轮内 agent 通过 `session/update` 流式报告，
在执行工具前可以通过 `session/request_permission` 请求客户端授权。

会话属于服务，不属于 viba 数据：viba 数据里走的是 `Session`——它记录会话连的是哪种 ACP 后端
（`AcpBackend`，示例里是 `ClaudeAgentBackend`）与后端的会话编号（`SessionId`）；会话本身保存在宿主的
`Conversation` 中（[`acp_client.py`](acp_client.py)），随服务进程结束而关闭。示例开两个会话（各带自己的
wiki 路径）、各执行一轮。

后端的会话上下文（`AgentSessionContext`）里的 `wiki_path` 在开会话、载入会话与接回会话时交给 agent：
agent 声明了 `sessionCapabilities.systemPrompt` 就作为 system prompt 发；没声明（dsh 与 trae 当前如此）就先发一轮
上下文提示——提示里只要它记下，不要用工具、不要作答——答案丢弃。

接口的类型名取自 ACP 的 schema，操作名对应 ACP 的方法：`new_session` 是 `session/new`，`send_prompt` 是
`session/prompt`。

## 2. 接口

接口声明在 [`viba_acp.viba.md`](../viba_acp.viba.md)，是三个模块：`viba_acp.public`（对外名字的转导出）、
`viba_acp.types`（类型）、`viba_acp.apis`（操作）。外部只 import `viba_acp.public`，不直接依赖另两个。

三条约定：

1. **每个操作先收环境。** `$env Env` 是第一个参数，与 viba 的其他调用一致；开会话与接回还要给
   `AcpBackend`，后端上下文里的 `wiki_path` 会写进 system prompt，其余参数由服务决定（工作目录、MCP
   服务器），viba 一侧不传。
2. **会话是数据。** `new_session` 的答案是 `Session`（后端 + 编号）；`send_prompt` 收 `$session Session`
   与一组 `ContentBlock`，在它所指的会话上执行。
3. **返回值是 `PromptResponse`。** 结束原因在 `$stop_reason`，token 用量在 `$usage`；消息文本不在
   返回值里，由 `$on_update` 逐条收到（第 3.3 节）。

## 3. 调用

### 3.1 运行入口

```bash
python3 demo/viba_acp/run.py                  # Claude Agent SDK 的 ACP 适配器
python3 demo/viba_acp/run.py --agent dsh      # DeepSeek Harness 的 acp profile
python3 demo/viba_acp/run.py --agent trae     # TraeCode CLI 的 ACP 服务器
python3 demo/viba_acp/run.py --fresh          # 从空 store 开始
```

这些命令要在有凭据的环境里跑：`claude` 需要环境里的 `ANTHROPIC_BASE_URL` 与 `ANTHROPIC_AUTH_TOKEN`（先
source 自己的 env 文件），`dsh` 需要 `DSH_EXECUTABLE` 或 `PATH` 上的 `dsh`。没有凭据时 `run.py` 退出 1，
它的报告里写明 agent 的认证错误，store 里只留下开头那几步。

store 默认按 agent 分开（`demo/viba_acp/.store/<agent>`），同一份程序换 agent 不会把上一份的答案回放出来。

`run.py` 把两份 `.viba.md` 文档按 `viba <name>` 代码块拼成模块：接口文档的 `viba public`/`viba types`/
`viba apis` 落到 `<store>/source/viba_acp/viba_acp/` 下的三个文件，块外的说明成为该文件的注释；示例程序
按标题落到 `<store>/source/viba_acp/two_wikis.viba`。示例程序构造两个 `ClaudeAgentBackend`，
各带自己的会话上下文（`$wiki_path`）；`--agent` 在建模块时把两处变体名换成对应实现，agent 收不收
system prompt 由客户端处理（见第 1 节），所以同一份程序能跑三种 agent。然后
启动 [`distributed/scheduler.py`](../../demo-viba/distributed/scheduler.py) 与一个服务进程（`agent_service`）。它产出
两项结果：

- stdout：每轮一行 JSON，最后一行为最终结果（`{"outcome": "ok", "rounds": 1, …}`）；
- `--trace`：每行一条 ACP 消息，`<store>/agent-trace.jsonl`。

退出码 0 表示调度达到 `ok`，1 表示未达到（`stuck`、`unfinished`、`broken`，与
[`distributed/README.md`](../../demo-viba/distributed/README.md) 一致）。

### 3.2 viba 程序的调用方式

```viba
import viba_acp.public as viba_acp

__decl__ = (viba_acp.PromptResponse * viba_acp.PromptResponse) <- $env Env

args = __get_args__ << __decl__

wiki_a =
    Object
  * $wiki_path "../demo-viba/viba/viba/wiki"
  * $version nil

wiki_b =
    Object
  * $wiki_path "../demo-viba/viba/viba/core"
  * $version nil

backend_a =
    $ClaudeAgentBackend Object
  * $context wiki_a

backend_b =
    $ClaudeAgentBackend Object
  * $context wiki_b

session_a =
     viba_acp.new_session
  << $env ($sub_env << args.env << "c0")
  << $backend backend_a

session_b =
     viba_acp.new_session
  << $env ($sub_env << args.env << "c1")
  << $backend backend_b

question =
    $TextContent Object
  * $text "In one sentence, state the wiki path this session was given."
  * $annotations nil

on_notification =
     void
  <- $env Env
  <- $notification viba_acp.SessionNotification
  <- { record one session/update notification }

answer_a =
     viba_acp.send_prompt
  << $env ($sub_env << args.env << "c2")
  << $session session_a
  << $prompt (ListLiteral[question])
  << $on_update on_notification
  << $sub_env_prefix_on_update "update"

answer_b =
     viba_acp.send_prompt
  << $env ($sub_env << args.env << "c3")
  << $session session_b
  << $prompt (ListLiteral[question])
  << $on_update on_notification
  << $sub_env_prefix_on_update "note"

__impl__ = answer_a * answer_b
```

两个 backend 的 `wiki_path` 由服务交给会话：agent 收 system prompt 就作为 `session/new`、
`session/load`、`session/resume` 的 `systemPrompt`（`append` 模式）发，不收就在建/接/载会话后补一轮
上下文提示（只要它记下，不要用工具、不要作答）。

`$on_update` 是函数型参数，和 `if` 的分支一样，实参写成一个**仍欠环境的那次调用**（`on_notification`）；服务持有的是这个调用，不是它的值。每条通知到达时，服务为它建一个子环境、再用一次新的调用对象去跑；`_HostArgument` 一次只答一次，同一份对象不能拿来跑第二条。示例里的 `on_notification` 是宿主实现的一步，`agent_service.py` 把每条通知记到 trace 旁边（`<trace>.updates`）。

`$sub_env_prefix_on_update` 决定那个子环境的名字前缀：`answer_a` 传 `"update"`，通知记在
`root/c2/update1`…；`answer_b` 传 `"note"`，通知记在 `root/c3/note1`…。

四次调用都在各自的子环境中执行（`c0`、`c1` 开两个会话，`c2`、`c3` 各执行一轮）。该子环境的数据路径即
本次调用的**存储路径**：答案记录在该路径下，同一 store 上的后续运行直接从该路径回放，不再向 agent 发送
请求（第 4 节）。两个响应用积一起返回（`answer_a * answer_b`）：按需求值时先左后右，所以两轮都会执行。

程序只引入 `viba_acp.public`。这些操作由服务提供；程序不引入服务，也不知道启动命令。

### 3.3 ACP 会话

一个服务进程与一个 agent 之间只有一条连接；示例在它上面开两个会话。时序如下（`--trace` 逐条记录；
`× n` 表示若干同型的 `session/update`）：

```text
client → initialize
agent  → protocolVersion 1、agentInfo、authMethods
client → session/new        {cwd, mcpServers: []}
agent  → {sessionId}
client → session/prompt     {sessionId, prompt: [text]}
agent  → session/update     {sessionUpdate: agent_message_chunk}  × n
agent  → {stopReason}
```

一条 `session/prompt` 请求与一轮的答复（摘自 trace）：

```json
{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"sess-1","prompt":[{"type":"text","text":"In one sentence, state the wiki path this session was given."}]}}
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"sess-1","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"The wiki path given to this session is `../demo-viba/viba/viba/wiki`."}}}}
{"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn"}}
```

以上规则由本客户端确定，[`acp_client.py`](acp_client.py) 的模块说明给出同一套：

- **分帧**：JSON-RPC 2.0，每条消息一行，UTF-8；agent 的 stdout 只能写 ACP 消息，日志写 stderr
  （[ACP transports](https://agentclientprotocol.com/protocol/v1/transports)）。
- **握手**：`protocolVersion: 1`；`fs` 与 `terminal` 均为 `false`，即客户端不代 agent 读写文件或执行
  命令。agent 报告的版本不是 1 时，客户端终止（第 5 节）。
- **会话**：每个服务进程 `initialize` 与 `session/new` 各一次；`send_prompt` 的 `$session` 必须与这个会话
  一致，否则报错。进程退出时关闭连接。
- **响应文本**：`session/update` 中 `agent_message_chunk` 的 `content.text` 按到达顺序拼接；其他
  update（工具调用、计划、思考）只计数，不计入响应文本。
- **权限请求**：agent 通过 `session/request_permission` 请求授权，客户端按 `permissions` 作出决定。
  默认 `reject`：优先选择 `reject_once`，其次 `reject_always`，两者皆无时返回 `cancelled`。该进程无人
  值守，因此默认不批准；`VIBA_AGENT_PERMISSION=allow_once` 表示调用方已作出批准决定。
- **结束原因**：`session/prompt` 的响应必须携带五个 stop reason 之一（`end_turn`、`max_tokens`、
  `max_turn_requests`、`refusal`、`cancelled`）。其他值不属于 ACP 定义，客户端报错，不将其作为响应
  接受。
- **超时**：一次 prompt 超过 `VIBA_AGENT_TIMEOUT`（默认 30 秒）时，客户端发送 `session/cancel`，再等待
  5 秒以获取 `cancelled` 响应；仍未收到则报错。调度器在 60 秒时终止服务进程，因此该上限设在其之前。

本客户端不提供 `fs` 与 `terminal` 能力：它适合纯文本的 agent，不适合依赖宿主文件系统与终端的编码代理。
需要后者时，必须在客户端实现并宣告这两组能力，同时处理 agent 发起的对应请求。

agent 发起一次授权请求时，客户端按策略决定；消息形如：

```json
{"jsonrpc":"2.0","id":1001,"method":"session/request_permission","params":{"sessionId":"sess-1","toolCall":{"toolCallId":"call-1"},"options":[{"optionId":"allow-once","name":"Allow once","kind":"allow_once"},{"optionId":"reject-once","name":"Reject","kind":"reject_once"}]}}
{"jsonrpc":"2.0","id":1001,"result":{"outcome":{"outcome":"selected","optionId":"reject-once"}}}
```

## 4. 运行结果

`python3 demo/viba_acp/run.py` 的 stdout（两行，`value` 截断）：

```text
{"round": 1, "answered": {...}, "runs": {"acp": {"service": "acp", "phase": "run",
  "api": [{"path": "root/c0", "computed": true, "value": "$backend (…) * $session_id \"sess-1\" …"},
          {"path": "root/c1", "computed": true, "value": "$backend (…) * $session_id \"sess-2\" …"},
          {"path": "root/c2", "computed": true, "value": "$stop_reason \"end_turn\" …"},
          {"path": "root/c3", "computed": true, "value": "$stop_reason \"end_turn\" …"}],
  "result": "ok", "value": "$stop_reason \"end_turn\" * $usage nil * $stop_reason \"end_turn\" * $usage nil"}}}
{"outcome": "ok", "rounds": 1, "service": "acp", "value": "$stop_reason \"end_turn\" * $usage nil * $stop_reason \"end_turn\" * $usage nil"}
```

store 中每一步对应一份快照：

```text
<store>/source/viba_acp/          # 示例程序；模块在 viba_acp/ 下（每次运行重新生成）
<store>/root/c0/value.viba        # 第一个会话的 Session
<store>/root/c1/value.viba        # 第二个会话的 Session
<store>/root/c2/value.viba        # 第一次 prompt 的 PromptResponse
<store>/root/c3/value.viba        # 第二次 prompt 的 PromptResponse
<store>/failure/round-1.viba      # 本轮的失败状态（无递延时为空记录）
```

在同一 store 上再次运行：`computed` 均为 `false`，trace 中没有 `session/prompt`；答案从 `value.viba`
回放。这是 `service.recorded`（`replayed`）的作用：agent 步骤与其他步骤一样，只执行一次。

## 5. 边界情形

这些是客户端自己处理的情形，行为如下；受控的 ACP 服务删掉后，套件只跑真实 agent，不再覆盖它们：

- **工具调用与权限被拒绝**：客户端按默认策略回 `reject_once`，工具调用记为 `failed`，本轮仍以
  `end_turn` 结束。
- **超时**：一轮 prompt 超过 `VIBA_AGENT_TIMEOUT`（默认 30 秒）时，客户端发送 `session/cancel`，再等
  `stopReason: "cancelled"`。
- **协议外的 stop reason**：客户端报 `AcpError`，指明该值不是 ACP 定义的结束原因。
- **不同的 ACP 版本**：客户端报 `AcpError`，指明 agent 报告的版本，而客户端只支持 1。
- **agent 无法启动或已退出**：`initialize` 收到 EOF，客户端报 `AcpError`，并附退出码与 stderr 末行。

## 6. 接入真实 agent

三个真实 agent 都已接好，命令与环境在 [`agents.py`](agents.py)，用 `--agent` 选（第 3.1 节：它决定建模块
时把示例程序的 `AcpBackend` 变体换成哪一个）。代码里没有本机路径：命令从 `PATH` 或环境变量解析，凭据取自
启动 `run.py` 的那个环境。

| `--agent` | 命令 | 凭据 |
|---|---|---|
| `claude` | `npx -y @agentclientprotocol/claude-agent-acp` | 环境里的 `ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`；`CLAUDE_CODE_EXECUTABLE` 或 `PATH` 里的 Claude CLI |
| `dsh` | `dsh acp`（`DSH_EXECUTABLE` 或 `PATH` 里的 `dsh`） | `~/.dsh` 里已有的 DeepSeek 凭据 |
| `trae` | `trae-cli acp serve`（`TRAE_EXECUTABLE` 或 `PATH` 里的 `trae-cli`） | `~/.trae` 里已有的 Trae 登录 |

`claude` 的适配器是 Claude Agent SDK 的 ACP 实现（[仓库](https://github.com/agentclientprotocol/claude-agent-acp)）；
它会去找 SDK 附带的 Claude CLI，`agents.py` 用 `PATH` 里的 `claude` 填 `CLAUDE_CODE_EXECUTABLE`。`dsh`
用 `dsh acp`，CLI 不在 `PATH` 里时用 `DSH_EXECUTABLE` 指过去。`trae` 用 `trae-cli acp serve`，同理可用
`TRAE_EXECUTABLE` 指过去；它的 ACP 不接受 system prompt（`sessionCapabilities` 里没有 `systemPrompt`），
上下文走补的那一轮提示。

同一份 viba 程序跑三个真实 agent：

```bash
python3 demo/viba_acp/test_agents.py
```

它依次跑 `claude`、`dsh`、`trae`，检查调度达到 `ok`、`initialize` 自报的 agent 名、两个会话各自的 wiki 与回答、
store 里两次会话与两次 prompt 的记录，以及同一 store 重跑时全部回放。缺命令的 agent 会跳过。

也可以先用客户端单独验证一个命令：客户端发送一次请求并输出 JSON。

```bash
python3 viba_acp/acp_client.py --agent "npx -y @agentclientprotocol/claude-agent-acp" \
    --permission allow "hello"
python3 viba_acp/acp_client.py --agent "dsh acp" "hello"
```

输出包含 `text`、`session`、`stop` 与 `agent`（`initialize` 中自报的名字与版本）。默认策略拒绝 agent 的
全部工具调用；如需允许，先确认命令：`VIBA_AGENT_PERMISSION=allow_once python3 demo/viba_acp/run.py
--agent claude`。在允许其修改文件之前，应阅读 ACP 的[安全说明](https://agentclientprotocol.com/protocol/v1/security)。

## 7. 文件

| 文件 | 说明 |
|---|---|
| [`viba_acp.viba.md`](../viba_acp.viba.md) | 三个模块：`public.viba`、`types.viba`、`apis.viba` |
| [`agent_service.py`](agent_service.py) | 服务那一侧：会话与操作，按 `AcpBackend` 变体启动对应 agent |
| [`agents.py`](agents.py) | 两个 agent 的命令、环境与可用性判断 |
| [`acp_client.py`](acp_client.py) | ACP 客户端：stdio 分帧、会话、prompt、权限、取消 |
| [`demo/viba_acp.viba.md`](../demo/viba_acp.viba.md) | 示例程序 `two_wikis.viba` 的文档 |
| [`demo/viba_acp/run.py`](../demo/viba_acp/run.py) | 运行入口：拼模块、调度、服务、store、trace |
| [`demo/viba_acp/test_agents.py`](../demo/viba_acp/test_agents.py) | 同一份程序跑三个真实 agent 的集成检查 |

运行方式：

```bash
python3 demo/viba_acp/run.py
python3 demo/viba_acp/test_agents.py
```

套件运行 `run.py`（与第 3.1 节相同），读取它的调度报告、trace 和 store：服务进程只 `initialize` 一次、
开两个会话，两个会话各自的 wiki 送到 agent；每个会话一轮 `session/prompt`；两个会话与两轮响应按调用路径
记下来，程序返回两个响应；同一 store 上再次运行时不向 agent 发送请求。
