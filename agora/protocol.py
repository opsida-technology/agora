"""
agora.protocol — The Agora Protocol: directives, message parsing, constants.

The Agora Protocol is a minimal conversation standard for multi-model
discourse. Every agent appends a directive block to its reply:

    @intent:    propose | critique | defend | synthesize |
                question | concede | yield
    @addressed: <agent name> | all
    @next:      continue | yield | invite:<agent name>
"""
import re
from dataclasses import dataclass, field
from typing import Optional


# ── Constants ───────────────────────────────────────────────────────────

AGORA_INSTRUCTION = """

After your reply, append a directive block. Each directive on its own line,
no other text after the block:

@intent: <propose|critique|defend|synthesize|question|concede|yield>
@addressed: <agent-name or "all">
@next: <continue|yield|invite:agent-name>

"intent" names what you are doing. "addressed" names who you are talking to
(or "all"). "next" controls turn-taking: "continue" keeps you in the debate,
"yield" removes you, "invite:X" passes the turn to agent X next.

GROUNDING RULE: Only state facts you can verify. If you have web search,
use it and cite URLs. If you lack information, say so explicitly rather
than guessing. Never fabricate citations, version numbers, or benchmarks."""

DIRECTIVE_RE = re.compile(r"^@(\w+)\s*:\s*(.+?)\s*$", re.MULTILINE)

INTENTS = frozenset({
    "propose", "critique", "defend", "synthesize",
    "question", "concede", "yield",
})


# ── Message ─────────────────────────────────────────────────────────────

@dataclass
class Message:
    speaker: str
    turn: int
    content: str
    intent: str = "propose"
    addressed: str = "all"
    next_action: str = "continue"   # continue | yield | invite:<name>

    @property
    def invited(self) -> Optional[str]:
        if self.next_action.startswith("invite:"):
            return self.next_action.split(":", 1)[1].strip()
        return None


# ── Parsing ─────────────────────────────────────────────────────────────

def parse_reply(raw: str, speaker: str, turn: int) -> Message:
    """Extract content and directives from a raw model reply."""
    directives = {m.group(1).lower(): m.group(2).strip()
                  for m in DIRECTIVE_RE.finditer(raw)}
    content = DIRECTIVE_RE.sub("", raw).strip()

    intent = directives.get("intent", "propose").lower()
    if intent not in INTENTS:
        intent = "propose"

    next_action = directives.get("next", "continue").lower()
    if not (next_action in {"continue", "yield"}
            or next_action.startswith("invite:")):
        next_action = "continue"

    return Message(
        speaker=speaker, turn=turn, content=content,
        intent=intent,
        addressed=directives.get("addressed", "all"),
        next_action=next_action,
    )


# ── Prompt helpers ──────────────────────────────────────────────────────

def build_chat_messages(system: str, topic: str, history: list,
                        self_name: str) -> list[dict]:
    """Build chat messages list for chat-completion backends (MLX / Ollama)."""
    msgs = [{"role": "system", "content": system + AGORA_INSTRUCTION},
            {"role": "user", "content": f"Debate topic: {topic}"}]
    for m in history:
        role = "assistant" if m.speaker == self_name else "user"
        body = m.content if role == "assistant" else f"[{m.speaker}]: {m.content}"
        msgs.append({"role": role, "content": body})
    msgs.append({"role": "user", "content": "Your turn."})
    return msgs


def build_cli_prompt(system: str, topic: str, history: list,
                     self_name: str) -> str:
    """Build flat text prompt for CLI backends."""
    parts = [f"SYSTEM INSTRUCTIONS:\n{system}{AGORA_INSTRUCTION}\n",
             f"DEBATE TOPIC: {topic}\n"]
    if history:
        parts.append("CONVERSATION SO FAR:")
        for m in history:
            parts.append(f"  [{m.speaker} — {m.intent}]: {m.content}")
    parts.append(f"\nYou are [{self_name}]. Your turn.")
    return "\n".join(parts)


# ── A2A Protocol Serialization ────────────────────────────────────────

def to_a2a(messages: list, topic: str, agents: list[dict]) -> dict:
    """Serialize debate state to an A2A-compatible JSON-RPC envelope."""
    transcript = []
    for m in messages:
        entry = {
            "speaker": m.speaker,
            "turn": m.turn,
            "content": m.content,
            "intent": m.intent,
            "addressed": m.addressed,
            "next_action": m.next_action,
            "invited": m.invited,
        }
        transcript.append(entry)

    return {
        "jsonrpc": "2.0",
        "method": "agora/debate",
        "params": {
            "topic": topic,
            "agents": agents,
            "transcript": transcript,
        },
    }


def from_a2a(payload: dict) -> tuple:
    """Deserialize an A2A envelope back to (topic, agents, messages)."""
    params = payload["params"]
    topic = params["topic"]
    agents = params["agents"]
    messages = []
    for entry in params["transcript"]:
        messages.append(Message(
            speaker=entry["speaker"],
            turn=entry["turn"],
            content=entry["content"],
            intent=entry.get("intent", "propose"),
            addressed=entry.get("addressed", "all"),
            next_action=entry.get("next_action", "continue"),
        ))
    return topic, agents, messages


# ── Structured Output Mode ─────────────────────────────────────────────

CLAIM_RE = re.compile(
    r"@claim\s*:\s*(.+?)\s*\|\s*evidence\s*:\s*(.+?)\s*\|\s*confidence\s*:\s*([\d.]+)",
    re.MULTILINE | re.IGNORECASE,
)
COUNTER_RE = re.compile(r"^@counter\s*:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)


@dataclass
class StructuredMessage(Message):
    claims: list = field(default_factory=list)   # [{claim, evidence, confidence}]
    counter_to: Optional[str] = None             # which claim this counters


def parse_structured_reply(raw: str, speaker: str, turn: int) -> StructuredMessage:
    """Parse a reply into a StructuredMessage with claims and counter info."""
    base = parse_reply(raw, speaker, turn)

    claims = []
    for m in CLAIM_RE.finditer(raw):
        try:
            confidence = float(m.group(3))
        except ValueError:
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        claims.append({
            "claim": m.group(1).strip(),
            "evidence": m.group(2).strip(),
            "confidence": confidence,
        })

    counter_match = COUNTER_RE.search(raw)
    counter_to = counter_match.group(1).strip() if counter_match else None

    return StructuredMessage(
        speaker=base.speaker,
        turn=base.turn,
        content=base.content,
        intent=base.intent,
        addressed=base.addressed,
        next_action=base.next_action,
        claims=claims,
        counter_to=counter_to,
    )


STRUCTURED_DIRECTIVES = """

In addition to the standard Agora directives, you may include structured
claims in your reply. Each claim should be on its own line using this format:

@claim: <your claim text> | evidence: <supporting evidence> | confidence: <0.0-1.0>

If your argument directly counters a previous claim, add:

@counter: <the claim text you are countering>

You may include multiple @claim lines. Place them before the standard
@intent/@addressed/@next directive block."""
