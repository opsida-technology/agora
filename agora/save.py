"""
agora.save — Save debate transcripts to markdown.

Creates a folder per debate:
    debates/2026-04-13_0345_fine-tune-vs-rag/
        transcript.md      — full conversation log
        decision.md        — decision summary (if available)
        config.yaml        — snapshot of the config used
        meta.json          — machine-readable metadata

Can be called:
    - Automatically at debate end (from web.py)
    - Manually via:  python -m agora.save
      (connects to running server's SSE and snapshots current state)
"""
import json
import re
import shutil
import urllib.request
from datetime import datetime
from pathlib import Path


DEBATES_DIR = Path(__file__).resolve().parent.parent / "debates"


def _slugify(text: str, max_len: int = 40) -> str:
    """Turn a topic into a short filesystem-safe slug."""
    text = text.strip().split("\n")[0]  # first line only
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_]+", "-", text).strip("-")
    return text[:max_len].rstrip("-")


def save_from_events(events: list[dict], cfg_path: str | None = None,
                     cfg_text: str | None = None) -> Path:
    """Save a debate from a list of parsed SSE events.

    Returns the output directory path.
    """
    # Extract data from events
    header = None
    turns = []
    yields = []
    summary = None
    decision_content = None
    done_reason = None

    for ev in events:
        t = ev.get("type")
        d = ev.get("data", {})
        if t == "header":
            header = d
        elif t == "turn_end":
            turns.append(d)
        elif t == "yield_notice":
            yields.append(d)
        elif t == "summary":
            summary = d
        elif t == "decision_end":
            decision_content = d.get("content", "")
        elif t == "done":
            done_reason = d.get("reason", "")

    if not header:
        raise ValueError("No header event found — debate may not have started")

    topic = header.get("topic", "untitled").strip()
    agents = header.get("agents", [])
    now = datetime.now()
    slug = _slugify(topic)
    folder_name = f"{now.strftime('%Y-%m-%d_%H%M')}_{slug}"

    out_dir = DEBATES_DIR / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── transcript.md ──
    lines = [
        f"# {topic}",
        "",
        f"**Date:** {now.strftime('%Y-%m-%d %H:%M')}  ",
        f"**Agents:** {', '.join(a['name'] + ' (' + a['model'] + ')' for a in agents)}  ",
        f"**Mode:** {header.get('mode', '?')}  ",
        f"**Turns:** {len(turns)}  ",
        "",
        "---",
        "",
    ]

    for turn in turns:
        closing = " (CLOSING)" if turn.get("closing") else ""
        yielded = " — YIELDED" if turn.get("yielded") else ""
        lines.append(f"## Turn {turn['turn']}: {turn['name']}{closing}")
        lines.append("")
        # Directives bar
        parts = [f"`intent: {turn.get('intent', '?')}`"]
        if turn.get("addressed"):
            parts.append(f"`to: {turn['addressed']}`")
        if turn.get("invited"):
            parts.append(f"`invited: {turn['invited']}`")
        if turn.get("yielded"):
            parts.append("`yielded`")
        lines.append(" · ".join(parts))
        lines.append("")
        lines.append(turn.get("content", ""))
        lines.append("")

    for y in yields:
        lines.append(f"> **{y['name']}** has yielded. Remaining: {', '.join(y.get('remaining', []))}")
        lines.append("")

    if done_reason:
        lines.append("---")
        lines.append(f"*{done_reason}*")
        lines.append("")

    (out_dir / "transcript.md").write_text("\n".join(lines), encoding="utf-8")

    # ── decision.md ──
    if decision_content:
        dec_lines = [
            f"# Decision Summary",
            "",
            f"**Debate:** {topic}",
            f"**Date:** {now.strftime('%Y-%m-%d %H:%M')}",
            "",
            "---",
            "",
            decision_content,
        ]
        (out_dir / "decision.md").write_text("\n".join(dec_lines), encoding="utf-8")

    # ── config.yaml ──
    if cfg_text:
        (out_dir / "config.yaml").write_text(cfg_text, encoding="utf-8")
    elif cfg_path:
        src = Path(cfg_path)
        if src.exists():
            shutil.copy2(src, out_dir / "config.yaml")

    # ── summary table in meta.json ──
    meta = {
        "topic": topic,
        "date": now.isoformat(),
        "folder": folder_name,
        "turns": len(turns),
        "agents": agents,
        "mode": header.get("mode"),
        "done_reason": done_reason,
    }
    if summary:
        meta["agent_stats"] = summary.get("agents", [])

    # Track continuation chain from config
    if cfg_text:
        try:
            import yaml as _yaml
            _cfg = _yaml.safe_load(cfg_text)
            if isinstance(_cfg, dict):
                if _cfg.get("_parent_debate"):
                    meta["parent_debate"] = _cfg["_parent_debate"]
                if _cfg.get("_parent_chain"):
                    meta["parent_chain"] = _cfg["_parent_chain"]
        except Exception:
            pass

    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return out_dir


def save_from_sse_url(url: str = "http://localhost:8420/events",
                      status_url: str = "http://localhost:8420/api/status",
                      config_base: str = "http://localhost:8420/api/config") -> Path:
    """Connect to a running server and snapshot the current debate state."""
    # Get config name from status
    try:
        st = json.loads(urllib.request.urlopen(status_url, timeout=5).read())
        cfg_name = st.get("config")
    except Exception:
        cfg_name = None

    # Get config text
    cfg_text = None
    if cfg_name:
        try:
            cfg_text = urllib.request.urlopen(
                f"{config_base}?file={cfg_name}", timeout=5
            ).read().decode()
        except Exception:
            pass

    # Read SSE events from replay buffer.
    # The replay dumps all past events immediately, so we read until
    # we see a terminal event or the stream stalls.
    events = []
    terminal_types = {"decision_end", "saved"}
    try:
        resp = urllib.request.urlopen(url, timeout=30)
        # Set a short socket timeout so readline doesn't block forever
        # after the replay buffer is exhausted.
        resp.fp.raw._sock.settimeout(3.0)
        buf = ""
        while True:
            try:
                raw = resp.readline()
            except Exception:
                break  # socket timeout = replay buffer exhausted
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line.startswith("event: "):
                buf = line[7:]
            elif line.startswith("data: ") and buf:
                try:
                    events.append({"type": buf, "data": json.loads(line[6:])})
                except json.JSONDecodeError:
                    pass
                if buf in terminal_types:
                    break  # got the last event we care about
                buf = ""
    except Exception as e:
        if not events:
            raise RuntimeError(f"Could not read events: {e}")

    return save_from_events(events, cfg_text=cfg_text)


# ── Integration with web.py ──

def save_from_bus(bus, cfg_path: str | None = None):
    """Save directly from the EventBus replay log (called at debate end)."""
    events = []
    with bus._lock:
        for payload in bus._log:
            # Parse SSE payload back to event+data
            lines = payload.strip().split("\n")
            ev_type = ""
            for line in lines:
                if line.startswith("event: "):
                    ev_type = line[7:]
                elif line.startswith("data: ") and ev_type:
                    try:
                        events.append({"type": ev_type, "data": json.loads(line[6:])})
                    except json.JSONDecodeError:
                        pass

    cfg_text = None
    if cfg_path:
        p = Path(cfg_path)
        if p.exists():
            cfg_text = p.read_text(encoding="utf-8")

    return save_from_events(events, cfg_path=cfg_path, cfg_text=cfg_text)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--from-server":
        url = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8420/events"
        out = save_from_sse_url(url)
    else:
        out = save_from_sse_url()
    print(f"Saved to: {out}")
