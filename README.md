# viba_acp

The viba-side interface for calling ACP agents, the host implementation behind it, and the demo that drives it.

- [`viba_acp.viba.md`](viba_acp.viba.md) — the interface, written as one document: `public.viba`, `types.viba` and `apis.viba`; `viba <name>` blocks name the modules.
- [`viba_acp/`](viba_acp/) — the Python side: `acp_client.py` (ACP over stdio), `agent_service.py` (the operations), `agents.py` (the three agents); [`viba_acp/README.md`](viba_acp/README.md) is the manual.
- [`demo/viba_acp.viba.md`](demo/viba_acp.viba.md) — the example program.
- [`demo/viba_acp/`](demo/viba_acp/) — `run.py`, `test_agents.py`.

## Dependency

The viba interpreter and the distributed scheduler are the neighbouring `demo-viba` workspace. `VIBA_ACP_HOST` points at it (default `../demo-viba`); the layer imports `viba` and `distributed.service` from there.

## Run

```bash
python3 demo/viba_acp/run.py              # Claude Agent SDK's ACP adapter
python3 demo/viba_acp/run.py --agent dsh  # DeepSeek Harness's acp profile
python3 demo/viba_acp/run.py --agent trae # TraeCode CLI's ACP server
python3 demo/viba_acp/test_agents.py      # the program against all three, with their suites
```

The store is per agent (`demo/viba_acp/.store/<agent>`). `claude` takes its endpoint and token from the environment it is started in and its CLI from `CLAUDE_CODE_EXECUTABLE` (or `PATH`); `dsh` is the `dsh acp` profile, found on `PATH` or named by `DSH_EXECUTABLE`, and uses `~/.dsh`; `trae` is `trae-cli acp serve`, found on `PATH` or named by `TRAE_EXECUTABLE`, and uses the login in `~/.trae`.
