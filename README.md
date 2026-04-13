# Agora — Multi-Agent Discourse Engine

A backend-agnostic multi-agent debate orchestrator using the
[Agora Protocol](AGORA_PROTOCOL.md) for structured conversation.

## What It Does

Point it at a YAML config — or use the web dashboard to build one.
It spins up N agents across any combination of local and remote models,
gives them a topic, and lets them debate until they converge or run out
of turns.

Agents decide when to stay, when to yield, and whom to invite next —
just like humans in a real conversation. Everything streams live to a
web UI accessible from any device on the network.

## Quick Start

```bash
pip install pyyaml

# Launch the dashboard (no config needed)
python -m agora.web

# Or auto-start a debate
python -m agora.web configs/hybrid.yaml

# Terminal mode (no web UI)
pip install rich
python debate.py configs/mlx_offline.yaml
```

The dashboard opens at `http://localhost:8420` and is accessible from
any device on your LAN.

## Web Dashboard

The dashboard at `/` lets you:
- **Browse configs** — see all YAML configs, click to edit
- **Create new configs** — template with all CLI/backend options
- **Edit configs** — YAML text editor + form UI with agent builder
- **Copy / Paste / Delete** configs
- **Start & Stop** debates from the browser
- **View live debate** at `/debate` with token-by-token streaming
- **Interaction map** — canvas chart showing who addressed whom
- **Past debates** at `/history` — browse transcripts, decisions, configs
- **Compare debates** — side-by-side diff of two past debates
- **Export to PDF** — print-friendly view from history

Everything is mobile-optimized with touch targets and safe area support.

## Project Structure

```
agora/
  __init__.py        — public API (from agora import run)
  protocol.py        — Agora Protocol: Message, parse_reply, A2A, structured output
  backends.py        — MLX, Ollama, CLI, API backends (all streaming)
  agent.py           — Agent class (speak with optional streaming callback)
  display.py         — Terminal UI (rich when available, plain fallback)
  orchestrator.py    — Turn-taking loop, anti-consensus, turn budgets
  web.py             — Dashboard + live debate UI (stdlib HTTP server)
  health.py          — Pre-flight CLI health checks
  save.py            — Auto-save transcripts to Markdown + JSON
debate.py            — CLI entry point (thin wrapper)
configs/             — debate configurations (YAML)
debates/             — auto-saved transcripts per debate (gitignored)
AGORA_PROTOCOL.md    — protocol specification
```

## Backends

| Backend  | Type   | Streaming | Setup                                  |
|----------|--------|-----------|----------------------------------------|
| `cli`    | Remote | Yes       | Installed CLI (claude/codex/gemini)    |
| `api`    | Any    | Yes       | Any OpenAI-compatible endpoint         |
| `mlx`    | Local  | Yes       | `pip install mlx-lm` (Apple Silicon)   |
| `ollama` | Local  | Yes       | `ollama serve` + `ollama pull <model>` |

All backends implement the same interface. Mixing is trivial — see
`configs/hybrid.yaml` for a 3-backend council.

### CLI Commands

| CLI     | Command                                                    |
|---------|------------------------------------------------------------|
| Claude  | `["claude", "-p"]`                                         |
| Claude  | `["claude", "-p", "--allowedTools", "WebSearch,WebFetch"]` |
| Gemini  | `["gemini", "--prompt"]`                                   |
| Codex   | `["codex", "exec"]`                                        |

## Configuration

Minimum YAML:

```yaml
topic: "Your question here"
max_turns: 16

agents:
  - name: advocate
    backend: cli
    command: ["claude", "-p"]
    timeout: 120
    system: |
      Your role instructions here.
```

### Advanced options

```yaml
memory_mode: auto          # auto | co-resident | hot-swap
anti_consensus: true       # inject devil's advocate on early convergence

orchestration:
  turn_order: [advocate, critic, judge]
  phase_plan:
    - phase: 1
      turns: 1-6
      objective: Establish positions
    - phase: 2
      turns: 7-12
      objective: Challenge and compare
  final_output_contract: |
    1. Recommended path
    2. What not to do now
    3. 90-day action plan

agents:
  - name: critic
    turn_budget: 8          # max turns for this agent
```

See `configs/*.yaml` for full examples.

## The Agora Protocol

Every agent reply ends with three directives:

```
@intent:    propose | critique | defend | synthesize | question | concede | yield
@addressed: <agent name or "all">
@next:      continue | yield | invite:<agent name>
```

These let agents control their own turn-taking, creating real debate
dynamics instead of mechanical round-robin. Full spec in
[AGORA_PROTOCOL.md](AGORA_PROTOCOL.md).

### Structured Output (optional)

Agents can also emit structured claims:

```
@claim: RAG outperforms fine-tuning at small data scales | evidence: PMC-LLaMA study | confidence: 0.8
@counter: Generic models understand manufacturing language
```

## Debate Dynamics

- **Anti-consensus** — when 3+ agents concede/synthesize in the first
  half of the debate, a moderator injects a devil's advocate prompt
- **Turn budgets** — each agent gets a configurable max turn count;
  over-budget agents only speak when explicitly invited
- **Phase transitions** — moderator announces phase objectives from
  `phase_plan` at the configured turn boundaries
- **First-turn yield protection** — agents cannot yield on turn 1

## Transcript Auto-Save

Every completed (or stopped) debate is automatically saved to `debates/`:

```
debates/2026-04-13_0923_should-we-fine-tune-or/
  transcript.md    — full conversation log
  decision.md      — moderator's decision summary
  config.yaml      — snapshot of the config used
  meta.json        — machine-readable metadata
```

Manual snapshot of a running debate:
```bash
python -m agora.save
```

## Programmatic Usage

```python
from agora import run
run("configs/mlx_offline.yaml")

# Or build your own setup:
from agora import Agent, MLXBackend
backend = MLXBackend(model="mlx-community/Qwen3.5-9B-MLX-4bit")
agent = Agent(name="proposer", system="You argue boldly.", backend=backend)

# A2A serialization:
from agora.protocol import to_a2a, from_a2a
envelope = to_a2a(messages, topic, agents)
topic, agents, messages = from_a2a(envelope)
```

## Memory Modes

- `co-resident` — all local models loaded at once (fast, needs RAM)
- `hot-swap` — only active local model loaded (slow, fits tight RAM)
- `auto` — picks based on estimated footprint (default)

## Roadmap

- [x] Streaming output (per-token display)
- [x] Rich terminal UI
- [x] Modular package structure
- [x] Web dashboard with config editor + YAML UI
- [x] Live debate streaming via SSE
- [x] Interaction chart (who addressed whom)
- [x] Transcript auto-save (Markdown + JSON)
- [x] Decision summary (moderator synthesis)
- [x] Debate history viewer + comparison
- [x] Mobile-optimized UI
- [x] Multi-CLI backend (Claude / Gemini / Codex)
- [x] APIBackend (OpenAI-compatible HTTP)
- [x] Real token streaming for CLI backends
- [x] A2A protocol serialization
- [x] Structured output mode (@claim/@counter)
- [x] Anti-consensus mechanism
- [x] Per-agent turn budgets
- [x] Orchestration config (turn_order, phase_plan, final_output_contract)
- [x] Health checks with graceful degradation
- [x] LAN access (0.0.0.0 binding)
- [x] Export to PDF
