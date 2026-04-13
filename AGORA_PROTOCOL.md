# Agora Protocol v0.2

A minimal conversation standard for multi-model discourse.

## The Problem

When multiple AI models talk to each other, raw text exchange is not
enough. Real conversations need signals beyond content:

- **Who is speaking to whom?**
- **What is the speaker trying to do?** (propose, critique, defend, ask)
- **Who should speak next?**

Without these signals, multi-model dialogue collapses into round-robin
monologues. Agora Protocol adds the minimum structure needed to
coordinate turn-taking, addressing, and intent — without breaking the
natural flow of the reply.

## The Contract

Every agent reply consists of two parts:

1. **Content** — normal prose the user reads
2. **Directives** — key-value lines at the end, machine-readable

```
[The agent's actual response, one or more paragraphs.]

@intent: defend
@addressed: phi-critic
@next: continue
```

Parsers strip the `@`-lines from the displayed output. The content
stays clean for humans; orchestrators read the directives.

## Core Directives

### `@intent`

What the speaker is doing. One of:

| Intent       | Meaning                                         |
|--------------|-------------------------------------------------|
| `propose`    | Putting forward a new position or idea          |
| `critique`   | Attacking a weakness in prior argument          |
| `defend`     | Responding to critique, refining position       |
| `synthesize` | Combining prior views into a new framing        |
| `question`   | Seeking clarification or raising unknown        |
| `concede`    | Accepting a point previously contested          |
| `yield`      | Stepping out of the conversation                |

### `@addressed`

Who the speaker is talking to. Either a specific agent name
(`phi-critic`) or `all` (broadcast). Default: `all`.

### `@next`

Turn-taking control:

| Value             | Effect                                          |
|-------------------|-------------------------------------------------|
| `continue`        | Speaker stays active, next in round-robin       |
| `yield`           | Speaker exits the conversation permanently      |
| `invite:<name>`   | Speaker passes the turn to named agent next     |

`invite:` overrides round-robin. Use it to create threading — "I was
responding to qwen, not phi, so qwen should speak next."

## Structured Output (optional)

Agents may include structured claims for machine-readable analysis:

```
@claim: RAG outperforms fine-tuning at <5K examples | evidence: PMC-LLaMA study (2024) | confidence: 0.8
@claim: Multilingual tokenizers lose 40% on domain code-switching | evidence: measured on internal corpus | confidence: 0.6
@counter: Generic models understand manufacturing language
```

Format: `@claim: <text> | evidence: <source> | confidence: <0.0-1.0>`

Claims are parsed into structured data for post-debate analysis.
`@counter` references a previous claim being disputed.

## Grounding Rule

Built into the protocol instruction:

> Only state facts you can verify. If you have web search, use it
> and cite URLs. If you lack information, say so explicitly rather
> than guessing. Never fabricate citations, version numbers, or
> benchmarks.

This is enforced by convention, not by parsing — agents that
hallucinate will be challenged by other agents in the debate.

## Termination

A debate ends when any of these happens:
- All agents have yielded
- Only one agent remains (they receive a closing statement turn)
- `max_turns` is reached
- A moderator (external) stops it

When an agent yields, remaining agents are notified via a moderator
message and may receive a closing statement turn.

## Orchestration Features

### Anti-Consensus

When 3+ agents concede or synthesize in the first half of a debate,
the orchestrator injects a devil's advocate prompt to prevent
premature convergence.

### Turn Budgets

Each agent may have a `turn_budget` limiting their maximum turns.
Over-budget agents only speak when explicitly invited.

### Phase Plans

Debates can be structured into phases with named objectives:

```yaml
phase_plan:
  - phase: 1
    turns: 1-6
    objective: Establish positions
  - phase: 2
    turns: 7-12
    objective: Challenge and compare
```

The moderator announces phase transitions at configured boundaries.

### Final Output Contract

A `final_output_contract` in the config specifies what the decision
summary must contain:

```yaml
final_output_contract: |
  1. Recommended path
  2. What not to do now
  3. 90-day action plan
```

## Why This Is Enough

Human conversations rely on far more signals (tone, eye contact,
pauses). But for written multi-agent discourse, the core directives
cover >90% of coordination needs:

- `intent` preserves the *rhetorical move* the speaker is making
- `addressed` preserves the *conversational thread*
- `next` preserves the *turn-taking right*

Structured output adds the remaining 10% for debates that need
machine-readable analysis.

## Relationship to Other Standards

- **A2A (Google, 2025):** Lower-level transport protocol. Agora
  includes `to_a2a()` / `from_a2a()` serialization — the directive
  block maps cleanly to A2A JSON-RPC metadata.
- **MCP (Anthropic, 2024):** Agent-to-tool protocol. Agora is
  agent-to-agent. They compose: an Agora-speaking agent can still
  use MCP tools internally.
- **OpenAI Chat format:** Agora directives fit inside `content`.
  Existing OpenAI-compatible stacks (Ollama, vLLM, LM Studio) work
  without modification.

## Versioning

Agora v0.2 adds structured output, grounding rules, anti-consensus,
turn budgets, phase plans, and A2A serialization over v0.1.

Future versions may add:
- `@references: turn_2, turn_5` — explicit citation of prior turns
- `@affect: urgent | neutral | playful` — emotional register
- `@evidence_quality: primary | secondary | anecdotal` — source grading

Agents that don't recognize new fields should ignore them. The
protocol is additive-only.
