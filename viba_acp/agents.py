"""The ACP agents this service can drive: their commands and the env they need.

`VIBA_AGENT` picks one (or `the_command(name)` does):

- `scripted` — the in-repo scripted agent, no model;
- `claude` — the Claude Agent SDK's ACP adapter, `npx -y
  @agentclientprotocol/claude-agent-acp`. It spawns the Claude CLI itself, so
  `CLAUDE_CODE_EXECUTABLE` points at the working one; the endpoint and token
  come from `~/.llm-config/flash-env.sh`;
- `dsh` — DeepSeek Harness's own `acp` profile, `dsh acp`; it uses the
  credentials `~/.dsh` already has.

`the_command()` answers `(command, extra environment)`: the environment is
layered over this process's own when the agent is spawned, never printed.
"""

import os
import shlex
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent            # viba_acp
DSH = Path("/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh")
FLASH_ENV = Path.home() / ".llm-config" / "flash-env.sh"
CLAUDE_ADAPTER = ["npx", "-y", "@agentclientprotocol/claude-agent-acp"]


def names():
    return ("scripted", "claude", "dsh")


def the_command(name=None):
    """(command, extra environment) for one agent; `VIBA_AGENT` when no name."""
    name = name or os.environ.get("VIBA_AGENT", "scripted")
    if name == "scripted":
        return [sys.executable, str(HERE / "scripted_agent.py")], {}
    if name == "claude":
        env = the_flash_env()
        executable = os.environ.get("CLAUDE_CODE_EXECUTABLE") or shutil.which("claude")
        if executable:
            env.setdefault("CLAUDE_CODE_EXECUTABLE", executable)
        return list(CLAUDE_ADAPTER), env
    if name == "dsh":
        executable = os.environ.get("DSH_EXECUTABLE") or str(DSH)
        return [executable, "acp"], {}
    raise ValueError(f"unknown agent {name!r}; choose one of {', '.join(names())}")


def the_flash_env():
    """`ANTHROPIC_*` from `~/.llm-config/flash-env.sh`, when that file is there."""
    env = {}
    if not FLASH_ENV.is_file():
        return env
    for line in FLASH_ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("export "):
            continue
        try:
            token = shlex.split(line[len("export "):])[0]
        except (ValueError, IndexError):
            continue
        key, _, value = token.partition("=")
        if key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL"):
            env.setdefault(key, value)
    return env


def available(name):
    """Whether this agent can start on this machine, as far as a path can tell."""
    if name == "scripted":
        return True
    if name == "claude":
        return shutil.which("npx") is not None and (
            os.environ.get("CLAUDE_CODE_EXECUTABLE") or shutil.which("claude"))
    if name == "dsh":
        return Path(os.environ.get("DSH_EXECUTABLE") or DSH).exists()
    return False
