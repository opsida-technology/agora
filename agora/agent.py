"""
agora.agent — Agent: a named participant backed by a model.
"""
from dataclasses import dataclass, field
from typing import Optional, Callable

from .protocol import Message, parse_reply
from .backends import Backend


@dataclass
class Agent:
    name: str
    system: str
    backend: Backend
    yielded: bool = False
    stats: dict = field(default_factory=lambda: {"turns": 0, "intents": {}})

    def speak(self, topic: str, history: list[Message], turn: int,
              on_token: Optional[Callable[[str], None]] = None) -> Message:
        """Run one turn.  If *on_token* is given, stream chunks to it."""
        if on_token:
            parts: list[str] = []
            for chunk in self.backend.stream(
                    self.system, topic, history, self.name):
                parts.append(chunk)
                on_token(chunk)
            raw = "".join(parts)
        else:
            raw = self.backend.generate(
                self.system, topic, history, self.name)

        msg = parse_reply(raw, self.name, turn)
        if turn > 1 and (msg.intent == "yield" or msg.next_action == "yield"):
            self.yielded = True
        self.stats["turns"] += 1
        self.stats["intents"][msg.intent] = \
            self.stats["intents"].get(msg.intent, 0) + 1
        return msg
