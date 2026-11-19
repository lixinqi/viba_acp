"""The viba_acp program against every ACP agent the layer can drive.

    python3 demo/viba_acp/test_agents.py

It runs `run.py --agent <name>` for `claude`, `dsh` and `trae`. The program opens two
sessions with different wikis and asks each session for the wiki path it was given;
each session's context reaches the agent either as the `session/new` `systemPrompt`
field or, when the agent advertises no such capability, as that session's first
prompt. For each agent the checks are:

- the schedule reaches `ok`, the service initializes once, and two sessions open,
  each with its own wiki;
- one question per session, answered (the answer names that session's wiki);
- the two sessions are recorded under their calls' paths (`root/c0`, `root/c1`)
  with the agent's backend variant and its wiki;
- the two prompt turns are recorded under their calls' paths (`root/c2`,
  `root/c3`) with `end_turn`, and the program returns both responses;
- a second run on the same store sends no prompt: everything is replayed.

An agent is skipped when its command is not on this machine: `claude` takes
`ANTHROPIC_*` from this process's environment, `dsh` its credentials from `~/.dsh`,
`trae` the login in `~/.trae`.
"""

import json
import os
import re
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
from distributed import service
from viba.interpret import get_snapshot
from viba.reflect import VibaReflectError

checks = Checks("viba_acp/agents")

WIKI_A = "../demo-viba/viba/viba/wiki"
WIKI_B = "../demo-viba/viba/viba/core"
BACKENDS = {"claude": "$ClaudeAgentBackend", "dsh": "$DshAgentBackend",
            "trae": "$TraeAgentBackend"}


def run(agent: str, scratch: Path, fresh: bool = True):
    """One `run.py` on the agent's store; answers its result and trace path."""
    store = scratch / agent
    trace = scratch / (f"{agent}.jsonl" if fresh else f"{agent}-replay.jsonl")
    command = [sys.executable, str(HERE / "run.py"), "--agent", agent,
               "--store", str(store), "--trace", str(trace)]
    if fresh:
        command.append("--fresh")
    return subprocess.run(command, capture_output=True, text=True, timeout=300), trace


def lines_of(result) -> list:
    """The scheduler's JSON lines from one run."""
    return [json.loads(one) for one in result.stdout.splitlines()
            if one.strip().startswith("{")]


def messages(trace: Path) -> list:
    """Every ACP message in the trace, as (direction, message)."""
    if not trace.exists():
        return []
    lines = [line for line in trace.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    return [(one["dir"], one["message"]) for one in map(json.loads, lines)]


def sent(trace: Path, method: str) -> list:
    """Every request of one method the client sent, in order."""
    return [message for direction, message in messages(trace)
            if direction == "client->agent" and message.get("method") == method]


def answered(trace: Path, request) -> list:
    """Every response the agent sent for one request."""
    return [message for direction, message in messages(trace)
            if direction == "agent->client" and message.get("id") == request.get("id")
            and "result" in message]


def system_prompt_text(message) -> str:
    """The text of one request's ACP `systemPrompt`, or '' when it has none."""
    prompt = (message.get("params") or {}).get("systemPrompt") or {}
    return "".join(part.get("text") or "" for part in prompt.get("content") or [])


def prompt_text(message) -> str:
    """The text blocks of one `session/prompt` request, joined."""
    params = message.get("params") or {}
    return "".join(block.get("text") or "" for block in params.get("prompt") or [])


def snapshot(store: Path, path: str):
    """The value recorded under one call's path."""
    return get_snapshot(service.environ_at(path, store))


def has_tag(value, tag) -> bool:
    try:
        value.by_tag(tag)
        return True
    except VibaReflectError:
        return False


def the_answers(trace: Path, asked: list) -> list:
    """(session, text) for each question, in the order it was asked.

    The text is the `agent_message_chunk` notifications between one question and the
    next request.
    """
    pairs = messages(trace)
    asked_ids = {request.get("id") for request in asked}
    answers = []
    for index, (direction, message) in enumerate(pairs):
        if direction != "client->agent" or message.get("id") not in asked_ids:
            continue
        texts = []
        for later_direction, later in pairs[index + 1:]:
            if later.get("method") == "session/prompt":
                break
            if later_direction != "agent->client":
                continue
            update = (later.get("params") or {}).get("update") or {}
            if update.get("sessionUpdate") == "agent_message_chunk":
                texts.append((update.get("content") or {}).get("text") or "")
        session = (message.get("params") or {}).get("sessionId")
        answers.append((session, "".join(texts)))
    return answers


def check_one(agent: str, scratch: Path) -> str:
    """Run the program once on the agent and check its report, trace and store."""
    backend = BACKENDS[agent]
    store = scratch / agent
    result, trace = run(agent, scratch, fresh=True)
    check = checks.check
    lines = lines_of(result)
    check(result.returncode == 0 and lines
          and lines[-1].get("outcome") == "ok",
          f"{agent}: the schedule reaches ok: "
          f"{lines[-1] if lines else result.stderr[-400:]!r}")
    check(len(sent(trace, "initialize")) == 1,
          f"{agent}: the service initializes once: "
          f"{len(sent(trace, 'initialize'))}")

    created = sent(trace, "session/new")
    check(len(created) == 2, f"{agent}: opens two sessions: {len(created)}")
    opened = []
    for request in created:
        response = answered(trace, request)
        session = ((response[-1].get("result") or {}).get("sessionId")
                   if response else None)
        opened.append(session)
    check(len(opened) == 2 and all(opened),
          f"{agent}: the sessions answer ids: {opened!r}")

    params = (created[0].get("params") or {}) if created else {}
    carried = isinstance(params.get("systemPrompt"), dict)
    contexts = ([system_prompt_text(request) for request in created]
                if carried else
                [prompt_text(request) for request in sent(trace, "session/prompt")
                 if prompt_text(request).startswith("Context:")])
    check(len(contexts) == 2 and contexts[0] != contexts[1],
          f"{agent}: the two sessions carry different wikis: {contexts!r}")

    prompted = sent(trace, "session/prompt")
    asked = [request for request in prompted
             if not prompt_text(request).startswith("Context:")]
    check(len(prompted) == (2 if carried else 4),
          f"{agent}: the context takes "
          f"{'the systemPrompt field' if carried else 'one prompt turn per session'}: "
          f"{len(prompted)} prompts")
    check([request["params"].get("sessionId") for request in asked] == opened,
          f"{agent}: one question per session, in order: "
          f"{[request['params'].get('sessionId') for request in asked]!r}")

    for path, session_id, wiki in (("root/c0", opened[0], WIKI_A),
                                   ("root/c1", opened[1], WIKI_B)):
        value = snapshot(store, path)
        check(value is not None, f"{agent}: the session is recorded at {path}")
        if value is None:
            continue
        check(value.by_tag("$session_id").value == session_id,
              f"{agent}: the session at {path} names {session_id}: "
              f"{value.by_tag('$session_id').value!r}")
        session_backend = value.by_tag("$backend")
        check(has_tag(session_backend, backend),
              f"{agent}: the session at {path} has {backend}: {session_backend!r}")
        check(session_backend.by_tag("$context").by_tag("$wiki_path").value == wiki,
              f"{agent}: the session at {path} carries its own wiki")

    for path in ("root/c2", "root/c3"):
        value = snapshot(store, path)
        check(value is not None, f"{agent}: the response is recorded at {path}")
        if value is not None:
            check(value.by_tag("$stop_reason").value == "end_turn",
                  f"{agent}: the response at {path} ends with end_turn")

    for path, prefix in (("root/c2", "update"), ("root/c3", "note")):
        names = sorted(child.parent.name
                       for child in (store / path).glob("*/value.viba"))
        check(bool(names) and all(re.fullmatch(rf"{prefix}\d+", name)
                                  for name in names),
              f"{agent}: the updates at {path} use the {prefix!r} prefix: {names!r}")

    outcome = lines[-1] if lines else {}
    check(str(outcome.get("value", "")).count("$stop_reason") == 2,
          f"{agent}: the program returns both responses: {outcome.get('value')!r}")

    answers = the_answers(trace, asked)
    check(len(answers) == 2 and all(text.strip() for _session, text in answers),
          f"{agent}: both questions are answered: {answers!r}")
    for (_session, text), wiki in zip(answers, (WIKI_A, WIKI_B)):
        check(wiki in text,
              f"{agent}: the answer names the session's wiki: {text[:200]!r}")
    return answers[-1][1] if answers else ""


def check_replay(agent: str, scratch: Path) -> None:
    """The same store again: every call is replayed, nothing is asked."""
    result, trace = run(agent, scratch, fresh=False)
    check = checks.check
    lines = lines_of(result)
    check(result.returncode == 0 and lines
          and lines[-1].get("outcome") == "ok",
          f"{agent} (replay): the schedule reaches ok: "
          f"{lines[-1] if lines else result.stderr[-400:]!r}")
    check(len(sent(trace, "session/prompt")) == 0,
          f"{agent} (replay): no prompt is sent: "
          f"{len(sent(trace, 'session/prompt'))}")
    rounds = [one for one in lines if "round" in one]
    computed = [note for note in (rounds[-1]["runs"]["acp"].get("api") or [])
                if note.get("computed")]
    check(not computed, f"{agent} (replay): nothing is computed: {computed!r}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="viba-acp-agents-") as scratch:
        scratch = Path(scratch)
        for agent in names():
            if not available(agent):
                print(f"{agent}: skipped (its command is not on this machine)")
                continue
            answer = check_one(agent, scratch)
            print(f"{agent}: {answer.strip()[:160]}")
            check_replay(agent, scratch)
    return checks.report()


if __name__ == "__main__":
    raise SystemExit(main())
