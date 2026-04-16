# Agora Protocol v0.1

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
2. **Directives** — three key-value lines at the end, machine-readable

```
[The agent's actual response, one or more paragraphs.]

@intent: defend
@addressed: phi-critic
@next: continue
```

Parsers strip the `@`-lines from the displayed output. The content
stays clean for humans; orchestrators read the directives.

## The Three Directives

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

## Termination

A debate ends when any of these happens:
- All agents have yielded
- `max_turns` is reached
- A moderator (external) stops it

## Why This Is Enough

Human conversations rely on far more signals (tone, eye contact,
pauses). But for written multi-agent discourse, these three directives
cover >90% of coordination needs:

- `intent` preserves the *rhetorical move* the speaker is making
- `addressed` preserves the *conversational thread*
- `next` preserves the *turn-taking right*

More fields can be layered on (confidence, references, emotion) but
the three above are the minimum viable protocol.

## Relationship to Other Standards

- **A2A (Google, 2025):** Lower-level transport protocol. Agora can
  serialize onto A2A messages without modification — the directive
  block maps cleanly to A2A metadata.
- **MCP (Anthropic, 2024):** Agent-to-tool protocol. Agora is
  agent-to-agent. They compose: an Agora-speaking agent can still use
  MCP tools internally.
- **OpenAI Chat format:** Agora directives fit inside `content`.
  Existing OpenAI-compatible stacks (Ollama, vLLM, LM Studio) work
  without modification.

## Versioning

Agora v0.1 is the minimum. Future versions may add:
- `@references: turn_2, turn_5` — explicit citation of prior turns
- `@confidence: 0.8` — speaker's certainty level
- `@affect: urgent | neutral | playful` — emotional register

Agents that don't recognize new fields should ignore them. The
protocol is additive-only.
