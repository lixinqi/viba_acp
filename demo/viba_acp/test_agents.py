"""The same viba program against every ACP agent the layer can drive.

    python3 demo/viba_acp/test_agents.py

It runs `run.py --agent <name>` for `scripted`, `claude` and `dsh`, checks that
the schedule reaches `ok` and that the agent streamed a non-empty answer, and
prints the first line of each answer. `claude` and `dsh` are skipped when their
binaries are missing; `claude` takes its endpoint and token from
`~/.llm-config/flash-env.sh` (through `agents.py`), `dsh` from `~/.dsh`.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
LAYER = ROOT / "viba_acp"
HOST = Path(os.environ.get("VIBA_ACP_HOST", str(ROOT.parent / "demo-viba"))).resolve()
sys.path.insert(0, str(HOST / "tests"))
sys.path.insert(0, str(HOST))
sys.path.insert(0, str(HOST / "viba"))
sys.path.insert(0, str(LAYER))
sys.path.insert(0, str(HERE))

from interpreter_support import Checks

from agents import available, names

checks = Checks("viba_acp/agents")


def run(agent: str, scratch: Path):
    store = scratch / agent
    trace = scratch / f"{agent}.jsonl"
    result = subprocess.run(
        [sys.executable, str(HERE / "run.py"), "--agent", agent,
         "--store", str(store), "--trace", str(trace)],
        capture_output=True, text=True, timeout=300)
    return result, trace


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="viba-acp-agents-") as scratch:
        scratch = Path(scratch)
        for agent in names():
            if not available(agent):
                print(f"{agent}: skipped (its command is not on this machine)")
                continue
            result, trace = run(agent, scratch)
            lines = [json.loads(one) for one in result.stdout.splitlines()
                     if one.strip().startswith("{")]
            check = checks.check
            check(result.returncode == 0 and lines
                  and lines[-1].get("outcome") == "ok",
                  f"{agent}: the schedule reaches ok: "
                  f"{lines[-1] if lines else result.stderr[-400:]!r}")
            messages = [json.loads(one)["message"] for one in
                        trace.read_text().splitlines() if one.strip()]
            names_seen = {message["result"]["agentInfo"]["name"]
                          for message in messages
                          if message.get("id") == 1 and "result" in message}
            check(len(names_seen) == 1,
                  f"{agent}: the agent names itself: {names_seen!r}")
            text = "".join(
                message["params"]["update"]["content"]["text"]
                for message in messages
                if message.get("method") == "session/update"
                and message["params"]["update"].get("sessionUpdate")
                == "agent_message_chunk")
            check(bool(text.strip()), f"{agent}: the agent answers with text")
            print(f"{agent} ({next(iter(names_seen), '?')}): {text.strip()[:160]}")
    return checks.report()


if __name__ == "__main__":
    raise SystemExit(main())
