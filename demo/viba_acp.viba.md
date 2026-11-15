# demo/viba_acp

这个示例用 `viba_acp` 的客户端接口调用一个 agent：开一个会话，提一个问题，拿回响应。

## explain_then_shorten.viba

```viba
import viba_acp.public as viba_acp

__decl__ = viba_acp.PromptResponse <- $env Env

args = __get_args__ << __decl__

session = viba_acp.open_session << $env ($sub_env << args.env << "c0")

question =
    $TextContent Object
  * $text "Explain in one sentence what a type class is."

on_notification =
     void
  <- $env Env
  <- $notification viba_acp.SessionNotification
  <- { record one session/update notification }

__impl__ =
     viba_acp.send_prompt << $env ($sub_env << args.env << "c1")
  << $session session
  << $prompt (ListLiteral[question])
  << $on_update on_notification
```
