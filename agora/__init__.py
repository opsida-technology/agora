"""
Agora — A minimal agent-to-agent conversation protocol.

    from agora import run
    run("configs/mlx_offline.yaml")

    from agora.protocol import Message, parse_reply
    from agora.backends import MLXBackend, OllamaBackend, CLIBackend
    from agora.agent import Agent
"""
from .protocol import Message, parse_reply, INTENTS, AGORA_INSTRUCTION
from .backends import (
    Backend, MLXBackend, OllamaBackend, CLIBackend,
    BACKEND_REGISTRY, make_backend,
)
from .agent import Agent
from .orchestrator import run

__all__ = [
    "Message", "parse_reply", "INTENTS", "AGORA_INSTRUCTION",
    "Backend", "MLXBackend", "OllamaBackend", "CLIBackend",
    "BACKEND_REGISTRY", "make_backend",
    "Agent",
    "run",
]
