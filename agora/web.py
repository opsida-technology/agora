"""
agora.web — Dashboard + live debate streaming UI.

    python -m agora.web                          # dashboard on :8420
    python -m agora.web configs/example.yaml     # auto-start debate

Zero-dependency beyond stdlib + PyYAML.
"""
import json
import os
import queue
import socket
import sys
import threading
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs

import yaml

from .protocol import Message, DIRECTIVE_RE
from .agent import Agent
from .backends import make_backend
from .display import estimate_gb
from .save import save_from_bus
from .health import run_health


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


_bus = EventBus()


# ── Debate runner ──────────────────────────────────────────────────────

_debate_thread: threading.Thread | None = None
_debate_stop = threading.Event()


def _run_debate(cfg_path: str):
    bus = _bus
    bus.wait_for_client()

    if _debate_stop.is_set():
        return

    # Health check
    bus.publish("status", {"text": "Running health checks..."})
    checks = run_health(cfg_path, as_json=False)
    bus.publish("health", {"checks": checks})
    failed = [c for c in checks if not c["auth"]]
    if failed:
        names = ", ".join(c["name"] for c in failed)
        bus.publish("status", {"text": f"Warning: {names} failed health check — continuing anyway..."})

    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    topic = cfg["topic"]
    max_turns = cfg.get("max_turns", 20)
    mode = cfg.get("memory_mode", "auto")

    # Orchestration config
    orch = cfg.get("orchestration", {})
    turn_order = orch.get("turn_order", None)     # list of agent names
    phase_plan = orch.get("phase_plan", [])        # [{phase, turns, objective}]
    final_contract = orch.get("final_output_contract", "")

    agents = []
    for a_cfg in cfg["agents"]:
        ac = dict(a_cfg)
        name = ac.pop("name")
        system = ac.pop("system")
        ac.pop("turn_budget", None)
        agents.append(Agent(name=name, system=system, backend=make_backend(ac)))

    est = estimate_gb(agents)
    if mode == "auto":
        mode = "co-resident" if est <= 17 else "hot-swap"

    # Build turn-order rotation from config or agent list order
    if turn_order:
        agent_by_name = {a.name: a for a in agents}
        rr = [agent_by_name[n] for n in turn_order if n in agent_by_name]
        # Append any agents not in turn_order
        for a in agents:
            if a not in rr:
                rr.append(a)
    else:
        rr = list(agents)

    # Parse phase_plan into a lookup: turn_number -> phase objective
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
                           "est_gb": est, "mode": mode})

    if mode == "co-resident":
        for a in agents:
            if a.backend.is_local:
                bus.publish("status", {"text": f"Loading {a.name}..."})
                a.backend.load()

    history, turn, last_msg = [], 0, None
    rr_idx = 0
    last_phase_num = 0

    def on_token(chunk):
        bus.publish("token", {"text": chunk})

    while turn < max_turns and not _debate_stop.is_set():
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

        # Phase transition — inject moderator guidance when phase changes
        next_turn = turn + 1
        phase = _get_phase(next_turn)
        if phase and phase.get("phase", 0) != last_phase_num:
            last_phase_num = phase["phase"]
            objective = phase.get("objective", "").strip()
            if objective:
                phase_msg = Message(
                    speaker="moderator", turn=next_turn,
                    content=(f"[Phase {last_phase_num}] {objective}"),
                    intent="question", addressed="all",
                    next_action="continue")
                history.append(phase_msg)
                bus.publish("status", {"text": f"Phase {last_phase_num}: {objective[:80]}..."})

        # Speaker selection: honour invite, then turn_order rotation
        speaker = None
        if last_msg and last_msg.invited:
            speaker = next(
                (a for a in agents
                 if a.name == last_msg.invited and not a.yielded), None)
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

        turn += 1
        bus.publish("turn_start", {"turn": turn, "name": speaker.name,
                                   "closing": False})

        msg = speaker.speak(topic, history, turn, on_token=on_token)

        bus.publish("turn_end", {
            "turn": turn, "name": msg.speaker, "closing": False,
            "intent": msg.intent, "addressed": msg.addressed,
            "next_action": msg.next_action, "content": msg.content,
            "yielded": speaker.yielded, "invited": msg.invited,
        })
        history.append(msg)
        last_msg = msg

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
    else:
        if not _debate_stop.is_set():
            bus.publish("done", {"reason": f"Max turns ({max_turns}) reached."})

    if _debate_stop.is_set():
        bus.publish("done", {"reason": "Debate stopped by user."})
        # Save partial transcript
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

    # Decision summary — fresh backend, no session contamination
    bus.publish("decision_start", {})
    # Build decision prompt — use final_output_contract if provided
    if final_contract:
        decision_system = (
            "You are a neutral moderator writing the final decision summary. "
            "You must NOT continue the debate or address agents. "
            "Do NOT use Agora directives.\n\n"
            "The debate organizer specified this output contract:\n"
            f"{final_contract}\n\n"
            "Write the decision document following that contract exactly. "
            "Be concise and specific. Write in the language the debate was "
            "conducted in.")
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
            "Write in the language the debate was conducted in.")
    # Use a fresh CLI backend to avoid session bleed
    from .backends import CLIBackend
    decision_backend = CLIBackend(
        command=["claude", "-p"], timeout=180)
    parts: list[str] = []
    for chunk in decision_backend.stream(
            decision_system, topic, history, "moderator"):
        bus.publish("decision_token", {"text": chunk})
        parts.append(chunk)
    raw = "".join(parts)
    content = DIRECTIVE_RE.sub("", raw).strip()
    bus.publish("decision_end", {"content": content})

    # Auto-save transcript
    try:
        out = save_from_bus(bus, cfg_path=cfg_path)
        bus.publish("saved", {"path": str(out)})
    except Exception as e:
        bus.publish("saved", {"error": str(e)})


def start_debate(cfg_path: str) -> bool:
    """Start a debate in background. Returns False if one is already running."""
    global _debate_thread
    if _debate_thread and _debate_thread.is_alive():
        return False
    _bus.reset()
    _debate_stop.clear()
    _debate_thread = threading.Thread(target=_run_debate,
                                      args=(cfg_path,), daemon=True)
    _debate_thread.start()
    return True


def stop_debate():
    """Signal the running debate to stop."""
    _debate_stop.set()


def debate_running() -> bool:
    return _debate_thread is not None and _debate_thread.is_alive()


# ── Config helpers ─────────────────────────────────────────────────────

CONFIGS_DIR = Path(__file__).resolve().parent.parent / "configs"


def list_configs() -> list[dict]:
    configs = []
    for p in sorted(CONFIGS_DIR.glob("*.yaml")):
        try:
            with open(p) as f:
                cfg = yaml.safe_load(f)
            configs.append({
                "name": p.stem,
                "file": p.name,
                "topic": cfg.get("topic", "").strip()[:120],
                "agents": len(cfg.get("agents", [])),
                "max_turns": cfg.get("max_turns", 20),
            })
        except Exception:
            configs.append({"name": p.stem, "file": p.name,
                            "topic": "(parse error)", "agents": 0,
                            "max_turns": 0})
    return configs


def list_debates() -> list[dict]:
    """List saved debate folders with metadata."""
    from .save import DEBATES_DIR
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


# ── HTML pages ─────────────────────────────────────────────────────────

HISTORY_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agora — Past Debates</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --dim: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --yellow: #d29922; --red: #f85149;
    --cyan: #39d353; --purple: #bc8cff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg); color: var(--text);
    max-width: 1100px; margin: 0 auto; padding: 24px 16px;
    line-height: 1.6;
  }
  .back-link { color: var(--dim); font-size: 12px; text-decoration: none; margin-bottom: 16px; display: inline-block; }
  .back-link:hover { color: var(--accent); }
  h1 { color: var(--accent); font-size: 20px; font-weight: 700; margin-bottom: 4px; }
  .subtitle { color: var(--dim); font-size: 13px; margin-bottom: 24px; }

  .debate-list { display: flex; flex-direction: column; gap: 12px; margin-bottom: 24px; }
  .debate-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px; cursor: pointer;
    transition: border-color 0.15s;
  }
  .debate-card:hover { border-color: var(--accent); }
  .debate-card.selected { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
  .debate-card .topic { font-size: 14px; font-weight: 600; color: var(--text); margin-bottom: 4px; }
  .debate-card .meta { font-size: 12px; color: var(--dim); display: flex; gap: 16px; flex-wrap: wrap; }
  .debate-card .meta span { white-space: nowrap; }
  .badge { display: inline-block; padding: 1px 8px; border-radius: 3px; font-size: 10px; font-weight: 600; }
  .badge.green { background: rgba(63,185,80,0.1); color: var(--green); border: 1px solid rgba(63,185,80,0.2); }
  .badge.yellow { background: rgba(210,153,34,0.1); color: var(--yellow); border: 1px solid rgba(210,153,34,0.2); }

  .viewer {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; overflow: hidden; display: none;
  }
  .viewer-tabs {
    display: flex; border-bottom: 1px solid var(--border);
  }
  .viewer-tab {
    padding: 10px 20px; font-size: 12px; color: var(--dim);
    cursor: pointer; border-bottom: 2px solid transparent;
    transition: all 0.15s;
  }
  .viewer-tab:hover { color: var(--text); }
  .viewer-tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .viewer-content {
    padding: 20px; max-height: 70vh; overflow-y: auto;
    font-family: 'SF Mono', 'Cascadia Code', monospace;
    font-size: 13px; line-height: 1.7; white-space: pre-wrap;
    word-wrap: break-word;
  }
  .viewer-content.md { font-family: -apple-system, sans-serif; white-space: normal; }
  .viewer-content.md h1 { font-size: 18px; color: var(--accent); margin: 16px 0 8px; }
  .viewer-content.md h2 { font-size: 15px; color: var(--accent); margin: 14px 0 6px; }
  .viewer-content.md h3 { font-size: 14px; color: var(--cyan); margin: 12px 0 6px; }
  .viewer-content.md p { margin: 6px 0; }
  .viewer-content.md strong { color: var(--text); }
  .viewer-content.md code { background: var(--bg); padding: 2px 5px; border-radius: 3px; font-size: 12px; font-family: monospace; }
  .viewer-content.md pre { background: var(--bg); padding: 10px; border-radius: 4px; overflow-x: auto; margin: 8px 0; }
  .viewer-content.md ul, .viewer-content.md ol { padding-left: 20px; margin: 6px 0; }
  .viewer-content.md li { margin: 3px 0; }
  .viewer-content.md hr { border: none; border-top: 1px solid var(--border); margin: 16px 0; }
  .viewer-content.md blockquote { border-left: 3px solid var(--yellow); padding-left: 12px; color: var(--dim); margin: 8px 0; }
  .viewer-content.md a { color: var(--accent); text-decoration: none; }
  .viewer-content.md table { border-collapse: collapse; width: 100%; margin: 8px 0; }
  .viewer-content.md th, .viewer-content.md td { padding: 6px 10px; border: 1px solid var(--border); font-size: 12px; }
  .viewer-content.md th { background: var(--bg); color: var(--dim); text-align: left; }

  .empty { text-align: center; padding: 60px 20px; color: var(--dim); font-size: 14px; }

  @media (max-width: 768px) {
    body { padding: 12px 8px; }
    h1 { font-size: 17px; }
    .debate-card { padding: 12px; }
    .debate-card .topic { font-size: 13px; }
    .debate-card .meta { font-size: 11px; gap: 10px; }
    .viewer-content { padding: 14px; font-size: 12px; max-height: 60vh; }
    .viewer-tab { padding: 10px 14px; font-size: 11px; }
  }

  @supports (padding-top: env(safe-area-inset-top)) {
    body { padding-top: calc(12px + env(safe-area-inset-top));
           padding-bottom: calc(12px + env(safe-area-inset-bottom)); }
  }
</style>
</head>
<body>
<a class="back-link" href="/">&larr; Dashboard</a>
<div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;">
  <div>
    <h1>Past Debates</h1>
    <div class="subtitle">Saved transcripts and decisions</div>
  </div>
  <div style="display:flex;gap:8px;">
    <button class="btn" id="compareBtn" onclick="toggleCompare()" style="font-size:12px;padding:6px 14px;">Compare</button>
    <button class="btn" id="exportBtn" onclick="exportPDF()" style="font-size:12px;padding:6px 14px;display:none;">Export PDF</button>
  </div>
</div>
<div id="compareBar" style="display:none;padding:10px 14px;background:var(--surface);border:1px solid var(--accent);border-radius:8px;margin-bottom:16px;font-size:12px;color:var(--accent);">
  Select two debates to compare. <button class="btn" onclick="runCompare()" style="font-size:11px;padding:4px 12px;margin-left:8px;" id="runCompareBtn" disabled>Compare Selected</button>
  <button class="btn" onclick="toggleCompare()" style="font-size:11px;padding:4px 12px;margin-left:4px;">Cancel</button>
</div>
<div class="debate-list" id="debateList"></div>
<div class="viewer" id="viewer">
  <div class="viewer-tabs" id="viewerTabs"></div>
  <div class="viewer-content md" id="viewerContent"></div>
</div>
<div id="compareView" style="display:none;">
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
    <div class="viewer" style="display:block;">
      <div class="viewer-tabs" id="compareTabs1"></div>
      <div class="viewer-content md" id="compareContent1" style="max-height:60vh;"></div>
    </div>
    <div class="viewer" style="display:block;">
      <div class="viewer-tabs" id="compareTabs2"></div>
      <div class="viewer-content md" id="compareContent2" style="max-height:60vh;"></div>
    </div>
  </div>
</div>
<script>
let debates = [];
let selectedDir = null;

function md(text) {
  return text
    .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>')
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/^---+$/gm, '<hr>')
    .replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>')
    .replace(/^\|(.+)\|$/gm, (m, row) => {
      const cells = row.split('|').map(c => c.trim());
      return '<tr>' + cells.map(c => c.match(/^-+$/) ? '' : '<td>' + c + '</td>').join('') + '</tr>';
    })
    .replace(/((?:<tr>.*<\/tr>\n?)+)/g, '<table>$1</table>')
    .replace(/^[-*] (.+)$/gm, '<li>$1</li>')
    .replace(/^\d+\. (.+)$/gm, '<li>$1</li>')
    .replace(/((?:<li>.*<\/li>\n?)+)/g, '<ul>$1</ul>')
    .replace(/\n\n+/g, '</p><p>')
    .replace(/\n/g, '<br>');
}

async function loadDebates() {
  const res = await fetch('/api/debates');
  debates = await res.json();
  const list = document.getElementById('debateList');
  if (!debates.length) {
    list.innerHTML = '<div class="empty">No saved debates yet. Run a debate and it will appear here.</div>';
    return;
  }
  list.innerHTML = debates.map(d => {
    const date = d.date ? new Date(d.date).toLocaleString() : d.dir;
    const agents = (d.agents || []).map(a => a.name).join(', ');
    const isSelected = compareMode ? compareSelections.includes(d.dir) : selectedDir === d.dir;
    const clickFn = compareMode ? `selectDebateForCompare('${d.dir}')` : `selectDebate('${d.dir}')`;
    return `
      <div class="debate-card ${isSelected ? 'selected' : ''}"
           onclick="${clickFn}">
        <div class="topic">${d.topic || d.dir}</div>
        <div class="meta">
          <span>${date}</span>
          <span>${d.turns || 0} turns</span>
          ${agents ? '<span>' + agents + '</span>' : ''}
          ${d.has_decision ? '<span class="badge green">decision</span>' : ''}
          ${d.done_reason && d.done_reason.includes('stopped') ? '<span class="badge yellow">partial</span>' : ''}
        </div>
      </div>`;
  }).join('');
}

async function selectDebate(dir) {
  selectedDir = dir;
  loadDebates();
  document.getElementById('compareView').style.display = 'none';
  document.getElementById('exportBtn').style.display = '';
  const d = debates.find(x => x.dir === dir);
  const viewer = document.getElementById('viewer');
  viewer.style.display = 'block';

  // Build tabs
  const tabs = [{label: 'Transcript', file: 'transcript.md'}];
  if (d && d.has_decision) tabs.push({label: 'Decision', file: 'decision.md'});
  if (d && d.has_config) tabs.push({label: 'Config', file: 'config.yaml'});
  tabs.push({label: 'Meta', file: 'meta.json'});

  document.getElementById('viewerTabs').innerHTML = tabs.map((t, i) =>
    `<div class="viewer-tab ${i === 0 ? 'active' : ''}" onclick="loadFile('${dir}', '${t.file}', this)">${t.label}</div>`
  ).join('');

  loadFile(dir, 'transcript.md', document.querySelector('.viewer-tab'));
  viewer.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function loadFile(dir, file, tabEl) {
  document.querySelectorAll('.viewer-tab').forEach(t => t.classList.remove('active'));
  if (tabEl) tabEl.classList.add('active');

  const res = await fetch(`/api/debate-file?dir=${encodeURIComponent(dir)}&file=${encodeURIComponent(file)}`);
  const text = await res.text();
  const content = document.getElementById('viewerContent');

  if (file.endsWith('.md')) {
    content.className = 'viewer-content md';
    content.innerHTML = md(text);
  } else if (file.endsWith('.json')) {
    content.className = 'viewer-content';
    try { content.textContent = JSON.stringify(JSON.parse(text), null, 2); }
    catch { content.textContent = text; }
  } else {
    content.className = 'viewer-content';
    content.textContent = text;
  }
}

// ── Compare mode ──
let compareMode = false;
let compareSelections = [];

function toggleCompare() {
  compareMode = !compareMode;
  compareSelections = [];
  document.getElementById('compareBar').style.display = compareMode ? '' : 'none';
  document.getElementById('compareBtn').textContent = compareMode ? 'Cancel' : 'Compare';
  document.getElementById('compareView').style.display = 'none';
  document.getElementById('viewer').style.display = 'none';
  document.getElementById('runCompareBtn').disabled = true;
  selectedDir = null;
  loadDebates();
}

function selectDebateForCompare(dir) {
  const idx = compareSelections.indexOf(dir);
  if (idx >= 0) { compareSelections.splice(idx, 1); }
  else if (compareSelections.length < 2) { compareSelections.push(dir); }
  document.getElementById('runCompareBtn').disabled = compareSelections.length !== 2;
  loadDebates();
}

async function runCompare() {
  if (compareSelections.length !== 2) return;
  document.getElementById('viewer').style.display = 'none';
  document.getElementById('compareView').style.display = '';

  for (let side = 0; side < 2; side++) {
    const dir = compareSelections[side];
    const d = debates.find(x => x.dir === dir);
    const tabsEl = document.getElementById('compareTabs' + (side + 1));
    const contentEl = document.getElementById('compareContent' + (side + 1));

    const tabs = [{label: 'Transcript', file: 'transcript.md'}];
    if (d && d.has_decision) tabs.push({label: 'Decision', file: 'decision.md'});

    tabsEl.innerHTML = `<div style="padding:8px 12px;font-size:11px;color:var(--cyan);border-bottom:1px solid var(--border)">${d ? d.topic : dir}</div>` +
      tabs.map((t, i) =>
        `<div class="viewer-tab ${i === 0 ? 'active' : ''}" onclick="loadCompareFile('${dir}', '${t.file}', this, ${side + 1})">${t.label}</div>`
      ).join('');

    const res = await fetch(`/api/debate-file?dir=${encodeURIComponent(dir)}&file=transcript.md`);
    contentEl.innerHTML = md(await res.text());
  }
}

async function loadCompareFile(dir, file, tabEl, side) {
  const tabsContainer = tabEl.parentElement;
  tabsContainer.querySelectorAll('.viewer-tab').forEach(t => t.classList.remove('active'));
  tabEl.classList.add('active');
  const res = await fetch(`/api/debate-file?dir=${encodeURIComponent(dir)}&file=${encodeURIComponent(file)}`);
  const text = await res.text();
  const el = document.getElementById('compareContent' + side);
  if (file.endsWith('.md')) { el.className = 'viewer-content md'; el.innerHTML = md(text); }
  else { el.className = 'viewer-content'; el.textContent = text; }
}

// ── PDF Export ──
function exportPDF() {
  if (!selectedDir) return;
  const content = document.getElementById('viewerContent').innerHTML;
  const d = debates.find(x => x.dir === selectedDir);
  const title = d ? d.topic : selectedDir;
  const w = window.open('', '_blank');
  w.document.write(`<!DOCTYPE html><html><head><meta charset="utf-8">
    <title>${title}</title>
    <style>
      body { font-family: -apple-system, sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; color: #222; line-height: 1.7; font-size: 14px; }
      h1 { font-size: 20px; color: #333; border-bottom: 2px solid #eee; padding-bottom: 8px; }
      h2 { font-size: 16px; color: #444; margin-top: 24px; }
      h3 { font-size: 14px; color: #555; }
      code { background: #f4f4f4; padding: 2px 5px; border-radius: 3px; font-size: 12px; }
      pre { background: #f4f4f4; padding: 12px; border-radius: 4px; overflow-x: auto; }
      blockquote { border-left: 3px solid #ddd; padding-left: 12px; color: #666; }
      table { border-collapse: collapse; width: 100%; }
      th, td { padding: 6px 10px; border: 1px solid #ddd; font-size: 12px; }
      th { background: #f8f8f8; }
      hr { border: none; border-top: 1px solid #eee; margin: 20px 0; }
      @media print { body { margin: 20px; } }
    </style>
  </head><body>${content}<script>setTimeout(()=>{window.print();},500)<\/script></body></html>`);
  w.document.close();
}

loadDebates();
</script>
</body>
</html>"""


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agora — Dashboard</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --dim: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --yellow: #d29922; --red: #f85149;
    --cyan: #39d353; --purple: #bc8cff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg); color: var(--text);
    max-width: 1100px; margin: 0 auto; padding: 24px 16px;
    line-height: 1.6;
  }
  h1 { color: var(--accent); font-size: 20px; font-weight: 700;
    letter-spacing: -0.3px; margin-bottom: 4px; }
  .subtitle { color: var(--dim); font-size: 13px; margin-bottom: 24px; }
  .status-bar {
    display: flex; align-items: center; gap: 12px;
    padding: 12px 16px; background: var(--surface);
    border: 1px solid var(--border); border-radius: 8px;
    margin-bottom: 24px; font-size: 13px;
  }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; }
  .status-dot.idle { background: var(--dim); }
  .status-dot.running { background: var(--green); animation: pulse 2s infinite; }
  @keyframes pulse { 50% { opacity: 0.4; } }
  .btn {
    padding: 8px 20px; border: 1px solid var(--border); border-radius: 6px;
    background: var(--surface); color: var(--text); font-size: 13px;
    cursor: pointer; font-weight: 500; transition: all 0.15s;
  }
  .btn:hover { border-color: var(--accent); color: var(--accent); }
  .btn.primary { background: var(--accent); color: #000; border-color: var(--accent); font-weight: 600; }
  .btn.primary:hover { background: #79c0ff; }
  .btn.danger { border-color: var(--red); color: var(--red); }
  .btn.danger:hover { background: var(--red); color: #fff; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }

  /* Config cards */
  .configs-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; margin-bottom: 24px; }
  .config-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px; cursor: pointer; transition: all 0.15s;
  }
  .config-card:hover { border-color: var(--accent); }
  .config-card.selected { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
  .config-card .name { color: var(--cyan); font-weight: 600; font-size: 14px; margin-bottom: 4px; }
  .config-card .topic { color: var(--dim); font-size: 12px; margin-bottom: 8px;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .config-card .meta { font-size: 11px; color: var(--dim); }
  .config-card .meta span { margin-right: 12px; }

  /* Editor */
  .editor-section {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; margin-bottom: 24px; overflow: hidden;
  }
  .editor-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 16px; border-bottom: 1px solid var(--border);
  }
  .editor-header h2 { font-size: 13px; color: var(--dim);
    text-transform: uppercase; letter-spacing: 0.8px; }
  .tab-bar { display: flex; gap: 0; }
  .tab {
    padding: 6px 16px; font-size: 12px; border: 1px solid var(--border);
    background: var(--bg); color: var(--dim); cursor: pointer;
    border-right: none; transition: all 0.15s;
  }
  .tab:first-child { border-radius: 4px 0 0 4px; }
  .tab:last-child { border-radius: 0 4px 4px 0; border-right: 1px solid var(--border); }
  .tab.active { background: var(--accent); color: #000; border-color: var(--accent); font-weight: 600; }
  .editor-body { position: relative; }
  #yamlText {
    width: 100%; min-height: 400px; padding: 16px;
    background: var(--bg); color: var(--text); border: none;
    font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
    font-size: 13px; line-height: 1.6; resize: vertical; outline: none;
    tab-size: 2;
  }
  /* Form editor */
  #formEditor { padding: 16px; display: none; }
  .form-group { margin-bottom: 16px; }
  .form-group label { display: block; font-size: 12px; color: var(--dim);
    text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
  .form-input {
    width: 100%; padding: 8px 12px; background: var(--bg);
    border: 1px solid var(--border); border-radius: 4px;
    color: var(--text); font-size: 13px; outline: none;
  }
  .form-input:focus { border-color: var(--accent); }
  textarea.form-input { min-height: 80px; font-family: inherit; resize: vertical; }
  textarea.system-prompt { min-height: 120px; }
  .agent-form {
    border: 1px solid var(--border); border-radius: 6px;
    padding: 12px; margin-bottom: 12px; background: var(--bg);
  }
  .agent-form .agent-header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 8px;
  }
  .agent-form .agent-name { color: var(--cyan); font-weight: 600; font-size: 13px; }
  .agent-form .form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 8px; }
  .remove-btn { background: none; border: none; color: var(--red); cursor: pointer; font-size: 16px; }

  /* Actions bar */
  .actions-bar { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  .actions-bar .spacer { flex: 1; }
  .save-msg { font-size: 12px; color: var(--green); transition: opacity 0.3s; }

  /* Toast */
  .toast {
    position: fixed; bottom: 24px; right: 24px; padding: 12px 20px;
    background: var(--surface); border: 1px solid var(--green);
    border-radius: 8px; color: var(--green); font-size: 13px;
    opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 999;
  }
  .toast.show { opacity: 1; }

  @media (max-width: 768px) {
    body { padding: 12px 8px; }
    h1 { font-size: 17px; }
    .subtitle { font-size: 12px; margin-bottom: 16px; }
    .status-bar {
      flex-wrap: wrap; gap: 8px; padding: 10px 12px; font-size: 12px;
    }
    .status-bar .btn { padding: 8px 14px; font-size: 12px; flex: 1; min-width: 0; text-align: center; }
    #statusText { flex: 1 1 100%; order: -1; }
    .configs-grid { grid-template-columns: 1fr; gap: 8px; margin-bottom: 16px; }
    .config-card { padding: 12px; }
    .config-card .name { font-size: 13px; }
    .editor-section { margin-bottom: 16px; }
    .editor-header { flex-direction: column; gap: 8px; padding: 10px 12px; }
    #yamlText { min-height: 250px; padding: 12px; font-size: 12px; }
    #formEditor { padding: 12px; }
    .agent-form .form-row { grid-template-columns: 1fr; }
    .actions-bar { flex-direction: column; align-items: stretch; gap: 8px; }
    .actions-bar .spacer { display: none; }
    .actions-bar .btn { width: 100%; text-align: center; }
    .btn { padding: 10px 16px; font-size: 13px; }
    .tab { padding: 8px 14px; }
    .form-group label { font-size: 11px; }
    .form-input { font-size: 14px; padding: 10px 12px; }
    textarea.form-input { min-height: 60px; }
    textarea.system-prompt { min-height: 150px !important; font-size: 13px; }
    .toast { bottom: 12px; right: 12px; left: 12px; text-align: center; }
  }

  /* Safe area for notched phones */
  @supports (padding-top: env(safe-area-inset-top)) {
    body { padding-top: calc(12px + env(safe-area-inset-top));
           padding-bottom: calc(12px + env(safe-area-inset-bottom));
           padding-left: calc(8px + env(safe-area-inset-left));
           padding-right: calc(8px + env(safe-area-inset-right)); }
  }

  /* Touch targets */
  @media (pointer: coarse) {
    .btn { min-height: 44px; }
    .config-card { min-height: 44px; }
    .tab { min-height: 40px; display: flex; align-items: center; justify-content: center; }
    .form-input, select.form-input { min-height: 44px; }
    .remove-btn { min-width: 44px; min-height: 44px; font-size: 20px;
      display: flex; align-items: center; justify-content: center; }
  }
</style>
</head>
<body>
<h1>Agora Protocol</h1>
<div class="subtitle">Multi-agent debate platform</div>

<div class="status-bar">
  <div class="status-dot" id="statusDot"></div>
  <span id="statusText">Idle</span>
  <div style="flex:1"></div>
  <a class="btn" href="/history" style="text-decoration:none">History</a>
  <button class="btn primary" id="startBtn" onclick="startDebate()">Start Debate</button>
  <button class="btn danger" id="stopBtn" onclick="stopDebate()" style="display:none">Stop</button>
  <a class="btn" id="viewBtn" href="/debate" target="_blank" style="display:none;text-decoration:none">View Debate</a>
</div>

<div class="configs-grid" id="configsGrid"></div>

<div class="editor-section">
  <div class="editor-header">
    <h2 id="editorTitle">Select a config or paste YAML</h2>
    <div class="tab-bar">
      <div class="tab active" onclick="switchTab('text')">YAML</div>
      <div class="tab" onclick="switchTab('form')">Editor</div>
    </div>
  </div>
  <div class="editor-body">
    <textarea id="yamlText" spellcheck="false" placeholder="# Paste or edit your debate config YAML here..."></textarea>
    <div id="formEditor"></div>
  </div>
</div>

<div class="actions-bar">
  <button class="btn" onclick="newConfig()">New Config</button>
  <button class="btn" onclick="saveConfig()">Save</button>
  <span class="save-msg" id="saveMsg"></span>
  <div class="spacer"></div>
  <button class="btn" onclick="copyConfig()">Copy YAML</button>
  <button class="btn" onclick="pasteConfig()">Paste</button>
</div>

<div class="toast" id="toast"></div>

<script>
let selectedConfig = null;
let configs = [];

function toast(msg, color) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.borderColor = color || 'var(--green)';
  el.style.color = color || 'var(--green)';
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2500);
}

// ── Config loading ──
async function loadConfigs() {
  const res = await fetch('/api/configs');
  configs = await res.json();
  const grid = document.getElementById('configsGrid');
  grid.innerHTML = configs.map(c => `
    <div class="config-card ${selectedConfig === c.file ? 'selected' : ''}"
         onclick="selectConfig('${c.file}')">
      <div style="display:flex;justify-content:space-between;align-items:start;">
        <div class="name">${c.name}</div>
        <button onclick="event.stopPropagation();deleteConfig('${c.file}')" style="background:none;border:none;color:var(--dim);cursor:pointer;font-size:14px;padding:0 4px;line-height:1;" title="Delete">&times;</button>
      </div>
      <div class="topic">${c.topic}</div>
      <div class="meta">
        <span>${c.agents} agents</span>
        <span>${c.max_turns} turns</span>
      </div>
    </div>`).join('');
}

async function selectConfig(file) {
  selectedConfig = file;
  const res = await fetch('/api/config?file=' + encodeURIComponent(file));
  const text = await res.text();
  document.getElementById('yamlText').value = text;
  document.getElementById('editorTitle').textContent = file;
  loadConfigs();  // re-render to show selection
  syncFormFromYaml();
}

// ── Tab switching ──
function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  if (tab === 'text') {
    document.querySelectorAll('.tab')[0].classList.add('active');
    document.getElementById('yamlText').style.display = 'block';
    document.getElementById('formEditor').style.display = 'none';
    syncYamlFromForm();
  } else {
    document.querySelectorAll('.tab')[1].classList.add('active');
    document.getElementById('yamlText').style.display = 'none';
    document.getElementById('formEditor').style.display = 'block';
    syncFormFromYaml();
  }
}

// ── Form <-> YAML sync ──
function syncFormFromYaml() {
  const text = document.getElementById('yamlText').value;
  const editor = document.getElementById('formEditor');
  try {
    // Simple YAML parse (we send to server for reliable parsing)
    fetch('/api/parse-yaml', {
      method: 'POST',
      headers: {'Content-Type': 'text/plain'},
      body: text
    }).then(r => r.json()).then(cfg => {
      if (cfg.error) { editor.innerHTML = `<p style="color:var(--red)">${cfg.error}</p>`; return; }
      renderForm(cfg);
    });
  } catch(e) {
    editor.innerHTML = `<p style="color:var(--red)">Parse error</p>`;
  }
}

function renderForm(cfg) {
  const editor = document.getElementById('formEditor');
  const agents = cfg.agents || [];
  let html = `
    <div class="form-group">
      <label>Topic</label>
      <textarea class="form-input" id="fTopic" oninput="formChanged()">${cfg.topic || ''}</textarea>
    </div>
    <div class="form-group" style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
      <div>
        <label>Max Turns</label>
        <input class="form-input" type="number" id="fMaxTurns" value="${cfg.max_turns || 20}" oninput="formChanged()">
      </div>
      <div>
        <label>Memory Mode</label>
        <select class="form-input" id="fMemMode" onchange="formChanged()">
          <option value="auto" ${(cfg.memory_mode||'auto')==='auto'?'selected':''}>auto</option>
          <option value="co-resident" ${cfg.memory_mode==='co-resident'?'selected':''}>co-resident</option>
          <option value="hot-swap" ${cfg.memory_mode==='hot-swap'?'selected':''}>hot-swap</option>
        </select>
      </div>
    </div>
    <div class="form-group">
      <label>Agents</label>
      <div id="agentsList">
  `;
  agents.forEach((a, i) => {
    html += renderAgentForm(a, i);
  });
  html += `
      </div>
      <button class="btn" onclick="addAgent()" style="margin-top:8px">+ Add Agent</button>
    </div>`;
  editor.innerHTML = html;
}

const CLI_PRESETS = [
  { label: 'Claude', cmd: 'claude -p', desc: 'Anthropic Claude CLI' },
  { label: 'Claude + Web', cmd: 'claude -p --allowedTools WebSearch,WebFetch', desc: 'Claude with web search' },
  { label: 'Gemini', cmd: 'gemini -p', desc: 'Google Gemini CLI' },
  { label: 'Codex', cmd: 'codex exec', desc: 'OpenAI Codex CLI' },
  { label: 'Custom', cmd: '', desc: 'Enter your own command' },
];

function renderAgentForm(a, i) {
  const cmdStr = Array.isArray(a.command) ? a.command.join(' ') : (a.command || '');
  const presetOptions = CLI_PRESETS.map(p =>
    `<option value="${p.cmd}" ${cmdStr === p.cmd ? 'selected' : ''}>${p.label}</option>`
  ).join('');
  const isCustomCmd = !CLI_PRESETS.some(p => p.cmd === cmdStr);
  return `
    <div class="agent-form" id="agent-${i}">
      <div class="agent-header">
        <span class="agent-name">${a.name || 'new-agent'}</span>
        <button class="remove-btn" onclick="removeAgent(${i})">&#x2715;</button>
      </div>
      <div class="form-row">
        <div><label>Name</label><input class="form-input" data-field="name" value="${a.name || ''}" oninput="formChanged()"></div>
        <div><label>Backend</label>
          <select class="form-input" data-field="backend" onchange="onBackendChange(this, ${i}); formChanged()">
            <option value="cli" ${a.backend==='cli'?'selected':''}>cli (Claude/Gemini/Codex)</option>
            <option value="mlx" ${a.backend==='mlx'?'selected':''}>mlx (Apple Silicon local)</option>
            <option value="ollama" ${a.backend==='ollama'?'selected':''}>ollama (local HTTP)</option>
          </select>
        </div>
      </div>
      <div class="form-row" id="cmd-row-${i}">
        <div>
          <label>CLI Tool</label>
          <select class="form-input" data-field="preset" onchange="onPresetChange(this, ${i})">
            ${presetOptions}
            ${isCustomCmd ? `<option value="${cmdStr}" selected>Custom</option>` : ''}
          </select>
        </div>
        <div><label>Timeout (s)</label><input class="form-input" type="number" data-field="timeout" value="${a.timeout || 120}" oninput="formChanged()"></div>
      </div>
      <div id="custom-cmd-${i}" style="${isCustomCmd ? '' : 'display:none'}">
        <label>Custom Command</label>
        <input class="form-input" data-field="command" value="${cmdStr}" oninput="formChanged()" placeholder="e.g. my-cli --flag">
      </div>
      <div id="model-field-${i}" style="${a.backend !== 'cli' ? '' : 'display:none'}">
        <label>Model</label>
        <input class="form-input" data-field="model" value="${a.model || ''}" oninput="formChanged()" placeholder="${a.backend === 'mlx' ? 'e.g. mlx-community/Qwen2.5-7B-4bit' : 'e.g. qwen3:8b'}">
      </div>
      <div><label>System Prompt</label><textarea class="form-input system-prompt" data-field="system" oninput="formChanged()">${a.system || ''}</textarea></div>
    </div>`;
}

function onBackendChange(sel, i) {
  const backend = sel.value;
  const cmdRow = document.getElementById('cmd-row-' + i);
  const modelField = document.getElementById('model-field-' + i);
  if (backend === 'cli') {
    cmdRow.style.display = '';
    modelField.style.display = 'none';
  } else {
    cmdRow.style.display = 'none';
    modelField.style.display = '';
  }
}

function onPresetChange(sel, i) {
  const customDiv = document.getElementById('custom-cmd-' + i);
  const cmdInput = customDiv.querySelector('[data-field="command"]');
  if (sel.value === '') {
    customDiv.style.display = '';
    cmdInput.value = '';
    cmdInput.focus();
  } else {
    customDiv.style.display = 'none';
    cmdInput.value = sel.value;
  }
  formChanged();
}

function addAgent() {
  const list = document.getElementById('agentsList');
  const i = list.children.length;
  list.insertAdjacentHTML('beforeend', renderAgentForm({
    name: 'new-agent', backend: 'cli', command: ['claude', '-p'],
    timeout: 120, system: ''
  }, i));
  formChanged();
}

function removeAgent(i) {
  document.getElementById(`agent-${i}`).remove();
  formChanged();
}

function formChanged() {
  // Rebuild YAML from form
  const topic = document.getElementById('fTopic')?.value || '';
  const maxTurns = parseInt(document.getElementById('fMaxTurns')?.value || '20');
  const memMode = document.getElementById('fMemMode')?.value || 'auto';

  const agentForms = document.querySelectorAll('.agent-form');
  const agents = [];
  agentForms.forEach(af => {
    const get = (f) => af.querySelector(`[data-field="${f}"]`)?.value || '';
    const backend = get('backend');
    const preset = af.querySelector('[data-field="preset"]')?.value || '';
    const customCmd = get('command');
    const cmdRaw = backend === 'cli' ? (preset || customCmd) : '';
    const cmdParts = cmdRaw.split(/\s+/).filter(Boolean);
    const agent = {
      name: get('name'),
      backend: backend,
      timeout: parseInt(get('timeout') || '120'),
      system: get('system'),
    };
    if (backend === 'cli') agent.command = cmdParts;
    else agent.model = get('model');
    agents.push(agent);
  });

  // Update agent name labels
  agentForms.forEach(af => {
    const name = af.querySelector('[data-field="name"]')?.value || 'agent';
    af.querySelector('.agent-name').textContent = name;
  });

  // Send to server for proper YAML serialization
  fetch('/api/to-yaml', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ topic, max_turns: maxTurns, memory_mode: memMode, agents })
  }).then(r => r.text()).then(yaml => {
    document.getElementById('yamlText').value = yaml;
  });
}

// ── Save / New / Paste ──
async function saveConfig() {
  const text = document.getElementById('yamlText').value;
  if (!text.trim()) { toast('Nothing to save', 'var(--red)'); return; }
  let name = selectedConfig;
  if (!name) {
    name = prompt('Config filename (without .yaml):');
    if (!name) return;
    name = name.replace(/\.yaml$/, '') + '.yaml';
    selectedConfig = name;
  }
  const res = await fetch('/api/config?file=' + encodeURIComponent(name), {
    method: 'POST',
    headers: {'Content-Type': 'text/plain'},
    body: text
  });
  if (res.ok) {
    toast('Saved ' + name);
    loadConfigs();
    document.getElementById('editorTitle').textContent = name;
  } else {
    toast('Save failed', 'var(--red)');
  }
}

function newConfig() {
  selectedConfig = null;
  document.getElementById('yamlText').value = [
    '# Debate Config',
    '# CLI options: claude, gemini, codex',
    '# Backend types: cli, mlx, ollama, api',
    '',
    'topic: |',
    '  Your debate topic here — be specific and debatable.',
    '',
    'max_turns: 20',
    'memory_mode: auto          # auto | co-resident | hot-swap',
    '# anti_consensus: true     # devil advocate on early convergence',
    '',
    '# ── Orchestration (optional) ──',
    '# orchestration:',
    '#   turn_order: [advocate, researcher, critic]',
    '#   phase_plan:',
    '#     - phase: 1',
    '#       turns: 1-6',
    '#       objective: Establish positions',
    '#     - phase: 2',
    '#       turns: 7-14',
    '#       objective: Challenge and compare',
    '#     - phase: 3',
    '#       turns: 15-20',
    '#       objective: Converge toward recommendation',
    '#   final_output_contract: |',
    '#     1. Recommended path',
    '#     2. What not to do now',
    '#     3. Conditions for revisiting',
    '#     4. 90-day action plan',
    '',
    'agents:',
    '',
    '  # ── Claude (strongest reasoning, has web search) ──',
    '  - name: advocate',
    '    backend: cli',
    '    command: ["claude", "-p", "--allowedTools", "WebSearch,WebFetch"]',
    '    timeout: 180',
    '    # turn_budget: 8',
    '    system: |',
    '      You argue FOR the proposal. Be specific, use evidence,',
    '      cite sources via web search. 5-7 sentences per turn.',
    '',
    '  # ── Gemini (literature, evidence, web search built-in) ──',
    '  - name: researcher',
    '    backend: cli',
    '    command: ["gemini", "--prompt"]',
    '    timeout: 180',
    '    system: |',
    '      You are the evidence authority. Search for papers,',
    '      benchmarks, and data. Challenge unsupported claims.',
    '      5-7 sentences per turn.',
    '',
    '  # ── Codex (concise, execution-focused) ──',
    '  - name: critic',
    '    backend: cli',
    '    command: ["codex", "exec"]',
    '    timeout: 180',
    '    system: |',
    '      You find weaknesses. One concrete issue per turn.',
    '      Quote the exact claim you are challenging.',
    '      4-6 sentences per turn.',
    '',
    '  # ── More CLI options ──',
    '  # Claude without web:  ["claude", "-p"]',
    '  # API backend:         backend: api, model: ..., base_url: http://localhost:8000/v1',
    '  # MLX (offline):       backend: mlx, model: mlx-community/Qwen3.5-9B-MLX-4bit',
    '  # Ollama:              backend: ollama, model: qwen3:8b',
    ''
  ].join('\n');
  document.getElementById('editorTitle').textContent = 'New config';
  loadConfigs();
  switchTab('text');
}

async function deleteConfig(file) {
  if (!confirm('Delete ' + file + '?')) return;
  const res = await fetch('/api/config?file=' + encodeURIComponent(file), {method: 'DELETE'});
  if (res.ok) {
    toast('Deleted ' + file);
    if (selectedConfig === file) {
      selectedConfig = null;
      document.getElementById('yamlText').value = '';
      document.getElementById('editorTitle').textContent = 'Select a config or paste YAML';
    }
    loadConfigs();
  } else { toast('Delete failed', 'var(--red)'); }
}

async function copyConfig() {
  const text = document.getElementById('yamlText').value;
  if (!text.trim()) { toast('Nothing to copy', 'var(--red)'); return; }
  try {
    await navigator.clipboard.writeText(text);
    toast('YAML copied to clipboard');
  } catch(e) {
    // Fallback for mobile/insecure context
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    toast('YAML copied');
  }
}

async function pasteConfig() {
  try {
    const text = await navigator.clipboard.readText();
    document.getElementById('yamlText').value = text;
    selectedConfig = null;
    document.getElementById('editorTitle').textContent = 'Pasted config';
    syncFormFromYaml();
    toast('Pasted from clipboard');
  } catch(e) {
    toast('Clipboard access denied', 'var(--red)');
  }
}

// ── Start / Stop debate ──
async function startDebate() {
  const text = document.getElementById('yamlText').value;
  if (!text.trim()) { toast('No config loaded', 'var(--red)'); return; }

  // Save to temp or selected
  let file = selectedConfig || '_temp.yaml';
  await fetch('/api/config?file=' + encodeURIComponent(file), {
    method: 'POST',
    headers: {'Content-Type': 'text/plain'},
    body: text
  });

  const res = await fetch('/api/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({config: file})
  });
  const result = await res.json();
  if (result.ok) {
    toast('Debate started');
    updateStatus();
    window.open('/debate', '_blank');
  } else {
    toast(result.error || 'Failed to start', 'var(--red)');
  }
}

async function stopDebate() {
  await fetch('/api/stop', {method: 'POST'});
  toast('Debate stopped');
  setTimeout(updateStatus, 500);
}

async function updateStatus() {
  const res = await fetch('/api/status');
  const st = await res.json();
  const dot = document.getElementById('statusDot');
  const text = document.getElementById('statusText');
  const startBtn = document.getElementById('startBtn');
  const stopBtn = document.getElementById('stopBtn');
  const viewBtn = document.getElementById('viewBtn');
  if (st.running) {
    dot.className = 'status-dot running';
    text.textContent = 'Debate running' + (st.config ? ': ' + st.config : '');
    startBtn.disabled = true;
    stopBtn.style.display = '';
    viewBtn.style.display = '';
  } else {
    dot.className = 'status-dot idle';
    text.textContent = 'Idle';
    startBtn.disabled = false;
    stopBtn.style.display = 'none';
    viewBtn.style.display = 'none';
  }
}

// Init
loadConfigs();
updateStatus();
setInterval(updateStatus, 5000);
</script>
</body>
</html>"""


DEBATE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agora Debate</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --dim: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --yellow: #d29922; --red: #f85149;
    --cyan: #39d353; --purple: #bc8cff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
    background: var(--bg); color: var(--text);
    max-width: 900px; margin: 0 auto; padding: 24px 16px;
    line-height: 1.6;
  }
  .back-link { color: var(--dim); font-size: 12px; text-decoration: none; margin-bottom: 16px; display: inline-block; }
  .back-link:hover { color: var(--accent); }
  #header {
    border: 1px solid var(--accent); border-radius: 8px;
    padding: 20px 24px; margin-bottom: 24px;
    background: var(--surface);
  }
  #header h1 { color: var(--accent); font-size: 14px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 2px; margin-bottom: 8px; }
  #topic { font-size: 16px; font-weight: 400; margin-bottom: 12px; }
  #agents-bar { display: flex; gap: 12px; flex-wrap: wrap; }
  .agent-chip {
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 4px; padding: 4px 10px; font-size: 12px;
  }
  .agent-chip .name { color: var(--cyan); font-weight: 600; }
  .agent-chip .model { color: var(--dim); margin-left: 6px; }
  #meta { color: var(--dim); font-size: 12px; margin-top: 8px; }
  .turn {
    border-left: 3px solid var(--border); margin-bottom: 16px;
    padding: 12px 16px; background: var(--surface);
    border-radius: 0 6px 6px 0;
  }
  .turn.active { border-left-color: var(--accent); }
  .turn.closing { border-left-color: var(--yellow); }
  .turn-header {
    display: flex; align-items: center; gap: 10px;
    margin-bottom: 8px; font-size: 13px;
  }
  .turn-num { color: var(--dim); }
  .turn-name { color: var(--cyan); font-weight: 600; }
  .turn-label { color: var(--yellow); font-size: 11px; font-weight: 600;
    border: 1px solid var(--yellow); border-radius: 3px; padding: 1px 6px; }
  .turn-content {
    font-size: 14px; white-space: pre-wrap; word-wrap: break-word;
    min-height: 1.6em;
  }
  .turn-content.md { white-space: normal; }
  .turn-content.md h1,.turn-content.md h2,.turn-content.md h3 {
    color: var(--accent); margin: 12px 0 6px; font-size: 15px; }
  .turn-content.md h2 { font-size: 14px; }
  .turn-content.md h3 { font-size: 13px; }
  .turn-content.md p { margin: 6px 0; }
  .turn-content.md strong { color: var(--text); }
  .turn-content.md a { color: var(--accent); text-decoration: none; }
  .turn-content.md a:hover { text-decoration: underline; }
  .turn-content.md code {
    background: var(--bg); padding: 2px 5px; border-radius: 3px;
    font-size: 13px; }
  .turn-content.md pre { background: var(--bg); padding: 10px;
    border-radius: 4px; overflow-x: auto; margin: 8px 0; }
  .turn-content.md pre code { padding: 0; }
  .turn-content.md ul,.turn-content.md ol { padding-left: 20px; margin: 6px 0; }
  .turn-content.md li { margin: 3px 0; }
  .turn-content.md hr { border: none; border-top: 1px solid var(--border); margin: 12px 0; }
  .turn-content .cursor {
    display: inline-block; width: 7px; height: 16px;
    background: var(--accent); vertical-align: text-bottom;
    animation: blink .6s step-end infinite;
  }
  @keyframes blink { 50% { opacity: 0; } }
  .directives {
    display: flex; gap: 8px; flex-wrap: wrap;
    margin-top: 10px; font-size: 11px;
  }
  .dir-badge {
    border-radius: 3px; padding: 2px 8px;
    border: 1px solid var(--border);
  }
  .dir-badge.intent { border-color: var(--green); color: var(--green); }
  .dir-badge.addressed { border-color: var(--purple); color: var(--purple); }
  .dir-badge.next { border-color: var(--yellow); color: var(--yellow); }
  .dir-badge.yielded { border-color: var(--red); color: var(--red); }
  .dir-badge.invited { border-color: var(--accent); color: var(--accent); }
  .dir-badge.closing { border-color: var(--yellow); color: var(--yellow); }
  .yield-notice {
    border: 1px solid var(--yellow); border-radius: 6px;
    padding: 10px 16px; margin-bottom: 16px;
    background: rgba(210,153,34,0.08); font-size: 13px;
  }
  .yield-notice .name { color: var(--yellow); font-weight: 600; }
  #summary {
    border: 1px solid var(--border); border-radius: 8px;
    padding: 16px; background: var(--surface);
    margin-top: 24px; display: none;
  }
  #summary h2 { font-size: 13px; color: var(--dim);
    text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }
  #summary table { width: 100%; border-collapse: collapse; font-size: 13px; }
  #summary th { text-align: left; color: var(--dim); font-weight: 400;
    padding: 4px 8px; border-bottom: 1px solid var(--border); }
  #summary td { padding: 6px 8px; }
  #summary .agent-name { color: var(--cyan); }
  #decision {
    border: 1px solid var(--green); border-radius: 8px;
    padding: 20px 24px; background: var(--surface);
    margin-top: 24px; display: none;
  }
  #decision h2 { color: var(--green); font-size: 14px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 2px; margin-bottom: 12px; }
  #decision-content { font-size: 14px; }
  #decision-content h1,#decision-content h2,#decision-content h3 {
    color: var(--accent); margin: 14px 0 6px; font-size: 15px; }
  #decision-content h2 { font-size: 14px; }
  #decision-content p { margin: 6px 0; }
  #decision-content strong { color: var(--text); }
  #decision-content a { color: var(--accent); }
  #decision-content ul,#decision-content ol { padding-left: 20px; margin: 6px 0; }
  #decision-content li { margin: 3px 0; }
  #decision-content code { background: var(--bg); padding: 2px 5px;
    border-radius: 3px; font-size: 13px; }
  #status {
    color: var(--dim); font-size: 12px; text-align: center;
    padding: 40px 0;
  }
  #status .spinner {
    display: inline-block; width: 12px; height: 12px;
    border: 2px solid var(--border); border-top-color: var(--accent);
    border-radius: 50%; animation: spin .8s linear infinite;
    margin-right: 8px; vertical-align: middle;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  #debate-chart { margin-top: 24px; }
  #debate-chart h2 { font-size: 13px; color: var(--dim);
    text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }

  @media (max-width: 768px) {
    body { padding: 12px 8px; }
    #header { padding: 14px 12px; margin-bottom: 16px; }
    #header h1 { font-size: 12px; letter-spacing: 1px; }
    #topic { font-size: 14px; }
    #agents-bar { gap: 6px; }
    .agent-chip { padding: 3px 8px; font-size: 11px; }
    .turn { padding: 10px 12px; margin-bottom: 12px; }
    .turn-header { font-size: 12px; gap: 6px; }
    .turn-content { font-size: 13px; }
    .turn-content.md h1,.turn-content.md h2,.turn-content.md h3 { font-size: 14px; }
    .turn-content.md pre { padding: 8px; font-size: 11px; }
    .directives { gap: 4px; font-size: 10px; }
    .dir-badge { padding: 2px 6px; }
    .yield-notice { padding: 8px 12px; font-size: 12px; }
    #summary { padding: 12px; }
    #summary table { font-size: 12px; }
    #decision { padding: 14px 12px; }
    #decision-content { font-size: 13px; }
    #debate-chart canvas { height: 200px !important; }
    .back-link { font-size: 11px; margin-bottom: 10px; }
  }

  @supports (padding-top: env(safe-area-inset-top)) {
    body { padding-top: calc(12px + env(safe-area-inset-top));
           padding-bottom: calc(12px + env(safe-area-inset-bottom)); }
  }
</style>
</head>
<body>
<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
  <a class="back-link" href="/" style="margin:0">&larr; Dashboard</a>
  <button id="debateStopBtn" onclick="fetch('/api/stop',{method:'POST'}).then(()=>{this.textContent='Stopped';this.disabled=true;})" style="padding:6px 16px;border:1px solid var(--red);border-radius:4px;background:none;color:var(--red);font-size:12px;cursor:pointer;display:none;">Stop Debate</button>
</div>
<div id="header" style="display:none">
  <h1>Agora Debate</h1>
  <div id="topic"></div>
  <div id="agents-bar"></div>
  <div id="meta"></div>
</div>
<div id="status"><span class="spinner"></span>Connecting to debate...</div>
<div id="turns"></div>
<div id="debate-chart" style="display:none">
  <h2>Interaction Map</h2>
  <canvas id="chartCanvas" style="width:100%;background:var(--surface);border:1px solid var(--border);border-radius:8px;"></canvas>
</div>
<div id="summary"><h2>Summary</h2><table id="summary-table"></table></div>
<div id="decision"><h2>Decision Summary</h2><div id="decision-content"></div></div>
<script>
const $ = s => document.querySelector(s);
let currentTurnEl = null;
let currentContentEl = null;
let userScrolledUp = false;
let debateEdges = [];
let agentSet = [];

window.addEventListener('scroll', () => {
  const atBottom = (window.innerHeight + window.scrollY) >= (document.body.offsetHeight - 80);
  userScrolledUp = !atBottom;
});

const AGENT_COLORS = ['#58a6ff','#3fb950','#d29922','#f85149','#bc8cff','#39d353','#f0883e','#8b949e'];

function drawChart() {
  const canvas = $('#chartCanvas');
  if (!canvas || agentSet.length < 2 || debateEdges.length < 1) return;
  $('#debate-chart').style.display = 'block';
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = Math.max(300, agentSet.length * 50 + 60);
  canvas.width = w * dpr; canvas.height = h * dpr;
  canvas.style.height = h + 'px';
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr); ctx.clearRect(0, 0, w, h);
  const padL = 130, padR = 30, padT = 30, padB = 30;
  const plotW = w - padL - padR, plotH = h - padT - padB;
  const maxTurn = Math.max(...debateEdges.map(e => e.turn), 1);
  const xStep = plotW / Math.max(maxTurn, 1);
  agentSet.forEach((name, i) => {
    const y = padT + (plotH / (agentSet.length - 1 || 1)) * i;
    const col = AGENT_COLORS[i % AGENT_COLORS.length];
    ctx.strokeStyle = col + '22'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.fillStyle = col; ctx.font = '11px monospace';
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    ctx.fillText(name, padL - 10, y);
    ctx.beginPath(); ctx.arc(padL - 4, y, 3, 0, Math.PI * 2); ctx.fill();
  });
  ctx.fillStyle = '#555568'; ctx.font = '9px monospace'; ctx.textAlign = 'center';
  for (let t = 1; t <= maxTurn; t++) ctx.fillText(t, padL + (t - 0.5) * xStep, padT - 12);
  debateEdges.forEach(edge => {
    const fi = agentSet.indexOf(edge.from), ti = agentSet.indexOf(edge.to);
    if (fi < 0 || ti < 0) return;
    const x = padL + (edge.turn - 0.5) * xStep;
    const y1 = padT + (plotH / (agentSet.length - 1 || 1)) * fi;
    const y2 = padT + (plotH / (agentSet.length - 1 || 1)) * ti;
    const col = AGENT_COLORS[fi % AGENT_COLORS.length];
    const isBroadcast = edge.broadcast;
    ctx.strokeStyle = col + (isBroadcast ? '44' : 'aa');
    ctx.lineWidth = isBroadcast ? 1 : 2;
    ctx.beginPath(); ctx.moveTo(x, y1);
    const cpx = x + (fi === ti ? 20 : 0), cpy = (y1 + y2) / 2;
    ctx.quadraticCurveTo(cpx + 15, cpy, x, y2); ctx.stroke();
    const angle = Math.atan2(y2 - cpy, x - cpx - 15);
    ctx.fillStyle = col + 'aa'; ctx.beginPath(); ctx.moveTo(x, y2);
    ctx.lineTo(x - 6 * Math.cos(angle - 0.4), y2 - 6 * Math.sin(angle - 0.4));
    ctx.lineTo(x - 6 * Math.cos(angle + 0.4), y2 - 6 * Math.sin(angle + 0.4));
    ctx.fill();
    ctx.fillStyle = col; ctx.beginPath(); ctx.arc(x, y1, 4, 0, Math.PI * 2); ctx.fill();
  });
}

window.addEventListener('resize', () => { if (debateEdges.length) drawChart(); });

function autoScroll(el) {
  if (!userScrolledUp) el.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

function md(text) {
  let html = text
    .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>')
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/^---+$/gm, '<hr>')
    .replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/^[-*] (.+)$/gm, '<li>$1</li>')
    .replace(/^\d+\. (.+)$/gm, '<li>$1</li>')
    .replace(/((?:<li>.*<\/li>\n?)+)/g, '<ul>$1</ul>')
    .replace(/\n\n+/g, '</p><p>')
    .replace(/\n/g, '<br>');
  return '<p>' + html + '</p>';
}

const es = new EventSource('/events');

es.addEventListener('header', e => {
  const d = JSON.parse(e.data);
  $('#header').style.display = 'block';
  $('#topic').textContent = d.topic;
  $('#agents-bar').innerHTML = d.agents.map(a =>
    `<span class="agent-chip"><span class="name">${a.name}</span><span class="model">${a.model}</span></span>`
  ).join('');
  $('#meta').textContent = `${d.agents.length} agents · ~${d.est_gb}GB · ${d.mode}`;
  $('#status').style.display = 'none';
  agentSet = d.agents.map(a => a.name);
  const sb = document.getElementById('debateStopBtn');
  if (sb) sb.style.display = '';
});

es.addEventListener('status', e => {
  const d = JSON.parse(e.data);
  $('#status').innerHTML = `<span class="spinner"></span>${d.text}`;
  $('#status').style.display = 'block';
});

es.addEventListener('turn_start', e => {
  const d = JSON.parse(e.data);
  $('#status').style.display = 'none';
  const div = document.createElement('div');
  div.className = d.closing ? 'turn active closing' : 'turn active';
  div.id = `turn-${d.turn}`;
  const label = d.closing ? '<span class="turn-label">CLOSING</span>' : '';
  div.innerHTML = `
    <div class="turn-header">
      <span class="turn-num">#${d.turn}</span>
      <span class="turn-name">${d.name}</span>
      ${label}
    </div>
    <div class="turn-content"><span class="cursor"></span></div>`;
  $('#turns').appendChild(div);
  currentTurnEl = div;
  currentContentEl = div.querySelector('.turn-content');
  autoScroll(div);
});

es.addEventListener('token', e => {
  if (!currentContentEl) return;
  const d = JSON.parse(e.data);
  const cursor = currentContentEl.querySelector('.cursor');
  if (cursor) cursor.remove();
  currentContentEl.appendChild(document.createTextNode(d.text));
  const c = document.createElement('span'); c.className = 'cursor';
  currentContentEl.appendChild(c);
  autoScroll(currentTurnEl);
});

es.addEventListener('turn_end', e => {
  const d = JSON.parse(e.data);
  if (!currentTurnEl) return;
  currentTurnEl.classList.remove('active');
  const cursor = currentContentEl.querySelector('.cursor');
  if (cursor) cursor.remove();
  currentContentEl.classList.add('md');
  currentContentEl.innerHTML = md(d.content);
  let badges = `<span class="dir-badge intent">intent: ${d.intent}</span>`;
  badges += `<span class="dir-badge addressed">to: ${d.addressed}</span>`;
  badges += `<span class="dir-badge next">next: ${d.next_action}</span>`;
  if (d.closing) badges += `<span class="dir-badge closing">closing statement</span>`;
  if (d.yielded) badges += `<span class="dir-badge yielded">yielded</span>`;
  if (d.invited) badges += `<span class="dir-badge invited">invited: ${d.invited}</span>`;
  const dirDiv = document.createElement('div');
  dirDiv.className = 'directives'; dirDiv.innerHTML = badges;
  currentTurnEl.appendChild(dirDiv);
  if (d.addressed && d.addressed !== 'all') {
    debateEdges.push({from: d.name, to: d.addressed, turn: d.turn, intent: d.intent});
  } else if (d.addressed === 'all') {
    // Broadcast: draw edges to all other agents
    agentSet.forEach(a => {
      if (a !== d.name) debateEdges.push({from: d.name, to: a, turn: d.turn, intent: d.intent, broadcast: true});
    });
  }
  if (d.invited)
    debateEdges.push({from: d.name, to: d.invited, turn: d.turn, intent: 'invite'});
  drawChart();
  currentTurnEl = null; currentContentEl = null;
});

es.addEventListener('yield_notice', e => {
  const d = JSON.parse(e.data);
  const div = document.createElement('div');
  div.className = 'yield-notice';
  div.innerHTML = `<span class="name">${d.name}</span> has yielded. ` +
    `Remaining: ${d.remaining.join(', ')}`;
  $('#turns').appendChild(div);
  autoScroll(div);
});

es.addEventListener('summary', e => {
  const d = JSON.parse(e.data);
  const tbl = $('#summary-table');
  tbl.innerHTML = '<tr><th>Agent</th><th>Turns</th><th>Intents</th><th>Status</th></tr>';
  d.agents.forEach(a => {
    const intents = Object.entries(a.intents).map(([k,v]) => `${k}x${v}`).join(', ');
    const st = a.yielded ? '<span style="color:var(--yellow)">yielded</span>' : 'active';
    tbl.innerHTML += `<tr><td class="agent-name">${a.name}</td><td>${a.turns}</td><td>${intents}</td><td>${st}</td></tr>`;
  });
  $('#summary').style.display = 'block';
  autoScroll($('#summary'));
});

es.addEventListener('decision_start', e => {
  $('#status').innerHTML = '<span class="spinner"></span>Generating decision summary...';
  $('#status').style.display = 'block';
});

es.addEventListener('decision_token', e => {
  if ($('#decision').style.display === 'none') {
    $('#decision').style.display = 'block'; $('#status').style.display = 'none';
  }
  const d = JSON.parse(e.data);
  $('#decision-content').appendChild(document.createTextNode(d.text));
  autoScroll($('#decision'));
});

es.addEventListener('decision_end', e => {
  const d = JSON.parse(e.data);
  $('#status').style.display = 'none';
  $('#decision').style.display = 'block';
  $('#decision-content').innerHTML = md(d.content);
  autoScroll($('#decision'));
});

es.addEventListener('done', e => {
  const d = JSON.parse(e.data);
  $('#status').innerHTML = d.reason;
  $('#status').style.display = 'block';
  const sb = document.getElementById('debateStopBtn');
  if (sb) sb.style.display = 'none';
});

es.onerror = () => { es.close(); };
</script>
</body>
</html>"""


# ── HTTP handler ───────────────────────────────────────────────────────

_active_config: str | None = None


class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._html(DASHBOARD_HTML)
        elif path == "/debate":
            self._html(DEBATE_HTML)
        elif path == "/events":
            self._serve_sse()
        elif path == "/health":
            self._json(run_health(
                _active_config or "", as_json=False))
        elif path == "/api/configs":
            self._json(list_configs())
        elif path == "/api/config":
            qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            fname = qs.get("file", [""])[0]
            fpath = CONFIGS_DIR / fname
            if fpath.exists() and fpath.suffix == ".yaml":
                self._text(fpath.read_text())
            else:
                self.send_error(404)
        elif path == "/api/status":
            self._json({"running": debate_running(),
                         "config": _active_config})
        elif path == "/api/debates":
            self._json(list_debates())
        elif path == "/api/debate-file":
            qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            dir_name = qs.get("dir", [""])[0]
            fname = qs.get("file", [""])[0]
            from .save import DEBATES_DIR
            fpath = DEBATES_DIR / dir_name / fname
            if fpath.exists() and fpath.is_file():
                self._text(fpath.read_text(encoding="utf-8"))
            else:
                self.send_error(404)
        elif path == "/history":
            self._html(HISTORY_HTML)
        else:
            self.send_error(404)

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if path == "/api/config":
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
        else:
            self.send_error(404)

    def do_POST(self):
        global _active_config
        path = self.path.split("?")[0]
        body = self._read_body()

        if path == "/api/config":
            qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            fname = qs.get("file", [""])[0]
            if not fname or not fname.endswith(".yaml"):
                self._json({"error": "Invalid filename"}, 400)
                return
            fpath = CONFIGS_DIR / fname
            fpath.write_text(body)
            self._json({"ok": True})

        elif path == "/api/start":
            data = json.loads(body)
            cfg_file = data.get("config", "")
            cfg_path = str(CONFIGS_DIR / cfg_file)
            if not Path(cfg_path).exists():
                self._json({"ok": False, "error": "Config not found"}, 404)
                return
            ok = start_debate(cfg_path)
            if ok:
                _active_config = cfg_file
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": "Debate already running"})

        elif path == "/api/stop":
            stop_debate()
            _active_config = None
            self._json({"ok": True})

        elif path == "/api/parse-yaml":
            try:
                cfg = yaml.safe_load(body)
                self._json(cfg if isinstance(cfg, dict) else {"error": "Not a mapping"})
            except Exception as e:
                self._json({"error": str(e)})

        elif path == "/api/to-yaml":
            data = json.loads(body)
            self._text(yaml.dump(data, default_flow_style=False,
                                 allow_unicode=True, sort_keys=False))
        else:
            self.send_error(404)

    # ── Helpers ──

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

    def _serve_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = _bus.subscribe()
        try:
            while True:
                try:
                    payload = q.get(timeout=30)
                    if payload is None:  # poison pill = bus reset
                        break
                    self.wfile.write(payload.encode())
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            _bus.unsubscribe(q)

    def log_message(self, fmt, *args):
        pass


# ── Server entry point ─────────────────────────────────────────────────

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
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True

    ip = _detect_ip()
    print(f"\n  Agora Dashboard → http://{ip}:{port}")
    print(f"                    http://localhost:{port}")
    print("  Press Ctrl+C to stop\n")

    if cfg_path:
        start_debate(cfg_path)

    threading.Thread(target=webbrowser.open,
                     args=(f"http://localhost:{port}",), daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down.")
        stop_debate()
        server.server_close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Agora Dashboard")
    ap.add_argument("config", nargs="?", default=None,
                    help="Path to YAML config (optional — starts dashboard only)")
    ap.add_argument("--port", "-p", type=int, default=8420)
    args = ap.parse_args()
    serve(args.config, port=args.port)
