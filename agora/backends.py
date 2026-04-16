"""
agora.backends — Backend implementations for model inference.

Each backend provides generate() (batch) and stream() (token-by-token).

Supported:
    MLXBackend     — Apple Silicon native via mlx-lm
    OllamaBackend  — HTTP, /api/chat endpoint (streaming NDJSON)
    CLIBackend     — subprocess (claude / codex / gemini CLI)
"""
import gc
import json
import re as _re
import subprocess
import urllib.request
from typing import Iterator

from .protocol import build_chat_messages, build_cli_prompt
from .display import status


# ── Base ────────────────────────────────────────────────────────────────

class Backend:
    is_local = False
    model_id = ""

    def load(self): pass
    def unload(self): pass

    def stream(self, system, topic, history, self_name) -> Iterator[str]:
        """Yield text chunks.  Default: single chunk via generate()."""
        yield self.generate(system, topic, history, self_name)

    def generate(self, system, topic, history, self_name) -> str:
        raise NotImplementedError


# ── MLX ─────────────────────────────────────────────────────────────────

class MLXBackend(Backend):
    is_local = True

    def __init__(self, model: str, max_tokens: int = 400):
        self.model_id = model
        self.max_tokens = max_tokens
        self._m = None
        self._t = None

    def load(self):
        if self._m is not None:
            return
        from mlx_lm import load, generate
        import mlx.core as mx
        status(f"loading MLX {self.model_id}...")
        self._m, self._t = load(self.model_id)
        # warmup
        p = self._t.apply_chat_template(
            [{"role": "user", "content": "Reply 'ready'."}],
            add_generation_prompt=True, tokenize=False)
        generate(self._m, self._t, prompt=p, max_tokens=6, verbose=False)
        mx.metal.clear_cache()

    def unload(self):
        import mlx.core as mx
        self._m = None
        self._t = None
        gc.collect()
        mx.metal.clear_cache()

    def _prompt(self, system, topic, history, self_name):
        msgs = build_chat_messages(system, topic, history, self_name)
        return self._t.apply_chat_template(
            msgs, add_generation_prompt=True, tokenize=False)

    def stream(self, system, topic, history, self_name):
        import mlx.core as mx
        prompt = self._prompt(system, topic, history, self_name)
        try:
            from mlx_lm.utils import stream_generate
            prev = ""
            for resp in stream_generate(
                    self._m, self._t, prompt=prompt,
                    max_tokens=self.max_tokens):
                text = resp.text if hasattr(resp, "text") else str(resp)
                if len(text) > len(prev):
                    yield text[len(prev):]
                    prev = text
        except (ImportError, AttributeError):
            yield self.generate(system, topic, history, self_name)
        mx.metal.clear_cache()

    def generate(self, system, topic, history, self_name):
        from mlx_lm import generate as gen
        import mlx.core as mx
        prompt = self._prompt(system, topic, history, self_name)
        out = gen(self._m, self._t, prompt=prompt,
                  max_tokens=self.max_tokens, verbose=False).strip()
        mx.metal.clear_cache()
        return out


# ── Ollama ──────────────────────────────────────────────────────────────

class OllamaBackend(Backend):
    is_local = True

    def __init__(self, model: str, host: str = "http://localhost:11434",
                 max_tokens: int = 400, temperature: float = 0.8):
        self.model_id = model
        self.host = host.rstrip("/")
        self.max_tokens = max_tokens
        self.temperature = temperature

    def load(self):
        status(f"warming up Ollama {self.model_id}...")
        req = urllib.request.Request(
            f"{self.host}/api/generate",
            data=json.dumps({"model": self.model_id,
                             "prompt": "ready", "stream": False,
                             "options": {"num_predict": 4}}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=60).read()
        except Exception as e:
            status(f"warning: Ollama warmup failed ({e})", warn=True)

    def stream(self, system, topic, history, self_name):
        msgs = build_chat_messages(system, topic, history, self_name)
        req = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps({
                "model": self.model_id, "messages": msgs,
                "stream": True,
                "options": {"num_predict": self.max_tokens,
                            "temperature": self.temperature},
            }).encode(),
            headers={"Content-Type": "application/json"})
        try:
            resp = urllib.request.urlopen(req, timeout=300)
            for line in resp:
                if not line.strip():
                    continue
                chunk = json.loads(line)
                token = chunk.get("message", {}).get("content", "")
                if token:
                    yield token
                if chunk.get("done"):
                    break
        except Exception as e:
            yield f"[ERROR Ollama]: {e}"

    def generate(self, system, topic, history, self_name):
        msgs = build_chat_messages(system, topic, history, self_name)
        req = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps({
                "model": self.model_id, "messages": msgs,
                "stream": False,
                "options": {"num_predict": self.max_tokens,
                            "temperature": self.temperature},
            }).encode(),
            headers={"Content-Type": "application/json"})
        try:
            resp = json.loads(urllib.request.urlopen(req, timeout=300).read())
            return resp["message"]["content"].strip()
        except Exception as e:
            return f"[ERROR Ollama]: {e}"


# ── API (OpenAI-compatible) ─────────────────────────────────────────────

class APIBackend(Backend):
    """Generic backend for any OpenAI-compatible HTTP API.

    Works with vLLM, LiteLLM, Ollama's OpenAI endpoint, and any server
    that implements POST /chat/completions with the standard schema.
    """
    is_local = False

    def __init__(self, model: str, base_url: str = "http://localhost:8000/v1",
                 api_key: str = "none", max_tokens: int = 400,
                 temperature: float = 0.8):
        self.model_id = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.temperature = temperature

    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def generate(self, system, topic, history, self_name) -> str:
        msgs = build_chat_messages(system, topic, history, self_name)
        body = json.dumps({
            "model": self.model_id,
            "messages": msgs,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": False,
        }).encode()
        req = urllib.request.Request(
            self._endpoint(), data=body, headers=self._headers())
        try:
            resp = json.loads(urllib.request.urlopen(req, timeout=300).read())
            return resp["choices"][0]["message"]["content"].strip()
        except Exception as e:
            return f"[ERROR API]: {e}"

    def stream(self, system, topic, history, self_name) -> Iterator[str]:
        msgs = build_chat_messages(system, topic, history, self_name)
        body = json.dumps({
            "model": self.model_id,
            "messages": msgs,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": True,
        }).encode()
        req = urllib.request.Request(
            self._endpoint(), data=body, headers=self._headers())
        try:
            resp = urllib.request.urlopen(req, timeout=300)
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line == "data: [DONE]":
                    break
                if not line.startswith("data: "):
                    continue
                try:
                    chunk = json.loads(line[6:])
                    delta = chunk["choices"][0].get("delta", {})
                    token = delta.get("content", "")
                    if token:
                        yield token
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
        except Exception as e:
            yield f"[ERROR API]: {e}"


# ── CLI ─────────────────────────────────────────────────────────────────

# Output cleaners strip wrapper noise from specific CLIs.
# Each takes raw stdout and returns the actual model response.

def _clean_codex(raw: str) -> str:
    """Strip Codex exec header/footer, keep only model response."""
    lines = raw.split("\n")
    # Find "codex" speaker line — response starts after it
    start = 0
    for i, line in enumerate(lines):
        if line.strip() == "codex":
            start = i + 1
            break
    # Find "tokens used" footer — response ends before it
    end = len(lines)
    for i in range(len(lines) - 1, start - 1, -1):
        if lines[i].strip() == "tokens used":
            end = i
            break
    # Also strip any trailing count line (e.g. "1,102")
    while end > start and _re.match(r"^[\d,]+$", lines[end - 1].strip()):
        end -= 1
    return "\n".join(lines[start:end]).strip()


def _clean_gemini(raw: str) -> str:
    """Strip Gemini CLI startup noise (credentials, profiler, etc.)."""
    lines = raw.split("\n")
    clean = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[STARTUP]"):
            continue
        if stripped.startswith("Loaded cached credentials"):
            continue
        if stripped.startswith("[ERROR gemini]"):
            # Keep error lines visible — don't swallow them
            pass
        if _re.match(r"^\[[\w_]+\]", stripped) and "duration:" in stripped:
            continue
        clean.append(line)
    return "\n".join(clean).strip()


def _clean_noop(raw: str) -> str:
    return raw.strip()


_OUTPUT_CLEANERS = {
    "codex": _clean_codex,
    "gemini": _clean_gemini,
}


class CLIBackend(Backend):
    """CLI backend with session persistence.

    First call starts a session. Subsequent calls resume it so the CLI
    keeps its conversation context (no cold start, no re-sending full
    history).

    Supported session modes:
        claude  — --session-id <uuid>, then --continue --session-id <uuid>
        codex   — session id captured from first run, then `exec resume <id>`
        gemini  — session index captured, then --resume <index>
        other   — no session support, full prompt every call (fallback)
    """
    is_local = False

    def __init__(self, command: list, timeout: int = 120):
        self.command = command    # e.g. ["claude", "-p"]
        self.timeout = timeout
        self.model_id = command[0]
        self._clean = _OUTPUT_CLEANERS.get(command[0], _clean_noop)
        self._session_id: str | None = None
        self._turn_count = 0
        self._last_history_len = 0

    # ── Session-aware command building ──────────────────────────────

    def _build_cmd(self, prompt: str, is_first: bool) -> tuple[list[str], str | None]:
        """Return (command_args, stdin_input_or_None)."""
        exe = self.command[0]

        if exe == "claude":
            import uuid
            if is_first:
                self._session_id = str(uuid.uuid4())
                cmd = self.command + [
                    "--session-id", self._session_id,
                ]
            else:
                cmd = self.command + [
                    "--resume", self._session_id,
                ]
            # claude -p reads from stdin
            return cmd, prompt

        elif exe == "codex":
            if is_first or not self._session_id:
                # First turn: normal exec, capture session id from output
                return self.command + [prompt], None
            else:
                # Resume: codex exec resume <session_id> <prompt>
                base = self.command[:-1] if self.command[-1] == "exec" else self.command
                return base + ["exec", "resume", self._session_id, prompt], None

        elif exe == "gemini":
            if is_first or self._session_id is None:
                # First turn: gemini --prompt <text>
                return ["gemini", "--prompt", prompt], None
            else:
                # Resume: gemini --resume <index> --prompt <text>
                return ["gemini", "--resume", self._session_id,
                        "--prompt", prompt], None

        else:
            # Generic CLI — no session support, pass prompt as arg
            return self.command + [prompt], None

    def _capture_codex_session(self, raw: str):
        """Extract session id from codex exec output."""
        for line in raw.split("\n"):
            if line.strip().startswith("session id:"):
                sid = line.split(":", 1)[1].strip()
                if sid:
                    self._session_id = sid
                    break

    def _capture_gemini_session(self):
        """Get the latest session index from gemini --list-sessions."""
        try:
            r = subprocess.run(
                ["gemini", "--list-sessions"],
                capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                # Use "latest" which always points to the most recent
                self._session_id = "latest"
        except Exception:
            pass

    # ── Core run ────────────────────────────────────────────────────

    def _run(self, prompt: str) -> tuple[str, str, int]:
        """Run one turn and return (stdout, stderr, returncode)."""
        is_first = self._turn_count == 0
        cmd, stdin_input = self._build_cmd(prompt, is_first)

        try:
            if stdin_input is not None:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True)
                out, err = proc.communicate(
                    input=stdin_input, timeout=self.timeout)
                return out, err, proc.returncode
            else:
                r = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=self.timeout)
                return r.stdout, r.stderr, r.returncode
        except subprocess.TimeoutExpired:
            if stdin_input is not None:
                proc.kill()
            return "", "", -1

    def stream(self, system, topic, history, self_name):
        """Real token-by-token streaming via subprocess stdout."""
        is_first = self._turn_count == 0

        if is_first:
            prompt = build_cli_prompt(system, topic, history, self_name)
        else:
            new_msgs = history[self._last_history_len:]
            if new_msgs:
                parts = ["NEW MESSAGES SINCE YOUR LAST TURN:"]
                for m in new_msgs:
                    parts.append(f"  [{m.speaker} — {m.intent}]: {m.content}")
                parts.append(f"\nYou are [{self_name}]. Your turn.")
                prompt = "\n".join(parts)
            else:
                prompt = f"You are [{self_name}]. Your turn."

        cmd, stdin_input = self._build_cmd(prompt, is_first)

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if stdin_input is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            yield f"[COMMAND NOT FOUND: {self.command[0]}]"
            return

        # Feed stdin if needed, then close it so the process can proceed.
        if stdin_input is not None:
            try:
                proc.stdin.write(stdin_input.encode())
                proc.stdin.close()
            except BrokenPipeError:
                pass

        # Stream stdout line by line, filtering noise in real-time.
        raw_chunks: list[str] = []
        _noise = ("Loaded cached credentials", "[STARTUP]", "[ERROR gemini]",
                  "Recording metric for phase:", "StartupProfiler",
                  "session id:", "tokens used", "workdir:", "model:",
                  "provider:", "approval:", "sandbox:", "reasoning effort:",
                  "reasoning summaries:", "mcp startup:")
        try:
            for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace")
                raw_chunks.append(line)
                stripped = line.strip()
                # Skip known CLI noise lines
                if any(stripped.startswith(n) or n in stripped for n in _noise):
                    continue
                # Skip codex header lines (e.g. "OpenAI Codex v0.114.0")
                if stripped.startswith("OpenAI Codex") or stripped == "--------":
                    continue
                # Skip codex speaker labels and token counts
                if stripped in ("user", "codex", "") or _re.match(r"^[\d,]+$", stripped):
                    continue
                yield line
        except OSError:
            pass

        # Wait for process to finish and capture stderr.
        try:
            proc.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            yield f"\n[TIMEOUT after {self.timeout}s]"
            return

        stderr = proc.stderr.read().decode("utf-8", errors="replace")
        rc = proc.returncode

        # Bookkeeping — mirror what generate() does.
        self._turn_count += 1
        self._last_history_len = len(history)

        if rc == -1:
            yield f"\n[TIMEOUT after {self.timeout}s]"
            return
        if rc != 0:
            yield f"\n[ERROR {self.command[0]}]: {stderr.strip()[:200]}"
            return

        # Capture session id from first run.
        raw_output = "".join(raw_chunks)
        if self._turn_count == 1:
            if self.command[0] == "codex":
                self._capture_codex_session(raw_output)
            elif self.command[0] == "gemini":
                self._capture_gemini_session()

    def generate(self, system, topic, history, self_name):
        is_first = self._turn_count == 0

        if is_first:
            # First turn: send full context (system + topic + history).
            prompt = build_cli_prompt(system, topic, history, self_name)
        else:
            # Subsequent turns: CLI already has context from the session.
            # Only send the new messages since last turn.
            new_msgs = history[self._last_history_len:]
            if new_msgs:
                parts = ["NEW MESSAGES SINCE YOUR LAST TURN:"]
                for m in new_msgs:
                    parts.append(f"  [{m.speaker} — {m.intent}]: {m.content}")
                parts.append(f"\nYou are [{self_name}]. Your turn.")
                prompt = "\n".join(parts)
            else:
                prompt = f"You are [{self_name}]. Your turn."

        try:
            out, err, rc = self._run(prompt)
            self._turn_count += 1
            self._last_history_len = len(history)

            if rc == -1:
                return f"[TIMEOUT after {self.timeout}s]"
            if rc != 0:
                return f"[ERROR {self.command[0]}]: {err.strip()[:200]}"

            # Capture session id from first run.
            if self._turn_count == 1:
                if self.command[0] == "codex":
                    self._capture_codex_session(out)
                elif self.command[0] == "gemini":
                    self._capture_gemini_session()

            cleaned = self._clean(out)
            if not cleaned:
                return f"[EMPTY RESPONSE from {self.command[0]}]"
            return cleaned
        except FileNotFoundError:
            return f"[COMMAND NOT FOUND: {self.command[0]}]"


# ── Registry ────────────────────────────────────────────────────────────

BACKEND_REGISTRY: dict[str, type[Backend]] = {
    "mlx": MLXBackend,
    "ollama": OllamaBackend,
    "api": APIBackend,
    "cli": CLIBackend,
}


def make_backend(cfg: dict) -> Backend:
    """Create a backend instance from a config dict (non-mutating)."""
    cfg = dict(cfg)
    kind = cfg.pop("backend", "mlx")
    cls = BACKEND_REGISTRY.get(kind)
    if cls is None:
        raise ValueError(f"Unknown backend: {kind}")
    return cls(**cfg)
