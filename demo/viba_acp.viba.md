# demo/viba_acp

这个示例用 `viba_acp` 的客户端接口调用一个 agent：开两个会话、各提一个问题、拿回两个响应。两个会话
带不同的 wiki 路径，问题也让 agent 说出它这个会话拿到的 wiki 路径；服务把各自的上下文交给 agent（能收
system prompt 的收字段，不收的补一轮提示）。两个响应用积一起返回，两轮都会执行。

## two_wikis.viba

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

answer_b =
     viba_acp.send_prompt
  << $env ($sub_env << args.env << "c3")
  << $session session_b
  << $prompt (ListLiteral[question])
  << $on_update on_notification

__impl__ = answer_a * answer_b
```
