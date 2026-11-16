# viba_acp：用 viba 代码调用 ACP agent

## 1. 概念

一次调用涉及三方：

- **viba 程序**调用 `viba_acp.new_session` 与 `viba_acp.send_prompt`；agent 的名字、启动命令与通信协议不在程序内。
- **服务**实现这两个操作（[`agent_service.py`](agent_service.py)），并在进程生命周期内维持一个 agent
  进程与一条 ACP 会话。
- **agent** 是外部进程，由 [`agents.py`](agents.py) 给出的命令启动，通过 ACP 通信；`VIBA_AGENT`
  选哪一个。

ACP 是客户端与 agent 之间的会话协议。服务启动 agent 进程，`initialize` 协商版本与能力；`session/new`
建立一个会话，此后每次 `session/prompt` 是该会话上的一轮。轮内 agent 通过 `session/update` 流式报告，
在执行工具前可以通过 `session/request_permission` 请求客户端授权。

会话属于服务，不属于 viba 数据：`SessionId` 是走数据的那份编号，会话本身保存在宿主的 `Conversation` 中
（[`acp_client.py`](acp_client.py)），随服务进程结束而关闭。示例开一个会话、执行一轮。

接口的类型名取自 ACP 的 schema，操作名对应 ACP 的方法：`new_session` 是 `session/new`，`send_prompt` 是
`session/prompt`。

## 2. 接口

接口声明在 [`viba_acp.viba.md`](../viba_acp.viba.md)，是三个模块：`viba_acp.public`（对外名字的转导出）、
`viba_acp.types`（类型）、`viba_acp.apis`（操作）。外部只 import `viba_acp.public`，不直接依赖另两个。

三条约定：

1. **每个操作先收环境。** `$env Env` 是第一个参数，与 viba 的其他调用一致；`new_session` 的其余参数由
   服务决定（工作目录、MCP 服务器），viba 一侧不传。
2. **会话编号是数据。** `new_session` 的答案是 `SessionId`；`send_prompt` 收 `$session SessionId` 与一组
   `ContentBlock`，在编号所指的会话上执行。
3. **返回值是 `PromptResponse`。** 结束原因在 `$stop_reason`，token 用量在 `$usage`；消息文本不在
   返回值里，由 `$on_update` 逐条收到（第 3.3 节）。

## 3. 调用

### 3.1 运行入口

```bash
python3 demo/viba_acp/run.py                  # 脚本化 agent
python3 demo/viba_acp/run.py --agent claude   # Claude Agent SDK 的 ACP 适配器
python3 demo/viba_acp/run.py --agent dsh      # DeepSeek Harness 的 acp profile
python3 demo/viba_acp/run.py --fresh          # 从空 store 开始
```

store 默认按 agent 分开（`demo/viba_acp/.store/<agent>`），同一份程序换 agent 不会把上一份的答案回放出来。

`run.py` 把两份 `.viba.md` 文档按 `viba <name>` 代码块拼成模块：接口文档的 `viba public`/`viba types`/
`viba apis` 落到 `<store>/source/viba_acp/viba_acp/` 下的三个文件，块外的说明成为该文件的注释；示例程序
按标题落到 `<store>/source/viba_acp/explain_then_shorten.viba`。然后
启动 [`distributed/scheduler.py`](../../demo-viba/distributed/scheduler.py) 与一个服务进程（`agent_service`）。它产出
两项结果：

- stdout：每轮一行 JSON，最后一行为最终结果（`{"outcome": "ok", "rounds": 1, …}`）；
- `--trace`：每行一条 ACP 消息，`<store>/agent-trace.jsonl`。

退出码 0 表示调度达到 `ok`，1 表示未达到（`stuck`、`unfinished`、`broken`，与
[`distributed/README.md`](../../demo-viba/distributed/README.md) 一致）。

### 3.2 viba 程序的调用方式

```viba
import viba_acp.public as viba_acp

__decl__ = viba_acp.PromptResponse <- $env Env

args = __get_args__ << __decl__

session = viba_acp.new_session << $env ($sub_env << args.env << "c0")

question =
    $TextContent Object
  * $text "Explain in one sentence what a type class is."

on_notification =
     void
  <- $env Env
  <- $notification viba_acp.SessionNotification
  <- { handle one session/update notification }

__impl__ =
     viba_acp.send_prompt << $env ($sub_env << args.env << "c1")
  << $session session
  << $prompt (ListLiteral[question])
  << $on_update on_notification
```

`$on_update` 是函数型参数，和 `if` 的分支一样，实参写成一个**仍欠环境的那次调用**（`on_notification`）；服务持有的是这个调用，不是它的值。每条通知到达时，服务为它建一个子环境、再用一次新的调用对象去跑；`_HostArgument` 一次只答一次，同一份对象不能拿来跑第二条。示例里的 `on_notification` 是宿主实现的一步，`agent_service.py` 把每条通知记到 trace 旁边（`<trace>.updates`）。

两次调用都在各自的子环境中执行（`c0`、`c1`）。该子环境的数据路径即本次调用的**存储路径**：答案记录在
该路径下，同一 store 上的后续运行直接从该路径回放，不再向 agent 发送请求（第 4 节）。

程序只引入 `viba_acp.public`。这些操作由服务提供；程序不引入服务，也不知道启动命令。

### 3.3 ACP 会话

一个服务进程与 agent 之间只有一条连接、一个会话。时序如下（`--trace` 逐条记录；`× n` 表示若干同型的
`session/update`）：

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
{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"sess-1","prompt":[{"type":"text","text":"Explain in one sentence what a type class is."}]}}
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"sess-1","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"scripted-agent: Explain in one"}}}}
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

`scripted_agent.py --tool-call` 会在每轮执行一次工具调用，并在执行前请求授权，用于覆盖这条路径。一次
授权请求与客户端的决定：

```json
{"jsonrpc":"2.0","id":1001,"method":"session/request_permission","params":{"sessionId":"sess-1","toolCall":{"toolCallId":"call-1"},"options":[{"optionId":"allow-once","name":"Allow once","kind":"allow_once"},{"optionId":"reject-once","name":"Reject","kind":"reject_once"}]}}
{"jsonrpc":"2.0","id":1001,"result":{"outcome":{"outcome":"selected","optionId":"reject-once"}}}
```

## 4. 运行结果

`python3 demo/viba_acp/run.py` 的 stdout（两行，`value` 截断）：

```text
{"round": 1, "answered": {...}, "runs": {"acp": {"service": "acp", "phase": "run",
  "api": [{"path": "root/c0", "computed": true, "value": "\"sess-1\""},
          {"path": "root/c1", "computed": true, "value": "$stop_reason \"end_turn\" …"}],
  "result": "ok", "value": "$stop_reason \"end_turn\" …"}}}
{"outcome": "ok", "rounds": 1, "service": "acp", "value": "$stop_reason \"end_turn\" …"}
```

store 中每一步对应一份快照：

```text
<store>/source/viba_acp/          # 示例程序；模块在 viba_acp/ 下（每次运行重新生成）
<store>/root/c0/value.viba       # new_session 的 SessionId
<store>/root/c1/value.viba       # prompt 的 PromptResponse
<store>/failure/round-1.viba     # 本轮的失败状态（无递延时为空记录）
```

在同一 store 上再次运行：`computed` 均为 `false`，trace 中没有 `session/prompt`；答案从 `value.viba`
回放。这是 `service.recorded`（`replayed`）的作用：agent 步骤与其他步骤一样，只执行一次。

## 5. 边界情形

[`test_agent.py`](../demo/viba_acp/test_agent.py) 覆盖以下情形：

- **工具调用与权限被拒绝**：`scripted_agent.py --tool-call` 每轮执行一次工具调用；客户端按默认策略回
  `reject_once`，工具调用记为 `failed`，本轮仍以 `end_turn` 结束。
- **超时**：`scripted_agent.py --mode hang` 不返回响应；客户端发送 `session/cancel`，收到
  `stopReason: "cancelled"`，响应为空，结束原因为 `cancelled`。
- **协议外的 stop reason**：`scripted_agent.py --stop paused`；客户端报 `AcpError`，指明该值不是 ACP
  定义的结束原因。
- **不同的 ACP 版本**：`scripted_agent.py --protocol 2`；客户端报 `AcpError`，指明 agent 报告版本 2，
  而客户端只支持 1。
- **agent 无法启动或已退出**：`initialize` 收到 EOF，客户端报 `AcpError`，并附退出码与 stderr 末行。

## 6. 接入真实 agent

三个 agent 都已接好，命令与环境在 [`agents.py`](agents.py)，用 `--agent` 选。代码里没有本机路径：
命令从 `PATH` 或环境变量解析，凭据取自启动 `run.py` 的那个环境。

| `--agent` | 命令 | 凭据 |
|---|---|---|
| `scripted` | `python3 viba_acp/scripted_agent.py` | 无 |
| `claude` | `npx -y @agentclientprotocol/claude-agent-acp` | 环境里的 `ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`；`CLAUDE_CODE_EXECUTABLE` 或 `PATH` 里的 Claude CLI |
| `dsh` | `dsh acp`（`DSH_EXECUTABLE` 或 `PATH` 里的 `dsh`） | `~/.dsh` 里已有的 DeepSeek 凭据 |

`claude` 的适配器是 Claude Agent SDK 的 ACP 实现（[仓库](https://github.com/agentclientprotocol/claude-agent-acp)）；
它会去找 SDK 附带的 Claude CLI，`agents.py` 用 `PATH` 里的 `claude` 填 `CLAUDE_CODE_EXECUTABLE`。`dsh`
用 `dsh acp`，CLI 不在 `PATH` 里时用 `DSH_EXECUTABLE` 指过去。

同一份 viba 程序跑三个 agent：

```bash
python3 demo/viba_acp/test_agents.py
```

它依次跑 `scripted`、`claude`、`dsh`，检查调度达到 `ok`、`initialize` 自报的 agent 名、以及 agent 确实回
了非空文本，并打印各答案的开头。缺命令的 agent 会跳过。

也可以先用客户端单独验证一个命令：客户端发送一次请求并输出 JSON。

```bash
python3 viba_acp/acp_client.py --agent "python3 viba_acp/scripted_agent.py" "hello"
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
| [`agent_service.py`](agent_service.py) | 服务那一侧：会话与操作，启动 `VIBA_AGENT` 选中的 agent |
| [`agents.py`](agents.py) | 三个 agent 的命令、环境与可用性判断 |
| [`acp_client.py`](acp_client.py) | ACP 客户端：stdio 分帧、会话、prompt、权限、取消 |
| [`scripted_agent.py`](scripted_agent.py) | 脚本化的 ACP agent：不含模型，响应可以预期 |
| [`demo/viba_acp.viba.md`](../demo/viba_acp.viba.md) | 示例程序 `explain_then_shorten.viba` 的文档 |
| [`demo/viba_acp/run.py`](../demo/viba_acp/run.py) | 运行入口：拼模块、调度、服务、store、trace |
| [`demo/viba_acp/test_agent.py`](../demo/viba_acp/test_agent.py) | 脚本化 agent 的测试套件 |
| [`demo/viba_acp/test_agents.py`](../demo/viba_acp/test_agents.py) | 同一份程序跑三个 agent 的集成检查 |

运行方式：

```bash
python3 demo/viba_acp/run.py
python3 demo/viba_acp/test_agent.py
```

测试套件运行 `run.py`（与第 3.1 节相同），读取其调度报告与 trace，检查：服务进程只 `initialize` 一次、
只开一个会话；一轮 `session/prompt` 在该会话上；同一 store 上再次运行时不向 agent 发送请求。此外，它
覆盖工具调用与权限、超时取消、协议外的 stop reason、不同的 ACP 版本与 agent 无法启动。
