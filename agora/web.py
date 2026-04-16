"""
agora.web — Dashboard + live debate streaming UI.

    python -m agora.web                          # dashboard on :8420
    python -m agora.web configs/example.yaml     # auto-start debate

Zero-dependency beyond stdlib + PyYAML.
"""
import json
import logging
import math
import queue
import re
import shutil
import socket
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs

import yaml

from .protocol import Message, DIRECTIVE_RE
from .agent import Agent
from .backends import make_backend, CLIBackend
from .display import estimate_gb
from .save import save_from_bus, DEBATES_DIR
from .health import run_health


log = logging.getLogger("agora")


# ── Template loading ──────────────────────────────────────────────────

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_template_cache: dict[str, str] = {}

_MIME_TYPES = {
    ".css": "text/css",
    ".js": "application/javascript",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def _load_template(name: str) -> str:
    """Load an HTML template from agora/templates/, cached after first read."""
    if name not in _template_cache:
        _template_cache[name] = (_TEMPLATES_DIR / name).read_text(encoding="utf-8")
    return _template_cache[name]


CONFIGS_DIR = Path(__file__).resolve().parent.parent / "configs"


# ── Event bus (resettable) ─────────────────────────────────────────────

class EventBus:
    """Thread-safe pub/sub with replay buffer.  Call reset() between debates."""

    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._log: list[str] = []
        self._client_connected = threading.Event()

    def reset(self):
        with self._lock:
            for q in self._subs:
                try:
                    q.put_nowait(None)   # poison pill
                except queue.Full:
                    pass
            self._subs.clear()
            self._log.clear()
        self._client_connected.clear()

    def publish(self, event: str, data: dict):
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        with self._lock:
            self._log.append(payload)
            dead = []
            for q in self._subs:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._subs.remove(q)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=4096)
        with self._lock:
            for payload in self._log:
                q.put_nowait(payload)
            self._subs.append(q)
        self._client_connected.set()
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def wait_for_client(self):
        self._client_connected.wait()


# ── DebateServer — all mutable state lives here ───────────────────────

class DebateServer(ThreadingHTTPServer):
    """Subclass of ThreadingHTTPServer that owns all debate state."""

    daemon_threads = True

    def __init__(self, addr, handler_class, cfg_path=None):
        super().__init__(addr, handler_class)
        self.bus = EventBus()
        self.debate_thread: threading.Thread | None = None
        self.debate_stop = threading.Event()
        self.moderator_queue: queue.Queue = queue.Queue()
        self.human_response = threading.Event()
        self.debate_paused = threading.Event()
        self.active_config: str | None = None
        self._initial_cfg = cfg_path

    def debate_running(self) -> bool:
        return self.debate_thread is not None and self.debate_thread.is_alive()

    def start_debate(self, cfg_path: str) -> bool:
        """Start a debate in background. Stops any running debate first."""
        if self.debate_thread and self.debate_thread.is_alive():
            log.info("Stopping previous debate before starting new one")
            self.stop_debate()
        self.bus.reset()
        self.debate_stop.clear()
        self.debate_thread = threading.Thread(
            target=self._run_debate, args=(cfg_path,), daemon=True)
        self.debate_thread.start()
        log.info("Debate started: %s", cfg_path)
        return True

    def stop_debate(self):
        """Signal the running debate to stop."""
        log.info("Stopping debate")
        self.debate_stop.set()
        t = self.debate_thread
        if t is not None:
            try:
                if t.is_alive():
                    t.join(timeout=5)
            except Exception:
                pass
            self.debate_thread = None

    def continue_debate(self, cfg_path: str, extra_context: str,
                        extra_turns: int = 6) -> bool:
        """Continue a finished debate with additional context/questions."""
        if self.debate_running():
            self.stop_debate()

        parent_dir = Path(cfg_path).parent.name

        with open(cfg_path) as f:
            cfg = yaml.safe_load(f)

        decision_path = Path(cfg_path).parent / "decision.md"
        prev_decision = ""
        if decision_path.exists():
            prev_decision = decision_path.read_text(encoding="utf-8")

        existing_context = cfg.get("context", "")
        continuation = "\n\nCONTINUATION — the debate previously concluded."
        if prev_decision:
            continuation += f"\n\nPREVIOUS DECISION SUMMARY:\n{prev_decision}"
        continuation += f"\n\nNEW INPUT FROM MODERATOR:\n{extra_context}"
        continuation += "\n\nAddress the new input. Build on the previous debate — do not repeat settled points."

        cfg["context"] = existing_context + continuation
        n_agents = len(cfg.get("agents", []))
        cfg["max_turns"] = extra_turns + n_agents

        parent_chain = cfg.get("_parent_chain", [])
        parent_chain.append(parent_dir)
        cfg["_parent_chain"] = parent_chain
        cfg["_parent_debate"] = parent_dir

        temp_path = str(CONFIGS_DIR / "_continue.yaml")
        with open(temp_path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)

        self.active_config = "_continue.yaml"
        return self.start_debate(temp_path)

    # ── Debate runner (runs in background thread) ─────────────────────

    def _run_debate(self, cfg_path: str):
        bus = self.bus
        try:
            self._run_debate_inner(cfg_path, bus)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            bus.publish("done", {"reason": f"Debate crashed: {e}"})
            log.error("Debate crashed:\n%s", tb)

    def _run_debate_inner(self, cfg_path: str, bus: EventBus):
        bus.wait_for_client()
        bus.publish("status", {"text": "Preparing debate..."})

        if self.debate_stop.is_set():
            return

        # Health check — quick install-only check
        bus.publish("status", {"text": "Checking CLI availability..."})
        needed_clis = set()
        with open(cfg_path) as _f:
            _cfg_check = yaml.safe_load(_f)
        for _a in _cfg_check.get("agents", []):
            _cmd = _a.get("command", [])
            if _cmd:
                needed_clis.add(_cmd[0])
        missing = [n for n in needed_clis if not shutil.which(n)]
        if missing:
            bus.publish("status", {"text": f"Warning: {', '.join(missing)} not found in PATH — continuing anyway..."})

        with open(cfg_path) as f:
            cfg = yaml.safe_load(f)

        topic = cfg["topic"]
        max_turns = cfg.get("max_turns", 20)
        mode = cfg.get("memory_mode", "auto")
        language = cfg.get("language", "English")
        context = cfg.get("context", "")

        # Orchestration config
        orch = cfg.get("orchestration", {})
        turn_order = orch.get("turn_order", None)
        phase_plan = orch.get("phase_plan", [])
        final_contract = orch.get("final_output_contract", "")

        # Debate dynamics config
        max_retries = cfg.get("max_retries", 3)
        max_consecutive_pair = cfg.get("max_consecutive_pair", 3)
        anti_consensus = cfg.get("anti_consensus", True)
        moderator_control = cfg.get("moderator_control", False)
        min_rounds = cfg.get("min_rounds", 2)

        agents = []
        num_agents = len(cfg.get("agents", []))
        min_yield_turn = max(1, max_turns // (num_agents * 2)) if num_agents > 0 else 1

        additions = ""
        if language and language.lower() != "english":
            additions += f"\n\nIMPORTANT: You MUST respond in {language}."
        if context:
            additions += f"\n\nBACKGROUND MATERIAL (read carefully before responding):\n{context}"
        additions += (
            "\n\nHUMAN-IN-THE-LOOP: If you encounter something you cannot verify, "
            "lack critical information, or need a factual input that only a human "
            "can provide, use @addressed: human in your directive block. "
            "The debate will pause and a human will answer your question.")

        for a_cfg in cfg["agents"]:
            ac = dict(a_cfg)
            name = ac.pop("name")
            system = ac.pop("system") + additions
            turn_budget = ac.pop("turn_budget", None)
            agent = Agent(name=name, system=system, backend=make_backend(ac),
                          turn_budget=turn_budget,
                          min_turns_before_yield=min_yield_turn)
            agents.append(agent)

        est = estimate_gb(agents)
        if mode == "auto":
            mode = "co-resident" if est <= 17 else "hot-swap"

        # Build turn-order rotation
        if turn_order:
            agent_by_name = {a.name: a for a in agents}
            rr = [agent_by_name[n] for n in turn_order if n in agent_by_name]
            for a in agents:
                if a not in rr:
                    rr.append(a)
        else:
            rr = list(agents)

        def _get_phase(t):
            for p in phase_plan:
                turns_str = str(p.get("turns", ""))
                if "-" in turns_str:
                    lo, hi = turns_str.split("-", 1)
                    if int(lo) <= t <= int(hi):
                        return p
                elif turns_str.isdigit() and int(turns_str) == t:
                    return p
            return None

        agent_list = [{"name": a.name, "model": a.backend.model_id,
                       "local": a.backend.is_local} for a in agents]
        bus.publish("header", {"topic": topic, "agents": agent_list,
                               "est_gb": est, "mode": mode,
                               "language": language, "max_turns": max_turns})

        if mode == "co-resident":
            for a in agents:
                if a.backend.is_local:
                    bus.publish("status", {"text": f"Loading {a.name}..."})
                    a.backend.load()

        _CONVERGE_INTENTS = frozenset({"concede", "synthesize"})
        _DIVERGE_INTENTS = frozenset({"critique", "defend"})
        _FAIL_MARKERS = ("[EMPTY RESPONSE", "[ERROR ", "[TIMEOUT", "[COMMAND NOT FOUND")

        default_budget = math.ceil(max_turns / max(len(agents), 1))
        turn_budgets = {a.name: (getattr(a, "turn_budget", None) or default_budget)
                        for a in agents}
        turns_used: dict[str, int] = {a.name: 0 for a in agents}

        history, turn, last_msg = [], 0, None
        rr_idx = 0
        last_phase_num = 0
        round_turn_count = 0
        completed_rounds = 0
        fail_streak: dict[str, int] = {a.name: 0 for a in agents}
        pair_streak: list[str] = []
        consensus_streak = 0
        last_conceder: str | None = None

        def on_token(chunk):
            bus.publish("token", {"text": chunk})

        while turn < max_turns and not self.debate_stop.is_set():
            active = [a for a in agents if not a.yielded]
            if not active:
                bus.publish("done", {"reason": "All agents yielded."})
                break
            if len(active) < 2:
                closer = active[0]
                turn += 1
                bus.publish("turn_start", {"turn": turn, "name": closer.name,
                                           "closing": True})
                close_history = history + [Message(
                    speaker="moderator", turn=turn,
                    content=("All other participants have yielded. "
                             "Provide your closing statement — summarise your "
                             "final position and key takeaways."),
                    intent="question", addressed=closer.name,
                    next_action="continue")]
                msg = closer.speak(topic, close_history, turn, on_token=on_token)
                bus.publish("turn_end", {
                    "turn": turn, "name": msg.speaker, "closing": True,
                    "intent": msg.intent, "addressed": msg.addressed,
                    "next_action": msg.next_action, "content": msg.content,
                    "yielded": True, "invited": msg.invited,
                })
                history.append(msg)
                closer.yielded = True
                bus.publish("done", {"reason": f"Only {closer.name} remained — debate complete."})
                break

            # Phase transition
            next_turn = turn + 1
            phase = _get_phase(next_turn)
            if phase and phase.get("phase", 0) != last_phase_num:
                last_phase_num = phase["phase"]
                objective = phase.get("objective", "").strip()
                if objective:
                    phase_msg = Message(
                        speaker="moderator", turn=next_turn,
                        content=f"[Phase {last_phase_num}] {objective}",
                        intent="question", addressed="all",
                        next_action="continue")
                    history.append(phase_msg)
                    bus.publish("moderator_msg", {"turn": next_turn,
                                "content": f"Phase {last_phase_num}: {objective}"})

            # Anti-monopoly: detect ping-pong
            speaker = None
            force_rr = False
            if len(pair_streak) >= max_consecutive_pair * 2:
                recent = pair_streak[-(max_consecutive_pair * 2):]
                if len(set(recent)) == 2:
                    force_rr = True

            # Speaker selection: invite (unless force_rr)
            if last_msg and not force_rr:
                target = last_msg.invited
                if not target and last_msg.addressed not in ("all", last_msg.speaker):
                    target = last_msg.addressed
                if target:
                    speaker = next(
                        (a for a in agents
                         if a.name == target and not a.yielded), None)

            # Turn budget priority
            if speaker is None:
                eligible = [
                    a for a in active
                    if turns_used[a.name] < turn_budgets[a.name]
                    and (last_msg is None or a.name != last_msg.speaker
                         or len(active) == 1)
                ]
                if eligible:
                    eligible.sort(
                        key=lambda a: turn_budgets[a.name] - turns_used[a.name],
                        reverse=True)
                    speaker = eligible[0]

            # Fallback: round-robin
            if speaker is None:
                for _ in range(len(rr)):
                    cand = rr[rr_idx % len(rr)]
                    rr_idx += 1
                    if not cand.yielded and (last_msg is None
                                             or cand.name != last_msg.speaker
                                             or len(active) == 1):
                        speaker = cand
                        break
            if speaker is None:
                break

            if mode == "hot-swap" and speaker.backend.is_local:
                for o in agents:
                    if o is not speaker and o.backend.is_local:
                        o.backend.unload()
                speaker.backend.load()

            # Pause check
            if self.debate_paused.is_set():
                bus.publish("status", {"text": "Debate paused — type your message and press Send to resume."})
            while self.debate_paused.is_set() and not self.debate_stop.is_set():
                if not self.moderator_queue.empty():
                    human_msg = self.moderator_queue.get()
                    history.append(Message(
                        speaker="human", turn=turn,
                        content=human_msg,
                        intent="propose", addressed="all",
                        next_action="continue"))
                    bus.publish("moderator_msg", {"turn": turn, "content": f"[Human] {human_msg}"})
                    self.debate_paused.clear()
                    break
                time.sleep(1)

            if self.debate_stop.is_set():
                break

            turn += 1
            bus.publish("turn_start", {"turn": turn, "name": speaker.name,
                                       "closing": False})

            msg = speaker.speak(topic, history, turn, on_token=on_token)

            # Retry logic
            is_fail = (any(msg.content.startswith(m) for m in _FAIL_MARKERS)
                       or len(msg.content.strip()) < 10)
            if is_fail:
                fail_streak[speaker.name] += 1
                if fail_streak[speaker.name] < max_retries:
                    bus.publish("turn_end", {
                        "turn": turn, "name": msg.speaker, "closing": False,
                        "intent": "error", "addressed": "",
                        "next_action": "continue", "content": msg.content,
                        "yielded": False, "invited": None,
                    })
                    bus.publish("moderator_msg", {"turn": turn,
                        "content": f"{speaker.name} failed (attempt {fail_streak[speaker.name]}/{max_retries}), retrying..."})
                    turn -= 1
                    continue
                else:
                    speaker.yielded = True
                    msg.intent = "yield"
                    msg.content = (f"[{speaker.name} auto-yielded after "
                                   f"{fail_streak[speaker.name]} consecutive failures]")
            else:
                fail_streak[speaker.name] = 0

            # Yield protection
            if speaker.yielded:
                all_spoke = all(a.stats["turns"] > 0 for a in agents)
                if not all_spoke:
                    speaker.yielded = False
                    msg.content += "\n\n[Yield blocked — not all agents have spoken yet]"

            bus.publish("turn_end", {
                "turn": turn, "name": msg.speaker, "closing": False,
                "intent": msg.intent, "addressed": msg.addressed,
                "next_action": msg.next_action, "content": msg.content,
                "yielded": speaker.yielded, "invited": msg.invited,
            })
            history.append(msg)
            last_msg = msg
            pair_streak.append(speaker.name)
            turns_used[speaker.name] += 1

            # Anti-consensus
            if anti_consensus:
                if msg.intent in _CONVERGE_INTENTS:
                    consensus_streak += 1
                    if msg.intent == "concede":
                        last_conceder = speaker.name
                elif msg.intent in _DIVERGE_INTENTS:
                    consensus_streak = 0
                    last_conceder = None

                if consensus_streak >= 3 and turn < max_turns * 0.5:
                    target_name = last_conceder or speaker.name
                    mod_content = (
                        f"The group appears to be converging early. "
                        f"{target_name}, can you identify what is being "
                        f"left unexamined? What assumption is the group "
                        f"making that might be wrong?")
                    history.append(Message(
                        speaker="moderator", turn=turn,
                        content=mod_content,
                        intent="question", addressed=target_name,
                        next_action="continue"))
                    bus.publish("moderator_msg", {"turn": turn, "content": mod_content})
                    consensus_streak = 0

            # Moderator convergence check
            if not is_fail:
                round_turn_count += 1
                n_active = len([a for a in agents if not a.yielded])
                if round_turn_count >= n_active and n_active > 0:
                    completed_rounds += 1
                    round_turn_count = 0

                    if (moderator_control and completed_rounds >= min_rounds
                            and not self.debate_stop.is_set()):
                        bus.publish("moderator_msg", {
                            "turn": turn,
                            "content": f"[Moderator evaluating after round {completed_rounds}...]"})
                        try:
                            eval_backend = agents[0].backend
                            eval_backend.load()
                            eval_prompt = (
                                "Based on the debate so far, should it CONTINUE or CONCLUDE?\n"
                                "CONTINUE if important points remain unexamined.\n"
                                "CONCLUDE if the debate has reached sufficient depth.\n\n"
                                "Reply with ONLY one word: CONTINUE or CONCLUDE.")
                            eval_result = eval_backend.generate(
                                "You are a neutral debate moderator. Reply with one word only.",
                                topic, history, "moderator-eval")
                            should_conclude = "conclude" in eval_result.lower()
                            bus.publish("moderator_msg", {
                                "turn": turn,
                                "content": f"[Moderator: {'CONCLUDE — ending debate' if should_conclude else 'CONTINUE — debate proceeds'}]"})
                            if should_conclude:
                                bus.publish("done", {
                                    "reason": f"Moderator concluded after round {completed_rounds}."})
                                break
                        except Exception:
                            pass

            # Human-in-the-loop
            if (msg.addressed == "human" or msg.invited == "human"
                    or msg.next_action == "invite:human"):
                bus.publish("human_needed", {
                    "turn": turn, "speaker": msg.speaker,
                    "question": msg.content,
                })
                self.human_response.clear()
                while not self.human_response.is_set() and not self.debate_stop.is_set():
                    if not self.moderator_queue.empty():
                        human_msg = self.moderator_queue.get()
                        history.append(Message(
                            speaker="human", turn=turn,
                            content=human_msg,
                            intent="propose", addressed=msg.speaker,
                            next_action="continue"))
                        bus.publish("moderator_msg", {
                            "turn": turn, "content": f"[Human] {human_msg}"})
                        self.human_response.set()
                        break
                    self.human_response.wait(timeout=2)

            if speaker.yielded:
                remaining = [a.name for a in agents if not a.yielded]
                if remaining:
                    bus.publish("yield_notice", {
                        "name": speaker.name, "remaining": remaining,
                    })
                    history.append(Message(
                        speaker="moderator", turn=turn,
                        content=(f"{speaker.name} has yielded. "
                                 f"Remaining: {', '.join(remaining)}."),
                        intent="yield", addressed="all",
                        next_action="continue"))

            # Inject queued moderator messages
            while not self.moderator_queue.empty():
                try:
                    human_msg = self.moderator_queue.get_nowait()
                    mod_message = Message(
                        speaker="moderator", turn=turn,
                        content=human_msg,
                        intent="question", addressed="all",
                        next_action="continue")
                    history.append(mod_message)
                    bus.publish("moderator_msg", {"turn": turn, "content": human_msg})
                except queue.Empty:
                    break
        else:
            if not self.debate_stop.is_set():
                bus.publish("done", {"reason": f"Max turns ({max_turns}) reached."})

        if self.debate_stop.is_set():
            bus.publish("done", {"reason": "Debate stopped by user."})
            try:
                save_from_bus(bus, cfg_path=cfg_path)
            except Exception:
                pass
            return

        # Stats
        stats = [{"name": a.name, "turns": a.stats["turns"],
                  "intents": a.stats["intents"], "yielded": a.yielded}
                 for a in agents]
        bus.publish("summary", {"agents": stats})

        # Decision summary — fresh backend
        bus.publish("decision_start", {})
        if final_contract:
            decision_system = (
                "You are a neutral moderator writing the final decision summary. "
                "You must NOT continue the debate or address agents. "
                "Do NOT use Agora directives.\n\n"
                "The debate organizer specified this output contract:\n"
                f"{final_contract}\n\n"
                "Write the decision document following that contract exactly. "
                f"Be concise and specific. Write in {language}.")
        else:
            decision_system = (
                "You are a neutral moderator writing the final decision summary. "
                "You must NOT continue the debate or address agents. "
                "Write ONLY a structured decision document with these sections:\n\n"
                "## Agreed Points\nBullet list of what the group converged on.\n\n"
                "## Open Disagreements\nBullet list of unresolved tensions.\n\n"
                "## Recommended Next Steps\nNumbered list of concrete actions.\n\n"
                "## Agent Contributions\nOne sentence per agent summarizing their key contribution.\n\n"
                "Be concise and specific. Do NOT use Agora directives. "
                f"Write in {language}.")

        decision_backend = CLIBackend(command=["claude", "-p"], timeout=180)
        parts: list[str] = []
        for chunk in decision_backend.stream(
                decision_system, topic, history, "moderator"):
            bus.publish("decision_token", {"text": chunk})
            parts.append(chunk)
        raw = "".join(parts)
        content = DIRECTIVE_RE.sub("", raw).strip()
        bus.publish("decision_end", {"content": content})

        # Auto-save
        try:
            out = save_from_bus(bus, cfg_path=cfg_path)
            bus.publish("saved", {"path": str(out)})
        except Exception as e:
            bus.publish("saved", {"error": str(e)})


# ── Config / debate listing helpers ───────────────────────────────────

def list_configs() -> list[dict]:
    configs = []
    for p in sorted(CONFIGS_DIR.glob("*.yaml")):
        if p.stem.startswith("_"):
            continue
        try:
            with open(p) as f:
                cfg = yaml.safe_load(f)
            agent_clis = []
            for a in cfg.get("agents", []):
                cmd = a.get("command", [])
                backend = a.get("backend", "mlx")
                if cmd:
                    agent_clis.append(cmd[0])
                else:
                    agent_clis.append(backend)
            configs.append({
                "name": p.stem, "file": p.name,
                "topic": cfg.get("topic", "").strip()[:120],
                "agents": len(cfg.get("agents", [])),
                "max_turns": cfg.get("max_turns", 20),
                "clis": list(set(agent_clis)),
                "mtime": p.stat().st_mtime,
            })
        except Exception:
            configs.append({"name": p.stem, "file": p.name,
                            "topic": "(parse error)", "agents": 0,
                            "max_turns": 0, "mtime": 0})
    configs.sort(key=lambda c: c.get("mtime", 0), reverse=True)
    return configs


def list_debates() -> list[dict]:
    """List saved debate folders with metadata."""
    debates = []
    if not DEBATES_DIR.exists():
        return debates
    for d in sorted(DEBATES_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                meta["has_decision"] = (d / "decision.md").exists()
                meta["has_config"] = (d / "config.yaml").exists()
                meta["dir"] = d.name
                debates.append(meta)
            except Exception:
                debates.append({"dir": d.name, "topic": "(corrupt)", "turns": 0})
        else:
            debates.append({"dir": d.name, "topic": "(no metadata)", "turns": 0})
    return debates


# ── HTTP handler ──────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    server: DebateServer  # type hint for IDE

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass

    # ── GET routes ────────────────────────────────────────────────────

    def do_GET(self):
        path = self.path.split("?")[0]
        route = self._GET_ROUTES.get(path)
        if route:
            route(self)
        elif path.startswith("/history/"):
            self._html(_load_template("detail.html"))
        elif path.startswith("/static/"):
            self._serve_static(path[8:])  # strip "/static/"
        else:
            self.send_error(404)

    def _get_dashboard(self):
        self._html(_load_template("dashboard.html"))

    def _get_debate(self):
        self._html(_load_template("debate.html"))

    def _get_events(self):
        self._serve_sse()

    def _get_health(self):
        self._json(run_health(self.server.active_config or "", as_json=False))

    def _get_configs(self):
        self._json(list_configs())

    def _get_config(self):
        qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        fname = qs.get("file", [""])[0]
        fpath = CONFIGS_DIR / fname
        if fpath.exists() and fpath.suffix == ".yaml":
            self._text(fpath.read_text())
        else:
            self.send_error(404)

    def _get_status(self):
        self._json({"running": self.server.debate_running(),
                     "config": self.server.active_config})

    def _get_debates(self):
        self._json(list_debates())

    def _get_debate_file(self):
        qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        dir_name = qs.get("dir", [""])[0]
        fname = qs.get("file", [""])[0]
        fpath = DEBATES_DIR / dir_name / fname
        if fpath.exists() and fpath.is_file():
            self._text(fpath.read_text(encoding="utf-8"))
        else:
            self.send_error(404)

    def _get_debate_a2a(self):
        qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        dir_name = qs.get("dir", [""])[0]
        from .protocol import to_a2a, Message
        meta_path = DEBATES_DIR / dir_name / "meta.json"
        if not meta_path.exists():
            self.send_error(404)
            return
        meta = json.loads(meta_path.read_text())
        # Parse transcript.md to reconstruct messages
        messages = []
        transcript_path = DEBATES_DIR / dir_name / "transcript.md"
        if transcript_path.exists():
            text = transcript_path.read_text(encoding="utf-8")
            # Parse turn blocks: ## Turn N: agent_name
            turn_pattern = re.compile(
                r'^## Turn (\d+): (.+?)(?:\s*\(CLOSING\))?\s*$', re.MULTILINE)
            directive_pattern = re.compile(r'`intent: (\w+)`')
            addressed_pattern = re.compile(r'`to: (\w+)`')
            parts = turn_pattern.split(text)
            # parts: [header, turn1, name1, body1, turn2, name2, body2, ...]
            i = 1
            while i + 2 < len(parts):
                turn_num = int(parts[i])
                speaker = parts[i + 1].strip()
                body = parts[i + 2]
                intent_m = directive_pattern.search(body)
                addr_m = addressed_pattern.search(body)
                # Extract content: between directive line and next section
                lines = body.strip().split('\n')
                content_lines = []
                past_directives = False
                for line in lines:
                    if line.startswith('`intent:') or line.startswith('`to:'):
                        past_directives = True
                        continue
                    if past_directives:
                        content_lines.append(line)
                content = '\n'.join(content_lines).strip()
                messages.append(Message(
                    speaker=speaker, turn=turn_num, content=content,
                    intent=intent_m.group(1) if intent_m else "propose",
                    addressed=addr_m.group(1) if addr_m else "all",
                    next_action="continue"))
                i += 3
        agents = meta.get("agents", [])
        envelope = to_a2a(messages, meta.get("topic", ""), agents)
        envelope["params"]["meta"] = meta
        if (DEBATES_DIR / dir_name / "decision.md").exists():
            envelope["params"]["decision"] = (
                DEBATES_DIR / dir_name / "decision.md"
            ).read_text(encoding="utf-8")
        self._json(envelope)

    def _get_debate_pdf(self):
        """Serve a print-friendly HTML page for browser PDF export."""
        qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        dir_name = qs.get("dir", [""])[0]
        transcript_path = DEBATES_DIR / dir_name / "transcript.md"
        decision_path = DEBATES_DIR / dir_name / "decision.md"
        if not transcript_path.exists():
            self.send_error(404)
            return
        transcript = transcript_path.read_text(encoding="utf-8")
        decision = decision_path.read_text(encoding="utf-8") if decision_path.exists() else ""
        # Simple markdown→HTML conversion for print
        def _md_to_html(text):
            import html as _html
            text = _html.escape(text)
            text = re.sub(r'^### (.+)$', r'<h3>\1</h3>', text, flags=re.MULTILINE)
            text = re.sub(r'^## (.+)$', r'<h2>\1</h2>', text, flags=re.MULTILINE)
            text = re.sub(r'^# (.+)$', r'<h1>\1</h1>', text, flags=re.MULTILINE)
            text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
            text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
            text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
            text = re.sub(r'^[-*] (.+)$', r'<li>\1</li>', text, flags=re.MULTILINE)
            text = re.sub(r'^---+$', '<hr>', text, flags=re.MULTILINE)
            text = text.replace('\n\n', '</p><p>').replace('\n', '<br>')
            return f'<p>{text}</p>'

        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Agora Debate — {dir_name}</title>
<style>
  body {{ font-family: 'Georgia', serif; max-width: 800px; margin: 2em auto;
         color: #1a1a2e; line-height: 1.7; font-size: 14px; }}
  h1 {{ color: #0f172a; font-size: 20px; border-bottom: 2px solid #3b82f6; padding-bottom: 8px; }}
  h2 {{ color: #1e293b; font-size: 16px; margin-top: 1.5em; }}
  h3 {{ color: #334155; font-size: 14px; }}
  code {{ background: #f1f5f9; padding: 1px 4px; border-radius: 3px; font-size: 12px; }}
  hr {{ border: none; border-top: 1px solid #e2e8f0; margin: 1.5em 0; }}
  strong {{ color: #0f172a; }}
  li {{ margin: 4px 0; }}
  .decision {{ background: #f0fdf4; border: 1px solid #86efac; border-radius: 8px;
               padding: 16px; margin-top: 2em; }}
  .decision h1 {{ color: #166534; border-color: #22c55e; }}
  @media print {{ body {{ margin: 0; }} .no-print {{ display: none; }} }}
</style></head><body>
<div class="no-print" style="margin-bottom:1em;">
  <button onclick="window.print()" style="padding:8px 20px;background:#3b82f6;color:white;border:none;border-radius:6px;cursor:pointer;font-size:14px;">Print / Save as PDF</button>
  <button onclick="window.close()" style="padding:8px 20px;background:#64748b;color:white;border:none;border-radius:6px;cursor:pointer;font-size:14px;margin-left:8px;">Close</button>
</div>
{_md_to_html(transcript)}
{"<div class='decision'>" + _md_to_html(decision) + "</div>" if decision else ""}
</body></html>"""
        self._html(html)

    def _get_history(self):
        self._html(_load_template("history.html"))

    def _get_configs_page(self):
        self._html(_load_template("configs.html"))

    _GET_ROUTES: dict  # defined after class body

    # ── DELETE routes ─────────────────────────────────────────────────

    def do_DELETE(self):
        path = self.path.split("?")[0]
        route = self._DELETE_ROUTES.get(path)
        if route:
            route(self)
        else:
            self.send_error(404)

    def _delete_config(self):
        qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        fname = qs.get("file", [""])[0]
        if not fname or not fname.endswith(".yaml"):
            self._json({"error": "Invalid filename"}, 400)
            return
        fpath = CONFIGS_DIR / fname
        if fpath.exists():
            fpath.unlink()
            self._json({"ok": True})
        else:
            self.send_error(404)

    def _delete_debate(self):
        qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        dir_name = qs.get("dir", [""])[0]
        if not dir_name or ".." in dir_name or "/" in dir_name:
            self._json({"error": "Invalid directory"}, 400)
            return
        target = DEBATES_DIR / dir_name
        if target.exists() and target.is_dir():
            shutil.rmtree(target)
            self._json({"ok": True})
        else:
            self.send_error(404)

    _DELETE_ROUTES: dict  # defined after class body

    # ── POST routes ───────────────────────────────────────────────────

    def do_POST(self):
        path = self.path.split("?")[0]
        route = self._POST_ROUTES.get(path)
        if route:
            route(self)
        else:
            self.send_error(404)

    def _post_config(self):
        qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        fname = qs.get("file", [""])[0]
        if not fname or not fname.endswith(".yaml"):
            self._json({"error": "Invalid filename"}, 400)
            return
        body = self._read_body()
        fpath = CONFIGS_DIR / fname
        fpath.write_text(body)
        self._json({"ok": True})

    def _post_start(self):
        body = self._read_body()
        data = json.loads(body)
        cfg_file = data.get("config", "")
        cfg_path = str(CONFIGS_DIR / cfg_file)
        if not Path(cfg_path).exists():
            self._json({"ok": False, "error": "Config not found"}, 404)
            return
        ok = self.server.start_debate(cfg_path)
        if ok:
            self.server.active_config = cfg_file
            self._json({"ok": True})
        else:
            self._json({"ok": False, "error": "Debate already running"})

    def _post_stop(self):
        self.server.stop_debate()
        self.server.active_config = None
        self._json({"ok": True})

    def _post_pause(self):
        self.server.debate_paused.set()
        self._json({"ok": True, "paused": True})

    def _post_resume(self):
        self.server.debate_paused.clear()
        self._json({"ok": True, "paused": False})

    def _post_intervene(self):
        body = self._read_body()
        data = json.loads(body)
        msg = data.get("message", "").strip()
        if not msg:
            self._json({"ok": False, "error": "Empty message"}, 400)
        elif not self.server.debate_running():
            self._json({"ok": False, "error": "No debate running"})
        else:
            self.server.moderator_queue.put(msg)
            if self.server.debate_paused.is_set():
                self.server.debate_paused.clear()
            self._json({"ok": True})

    def _post_continue(self):
        body = self._read_body()
        data = json.loads(body)
        cfg_dir = data.get("debate_dir", "")
        message = data.get("message", "")
        extra_turns = data.get("turns", 6)
        config_path = DEBATES_DIR / cfg_dir / "config.yaml"
        if not config_path.exists():
            self._json({"ok": False, "error": "Config not found in debate"})
        elif self.server.debate_running():
            self._json({"ok": False, "error": "A debate is already running"})
        else:
            ok = self.server.continue_debate(str(config_path), message, extra_turns)
            self._json({"ok": ok})

    def _post_quiz(self):
        body = self._read_body()
        data = json.loads(body)
        ctx = data.get("context", "")
        topic = data.get("topic", "")
        action = data.get("action", "generate")

        if action == "generate":
            quiz_backend = CLIBackend(command=["claude", "-p"], timeout=60)
            prompt = (
                f"You are preparing a multi-agent debate on this topic:\n"
                f"{topic}\n\n"
                f"The user provided this background material:\n"
                f"{ctx}\n\n"
                f"Generate exactly 5 questions about gaps, ambiguities, "
                f"or missing information in this material that would "
                f"help the debate be more productive. For each question, "
                f"provide 3-4 multiple choice options plus an 'Other' option.\n\n"
                f"Respond in JSON array format:\n"
                f'[{{"question": "...", "options": ["A) ...", "B) ...", "C) ...", "Other"]}}]'
            )
            raw = quiz_backend.generate(
                "You output valid JSON only. No markdown, no explanation.",
                prompt, [], "quiz-agent")
            match = re.search(r'\[.*\]', raw, re.DOTALL)
            if match:
                try:
                    questions = json.loads(match.group())
                    self._json({"questions": questions})
                except json.JSONDecodeError:
                    self._json({"questions": [], "raw": raw})
            else:
                self._json({"questions": [], "raw": raw})

        elif action == "answer":
            answers = data.get("answers", [])
            enriched = ctx + "\n\nADDITIONAL CONTEXT FROM PRE-DEBATE Q&A:\n"
            for qa in answers:
                enriched += f"Q: {qa.get('question', '')}\n"
                enriched += f"A: {qa.get('answer', '')}\n\n"
            self._json({"enriched_context": enriched})

    def _post_parse_yaml(self):
        body = self._read_body()
        try:
            cfg = yaml.safe_load(body)
            self._json(cfg if isinstance(cfg, dict) else {"error": "Not a mapping"})
        except Exception as e:
            self._json({"error": str(e)})

    def _post_to_yaml(self):
        body = self._read_body()
        data = json.loads(body)
        self._text(yaml.dump(data, default_flow_style=False,
                             allow_unicode=True, sort_keys=False))

    _POST_ROUTES: dict  # defined after class body

    # ── Response helpers ──────────────────────────────────────────────

    def _read_body(self) -> str:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length).decode() if length else ""

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, content):
        body = content.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, content):
        body = content.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, name: str):
        """Serve a file from agora/static/ with appropriate MIME type."""
        if ".." in name or name.startswith("/"):
            self.send_error(403)
            return
        fpath = _STATIC_DIR / name
        if not fpath.exists() or not fpath.is_file():
            self.send_error(404)
            return
        mime = _MIME_TYPES.get(fpath.suffix, "application/octet-stream")
        body = fpath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _serve_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = self.server.bus.subscribe()
        try:
            while True:
                try:
                    payload = q.get(timeout=30)
                    if payload is None:
                        break
                    self.wfile.write(payload.encode())
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.bus.unsubscribe(q)

    def log_message(self, fmt, *args):
        # Suppress default stderr logging; route through structured logger
        pass

    def log_request(self, code="-", size="-"):
        if self.path != "/events":  # SSE is noisy
            log.info("%s %s %s", self.command, self.path, code)


# Route dispatch tables (dict-based instead of if/elif chains)
Handler._GET_ROUTES = {
    "/": Handler._get_dashboard,
    "/debate": Handler._get_debate,
    "/events": Handler._get_events,
    "/health": Handler._get_health,
    "/api/configs": Handler._get_configs,
    "/api/config": Handler._get_config,
    "/api/status": Handler._get_status,
    "/api/debates": Handler._get_debates,
    "/api/debate-file": Handler._get_debate_file,
    "/api/debate-a2a": Handler._get_debate_a2a,
    "/api/debate-pdf": Handler._get_debate_pdf,
    "/history": Handler._get_history,
    "/configs": Handler._get_configs_page,
}

Handler._DELETE_ROUTES = {
    "/api/config": Handler._delete_config,
    "/api/debate": Handler._delete_debate,
}

Handler._POST_ROUTES = {
    "/api/config": Handler._post_config,
    "/api/start": Handler._post_start,
    "/api/stop": Handler._post_stop,
    "/api/pause": Handler._post_pause,
    "/api/resume": Handler._post_resume,
    "/api/intervene": Handler._post_intervene,
    "/api/continue": Handler._post_continue,
    "/api/quiz": Handler._post_quiz,
    "/api/parse-yaml": Handler._post_parse_yaml,
    "/api/to-yaml": Handler._post_to_yaml,
}


# ── Server entry point ───────────────────────────────────────────────

def _detect_ip() -> str:
    """Best-effort LAN IP detection."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def serve(cfg_path: str | None = None, port: int = 8420):
    """Start dashboard server.  Optionally auto-start a debate."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    server = DebateServer(("0.0.0.0", port), Handler)

    ip = _detect_ip()
    print(f"\n  Agora Dashboard → http://{ip}:{port}")
    print(f"                    http://localhost:{port}")
    print("  Press Ctrl+C to stop\n")

    if cfg_path:
        server.start_debate(cfg_path)

    threading.Thread(target=webbrowser.open,
                     args=(f"http://localhost:{port}",), daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down.")
        server.stop_debate()
        server.server_close()


def _cli_main():
    """Entry point for `agora` console script."""
    import argparse
    ap = argparse.ArgumentParser(description="Agora Dashboard")
    ap.add_argument("config", nargs="?", default=None,
                    help="Path to YAML config (optional — starts dashboard only)")
    ap.add_argument("--port", "-p", type=int, default=8420)
    args = ap.parse_args()
    serve(args.config, port=args.port)


if __name__ == "__main__":
    _cli_main()
