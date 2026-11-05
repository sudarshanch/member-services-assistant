# Member-services assistant

Governed assistant for member-services representatives. A conversation is one tenant and one member. Tool calls go through a gateway that enforces tenant isolation, minimum necessary PHI, confirmation before a grievance write, and a per-conversation budget. Plan documents are retrieved only when the file's directory and its front matter name the same tenant.

The default brain is a deterministic policy (`deterministic-policy-v1`). It chooses tools and answers from tool results. Set `MODEL_PROVIDER=openai` to use any OpenAI-compatible tool-calling API instead. The gateway is the same either way. The evaluation gate was run with the deterministic policy and no remote model, which is what keeps a conversation inside the $0.03 cost cap and makes the run repeatable.

## Setup

Python 3.10 or newer. The standard library is enough. From this repository:

```bash
export PACK_DIR=/path/to/the/task/pack
export PYTHONPATH=src
python "$PACK_DIR/mock_services/server.py" --port 8090
```

`PACK_DIR` must contain `kb/`, `mock_services/`, `eval/conversations.json`, and `tools/`. If you run from the pack itself, the pack is discovered and `PACK_DIR` can be omitted. Do not commit the pack. This repo gitignores it.

Optional environment:

| Variable | Default | Purpose |
|---|---|---|
| `MOCK_BASE_URL` | `http://127.0.0.1:8090` | Member, claims, grievance, and directory base URL |
| `MODEL_PROVIDER` | `deterministic` | `deterministic` or `openai` |
| `MODEL_NAME` | `deterministic-policy-v1` | Sent to the OpenAI-compatible API when that provider is on |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Also works for Ollama's OpenAI-compatible endpoint |
| `OPENAI_API_KEY` | empty | Required only for `MODEL_PROVIDER=openai` |
| `PLAN_A_TOKEN`, `PLAN_B_TOKEN`, `DIRECTORY_API_KEY` | values in the pack | Override credentials so they are not read from the pack file |
| `MAX_STEPS`, `MAX_COST_USD`, `MAX_TURN_LATENCY_S` | `12`, `0.03`, `45` | Conversation budgets |

## Run a conversation

```bash
python -m member_services.cli --tenant plan_a --member M749817 \
  --message "The member is asking what they pay for an in-network specialist visit."
```

Omit `--message` for a multi-turn prompt. HTTP, after the mock is up:

```bash
python -m member_services.server --port 8088
```

`POST /v1/conversations` with `{"tenant","member_id"}`, then `POST /v1/conversations/{id}/messages` with `{"text"}`. The response includes the reply and the redacted trace.

## Tests and the gate

```bash
python -m unittest discover -s tests
python -m member_services.harness
```

The harness writes `eval/last_report.md` and `eval/last_report.json` and exits non-zero when the gate fails. The gate requires governed-fact recall and behaviour accuracy of at least 0.95, a perfect score on safety behaviours, no unauthorized grievance, and no cross-tenant or SSN leak.

Last run, deterministic policy, mock clock 2026-11-15: **pass**. Behaviour 40/40, governed facts 40/40, published facts 36/40, safety 1.0, unauthorized writes 0, isolation or PHI leaks 0. The four published misses are explained in the report and in `design/DESIGN.md` (ADR-3). Matching them would quote another tenant, another plan year, another product, or the wrong claim status.

## Layout

| Path | Role |
|---|---|
| `src/member_services/gateway.py` | Tenant, PHI, confirmation, idempotency, budgets |
| `src/member_services/agent.py` | Policy, and the optional tool-calling loop |
| `src/member_services/kb.py` | Admissible plan documents and benefit tables |
| `src/member_services/api_client.py` | Member APIs and directory. Refuses a cross-tenant path |
| `src/member_services/harness.py` | Eval runner and regression gate |
| `design/DESIGN.md` | AWS deployment, Azure mapping, threats, ADRs |
| `NOTES.md` | Assumptions, surprises, AI use |
