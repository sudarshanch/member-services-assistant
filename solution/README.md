# Member-services assistant

Governed assistant for member-services representatives. A conversation is one tenant and one member. Tool calls go through a gateway that enforces tenant isolation, minimum necessary PHI, confirmation before a grievance write, and a per-conversation budget. Plan documents are retrieved only when the file's directory and its front matter name the same tenant.

The default brain is a deterministic policy (`deterministic-policy-v1`). It chooses tools and answers from tool results. Set `MODEL_PROVIDER=openai` to use any OpenAI-compatible tool-calling API instead, including Gemini. The gateway is the same either way: the model may only request a tool, and the gateway decides whether that call is allowed. The evaluation gate was run with the deterministic policy and no remote model, which is what keeps a conversation inside the $0.03 cost cap and makes the run repeatable.

The member, claims, grievance, and directory APIs stay the system of record when a real model is on. For this assignment those APIs are the supplied mock. The model does not replace them. Copy `solution/.env.example` to `solution/.env`, set `OPENAI_API_KEY`, and leave the mock running at `MOCK_BASE_URL`. Do not commit `.env`.

## Setup

Python 3.10 or newer. The standard library is enough. Run everything below from `solution/`.

The model is selected by `solution/.env`, which the process loads on startup. Copy the example and set the key. That file is gitignored.

```bash
cd solution
cp .env.example .env
```

`.env.example` sets `MODEL_PROVIDER=openai`, `OPENAI_BASE_URL` to Gemini's OpenAI-compatible endpoint, and `MODEL_NAME=gemini-3.8-flash`. Put the key in `OPENAI_API_KEY`. For OpenAI itself, change the base URL to `https://api.openai.com/v1` and set `MODEL_NAME` to the model you use. No extra `export` is required for those values.

The member APIs are still a separate process. For this assignment they are the pack mock, and `PACK_DIR` is the parent of `solution/` (the directory that contains `kb/`, `mock_services/`, `eval/conversations.json`, and `tools/`). Start it in one terminal and leave it running:

```bash
cd solution
export PACK_DIR="$(pwd)/.."
export PYTHONPATH=src
python "$PACK_DIR/mock_services/server.py" --port 8090
```

`MOCK_BASE_URL` defaults to `http://127.0.0.1:8090`, which matches that process. Do not commit the pack. The repository gitignore keeps it out.

Optional environment:

| Variable | Default | Purpose |
|---|---|---|
| `MOCK_BASE_URL` | `http://127.0.0.1:8090` | Member, claims, grievance, and directory base URL |
| `MODEL_PROVIDER` | `deterministic` when `.env` is absent. `.env.example` sets `openai` | `deterministic` or `openai` |
| `MODEL_NAME` | `deterministic-policy-v1` in code. `.env.example` sets `gemini-3.8-flash` | Model sent to the OpenAI-compatible API |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` in code. `.env.example` sets the Gemini endpoint | OpenAI-compatible base URL |
| `OPENAI_API_KEY` | empty | Required when `MODEL_PROVIDER=openai`. Only in `solution/.env` |
| `PLAN_A_TOKEN`, `PLAN_B_TOKEN`, `DIRECTORY_API_KEY` | values in the pack | Override credentials so they are not read from the pack file |
| `MAX_STEPS`, `MAX_COST_USD`, `MAX_TURN_LATENCY_S` | `12`, `0.03`, `45` | Conversation budgets |

## Run a conversation

In a second terminal, from `solution/`, with the mock still running on 8090:

```bash
export PACK_DIR="$(pwd)/.."
export PYTHONPATH=src
python -m member_services.cli --tenant plan_a --member M749817 \
  --message "The member is asking what they pay for an in-network specialist visit."
```

The CLI uses the provider in `solution/.env`. With the example file filled in, this call goes to Gemini and the gateway still reads the member from the mock. Omit `--message` for a multi-turn prompt.

The evaluation gate is repeatable only on the deterministic policy. A filled-in `.env` would send the gate to the remote model. Run the gate with:

```bash
export MEMBER_SERVICES_SKIP_DOTENV=1
export MODEL_PROVIDER=deterministic
export MODEL_NAME=deterministic-policy-v1
```

HTTP, after the mock is up:

```bash
python -m member_services.server --port 8088
```

`POST /v1/conversations` with `{"tenant","member_id"}`, then `POST /v1/conversations/{id}/messages` with `{"text"}`. The response includes the reply and the redacted trace.

## Tests and the gate

```bash
python -m unittest discover -s tests
python -m member_services.harness
```

The harness writes `eval/last_report.md` and `eval/last_report.json` in this directory and exits non-zero when the gate fails. The gate requires governed-fact recall and behaviour accuracy of at least 0.95, a perfect score on safety behaviours, no unauthorized grievance, and no cross-tenant or SSN leak.

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
