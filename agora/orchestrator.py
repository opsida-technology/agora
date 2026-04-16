"""
agora.orchestrator — Turn-taking loop and config loading.

NOTE: web.py's _run_debate function mirrors the main loop below.
      If you change anti-consensus or turn-budget logic here, apply
      the same changes in web.py._run_debate.
"""
import math
import yaml

from .protocol import Message, DIRECTIVE_RE
from .agent import Agent
from .backends import make_backend
from .display import (
    estimate_gb, emit_token,
    show_header, show_turn_start, show_turn_end,
    show_yield_notice, show_all_yielded, show_last_standing,
    show_max_turns, show_summary,
    show_generating_decision, show_decision,
)

# Intents that count toward an early-consensus streak.
_CONVERGE_INTENTS = frozenset({"concede", "synthesize"})
# Intents that break the consensus streak.
_DIVERGE_INTENTS = frozenset({"critique", "defend"})


def load_agents(cfg: dict) -> list[Agent]:
    """Create Agent instances from a parsed YAML config.

    Each agent dict may include an optional ``turn_budget`` key.
    The value is stored on the Agent instance as ``agent.turn_budget``.
    """
    agents = []
    for a_cfg in cfg["agents"]:
        ac = dict(a_cfg)
        name = ac.pop("name")
        system = ac.pop("system")
        turn_budget = ac.pop("turn_budget", None)  # handled later
        agent = Agent(name=name, system=system, backend=make_backend(ac),
                      turn_budget=turn_budget)
        agents.append(agent)
    return agents


def _init_turn_budgets(agents: list[Agent], max_turns: int) -> dict[str, int]:
    """Return {agent_name: budget} with defaults filled in."""
    default_budget = math.ceil(max_turns / len(agents))
    budgets: dict[str, int] = {}
    for a in agents:
        explicit = getattr(a, "turn_budget", None)
        budgets[a.name] = explicit if explicit is not None else default_budget
    return budgets


def _closing_turn(agent, topic, history, turn, stream):
    """Give the last standing agent a closing statement."""
    turn += 1
    show_turn_start(turn, agent.name, closing=True)

    # Inject moderator cue so the agent knows to summarise.
    close_history = history + [Message(
        speaker="moderator", turn=turn,
        content=(
            "All other participants have yielded. "
            "Provide your closing statement — summarise your final "
            "position and key takeaways."),
        intent="question", addressed=agent.name,
        next_action="continue")]

    callback = emit_token if stream else None
    msg = agent.speak(topic, close_history, turn, on_token=callback)
    show_turn_end(msg, closing=True)
    history.append(msg)
    agent.yielded = True
    return turn


def _decision_summary(agents, topic, history, stream):
    """Ask the first agent's backend to produce a structured summary."""
    show_generating_decision()

    backend = agents[0].backend
    # Make sure it's loaded (hot-swap may have unloaded).
    backend.load()

    system = (
        "You are a neutral moderator. Based on the full debate transcript, "
        "write a structured DECISION SUMMARY with these sections:\n"
        "## Agreed Points\nWhat both sides converged on.\n"
        "## Open Disagreements\nUnresolved tensions.\n"
        "## Recommended Next Steps\nConcrete actions based on the debate.\n"
        "Be concise and specific. Write in the language the debate was "
        "conducted in.")

    parts: list[str] = []
    if stream:
        for chunk in backend.stream(system, topic, history, "moderator"):
            emit_token(chunk)
            parts.append(chunk)
    else:
        parts.append(backend.generate(system, topic, history, "moderator"))

    raw = "".join(parts)
    content = DIRECTIVE_RE.sub("", raw).strip()
    if stream:
        print()  # newline after streamed tokens
    show_decision(content)


def run(cfg_path: str, stream: bool = True):
    """Run a debate from a YAML config file."""
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    topic = cfg["topic"]
    max_turns = cfg.get("max_turns", 20)
    mode = cfg.get("memory_mode", "auto")
    moderator_control = cfg.get("moderator_control", False)
    min_rounds = cfg.get("min_rounds", 2)

    agents = load_agents(cfg)
    # Yield protection: min half of fair share before yield allowed
    min_yield = max(1, max_turns // (len(agents) * 2)) if agents else 1
    for a in agents:
        a.min_turns_before_yield = min_yield
    est = estimate_gb(agents)

    if mode == "auto":
        mode = "co-resident" if est <= 17 else "hot-swap"

    show_header(topic, agents, est, mode)

    if mode == "co-resident":
        for a in agents:
            if a.backend.is_local:
                a.backend.load()

    max_retries = cfg.get("max_retries", 3)
    max_consecutive_pair = cfg.get("max_consecutive_pair", 3)
    anti_consensus = cfg.get("anti_consensus", True)

    history, turn, last_msg = [], 0, None
    fail_streak: dict[str, int] = {a.name: 0 for a in agents}
    pair_streak: list[str] = []          # recent speaker names
    rr = list(agents)
    rr_idx = 0

    # -- Per-agent turn budgets --
    turn_budgets = _init_turn_budgets(agents, max_turns)
    turns_used: dict[str, int] = {a.name: 0 for a in agents}

    # -- Anti-consensus tracking --
    consensus_streak = 0        # consecutive concede/synthesize turns
    last_conceder: str | None = None  # name of most recent conceder

    # -- Moderator convergence control --
    round_turn_count = 0
    completed_rounds = 0

    while turn < max_turns:
        active = [a for a in agents if not a.yielded]
        if not active:
            show_all_yielded()
            break
        if len(active) < 2:
            # Last agent standing — closing statement.
            turn = _closing_turn(active[0], topic, history, turn, stream)
            show_last_standing(active[0].name)
            break

        # Determine next speaker: honour invite or addressed, else round-robin.
        # Never let the same agent speak twice in a row (unless invited).
        #
        # Anti-monopoly: if the same pair has been ping-ponging for
        # max_consecutive_pair turns, ignore the invite and force round-robin
        # so other agents get a chance to speak.
        speaker = None
        force_rr = False
        if len(pair_streak) >= max_consecutive_pair * 2:
            recent = pair_streak[-(max_consecutive_pair * 2):]
            unique = set(recent)
            if len(unique) == 2:
                force_rr = True

        if last_msg and not force_rr:
            # Explicit invite takes priority.
            target = last_msg.invited
            # Implicit invite: if addressed to a specific agent, treat as invite.
            if not target and last_msg.addressed not in ("all", last_msg.speaker):
                target = last_msg.addressed
            if target:
                speaker = next(
                    (a for a in agents
                     if a.name == target and not a.yielded),
                    None)
        # -- Per-agent turn budgets: prefer the agent with the most
        #    remaining budget (fewest turns used relative to budget).
        #    An agent who exhausted their budget can only speak if
        #    explicitly invited (handled above).  This block sits
        #    between invite-check and the round-robin fallback. --
        if speaker is None:
            eligible = [
                a for a in active
                if turns_used[a.name] < turn_budgets[a.name]
                and (last_msg is None or a.name != last_msg.speaker
                     or len(active) == 1)
            ]
            if eligible:
                # Pick the agent with the most remaining budget.
                eligible.sort(
                    key=lambda a: turn_budgets[a.name] - turns_used[a.name],
                    reverse=True)
                speaker = eligible[0]

        # Fallback: plain round-robin (ignoring budgets) so the debate
        # never stalls if every agent is over-budget.
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

        # Hot-swap: unload others, load speaker.
        if mode == "hot-swap" and speaker.backend.is_local:
            for o in agents:
                if o is not speaker and o.backend.is_local:
                    o.backend.unload()
            speaker.backend.load()

        turn += 1
        show_turn_start(turn, speaker.name)

        callback = emit_token if stream else None
        msg = speaker.speak(topic, history, turn, on_token=callback)

        # Detect failed or empty responses and retry.
        _FAIL_MARKERS = ("[EMPTY RESPONSE", "[ERROR ", "[TIMEOUT", "[COMMAND NOT FOUND")
        is_fail = (any(msg.content.startswith(m) for m in _FAIL_MARKERS)
                   or len(msg.content.strip()) < 10)

        if is_fail:
            fail_streak[speaker.name] += 1
            if fail_streak[speaker.name] < max_retries:
                show_turn_end(msg, retry=True)
                turn -= 1          # don't consume a turn for a failed attempt
                continue           # retry same speaker next iteration
            else:
                # Too many failures — force yield this agent.
                speaker.yielded = True
                msg.intent = "yield"
                msg.content = (
                    f"[{speaker.name} auto-yielded after "
                    f"{fail_streak[speaker.name]} consecutive failures]")
                show_turn_end(msg)
        else:
            fail_streak[speaker.name] = 0
            show_turn_end(msg)

        history.append(msg)
        last_msg = msg
        pair_streak.append(speaker.name)
        turns_used[speaker.name] += 1

        # -- Anti-consensus: detect premature convergence --
        if anti_consensus:
            if msg.intent in _CONVERGE_INTENTS:
                consensus_streak += 1
                if msg.intent == "concede":
                    last_conceder = speaker.name
            elif msg.intent in _DIVERGE_INTENTS:
                consensus_streak = 0
                last_conceder = None

            if (consensus_streak >= 3
                    and turn < max_turns * 0.5):
                # Inject a devil's-advocate moderator prompt.
                target_name = last_conceder or speaker.name
                mod_content = (
                    f"The group appears to be converging early. "
                    f"{target_name}, can you identify what is being "
                    f"left unexamined? What assumption is the group "
                    f"making that might be wrong?"
                )
                history.append(Message(
                    speaker="moderator", turn=turn,
                    content=mod_content,
                    intent="question",
                    addressed=target_name,
                    next_action="continue",
                ))
                # Reset streak so we don't re-inject every turn.
                consensus_streak = 0

        # ── Moderator convergence check (dynamic turn control) ──
        if not is_fail:
            round_turn_count += 1
            n_active = len([a for a in agents if not a.yielded])
            if round_turn_count >= n_active and n_active > 0:
                completed_rounds += 1
                round_turn_count = 0
                if moderator_control and completed_rounds >= min_rounds:
                    try:
                        from .display import status as _status
                        _status(f"Moderator evaluating after round {completed_rounds}...")
                        eval_backend = agents[0].backend
                        eval_backend.load()
                        eval_result = eval_backend.generate(
                            "You are a neutral debate moderator. Reply with one word only.",
                            topic, history, "moderator-eval")
                        if "conclude" in eval_result.lower():
                            _status("Moderator: CONCLUDE")
                            break
                        _status("Moderator: CONTINUE")
                    except Exception:
                        pass

        # If speaker yielded, notify remaining agents.
        if speaker.yielded:
            remaining = [a.name for a in agents if not a.yielded]
            if remaining:
                show_yield_notice(speaker.name, remaining)
                history.append(Message(
                    speaker="moderator", turn=turn,
                    content=(f"{speaker.name} has yielded and left the "
                             f"debate. Remaining: {', '.join(remaining)}."),
                    intent="yield", addressed="all",
                    next_action="continue"))
    else:
        show_max_turns(max_turns)

    show_summary(agents)
    _decision_summary(agents, topic, history, stream)
