"""
agora.display — Terminal UI (rich when available, plain-text fallback).
"""
import sys

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.rule import Rule
    from rich.markdown import Markdown
    console = Console()
    RICH = True
except ImportError:
    console = None  # type: ignore[assignment]
    RICH = False


# ── Size estimation ─────────────────────────────────────────────────────

SIZE_HINTS = {
    "0.8b": 1, "1b": 1, "2b": 2, "e2b": 2, "3b": 3, "4b": 3,
    "e4b": 3, "7b": 5, "8b": 5, "9b": 6, "14b": 9, "20b": 12,
    "26b": 16, "27b": 17, "35b": 21,
}


def estimate_gb(agents) -> int:
    total = 0
    for a in agents:
        if not a.backend.is_local:
            continue
        n = a.backend.model_id.lower()
        total += next((v for k, v in SIZE_HINTS.items() if k in n), 8)
    return total


# ── Primitives ──────────────────────────────────────────────────────────

def status(text: str, warn: bool = False):
    """Print a status line (dim or yellow)."""
    if RICH:
        style = "yellow" if warn else "dim"
        console.print(f"  [{style}]{text}[/{style}]", highlight=False)
    else:
        print(f"  {text}", flush=True)


def emit_token(token: str):
    """Write a single streaming token to stdout."""
    sys.stdout.write(token)
    sys.stdout.flush()


# ── Debate chrome ───────────────────────────────────────────────────────

def show_header(topic: str, agents, est: int, mode: str):
    local_n = sum(1 for a in agents if a.backend.is_local)
    if RICH:
        console.print()
        console.print(Panel(
            f"[bold]{topic}[/bold]",
            title="[blue bold]AGORA DEBATE[/blue bold]",
            border_style="blue", padding=(1, 2)))
        names = " · ".join(f"[cyan]{a.name}[/cyan]" for a in agents)
        console.print(f"  {names}")
        console.print(
            f"  [dim]{len(agents)} agents ({local_n} local, "
            f"{len(agents)-local_n} remote) · ~{est} GB · {mode}[/dim]\n")
    else:
        print(f"\nAgents: {len(agents)} ({local_n} local, "
              f"{len(agents)-local_n} remote) | "
              f"Local est: ~{est} GB | Mode: {mode}")
        print(f"\n{'='*72}\nTOPIC: {topic}\n{'='*72}\n")


def show_turn_start(turn: int, name: str, closing: bool = False):
    label = "CLOSING" if closing else f"Turn {turn}"
    if RICH:
        style = "yellow" if closing else "dim"
        console.print(
            Rule(f"[bold]{label}[/bold] · [cyan]{name}[/cyan]",
                 style=style))
    else:
        print(f"\n── {label} · {name} ──")


def show_turn_end(msg, closing: bool = False, retry: bool = False):
    print()   # newline after streamed content
    if retry:
        if RICH:
            console.print(
                f"  [red bold]⟳ {msg.speaker} failed — retrying…[/red bold]")
            console.print(f"  [dim red]{msg.content}[/dim red]")
        else:
            print(f"  ⟳ {msg.speaker} failed — retrying… ({msg.content})")
        print()
        return
    if RICH:
        parts = [f"[green]intent:[/green]{msg.intent}",
                 f"[green]to:[/green]{msg.addressed}",
                 f"[green]next:[/green]{msg.next_action}"]
        line = "  " + " · ".join(parts)
        console.print(f"[dim]{line}[/dim]")
        if closing:
            console.print("  [yellow]↳ closing statement[/yellow]")
        if msg.next_action == "yield":
            console.print(f"  [yellow]↳ {msg.speaker} yielded[/yellow]")
        if msg.invited:
            console.print(f"  [blue]↳ invited {msg.invited}[/blue]")
    else:
        print(f"  [{msg.intent} → {msg.addressed} | "
              f"next:{msg.next_action}]")
        if closing:
            print("  ↳ closing statement")
        if msg.next_action == "yield":
            print(f"  ↳ {msg.speaker} yielded")
        if msg.invited:
            print(f"  ↳ invited {msg.invited}")
    print()


def show_yield_notice(name: str, remaining: list[str]):
    names = ", ".join(remaining)
    if RICH:
        console.print(Panel(
            f"[yellow bold]{name}[/yellow bold] has yielded.\n"
            f"[dim]Remaining: {names} — final word incoming.[/dim]",
            border_style="yellow", padding=(0, 2)))
    else:
        print(f"\n  *** {name} has yielded. "
              f"Remaining: {names} — final word incoming. ***\n")


def show_all_yielded():
    if RICH:
        console.print(
            "[bold green]All agents yielded. "
            "Debate complete.[/bold green]\n")
    else:
        print("All agents yielded. Debate complete.\n")


def show_last_standing(name: str):
    msg = f"Only {name} remains — debate complete."
    if RICH:
        console.print(f"[bold green]{msg}[/bold green]\n")
    else:
        print(f"{msg}\n")


def show_max_turns(max_turns: int):
    note = f"Max turns ({max_turns}) reached."
    if RICH:
        console.print(f"[dim]{note}[/dim]\n")
    else:
        print(f"{note}\n")


def show_summary(agents):
    if RICH:
        table = Table(title="Debate Summary", border_style="dim")
        table.add_column("Agent", style="cyan")
        table.add_column("Turns", justify="right")
        table.add_column("Intents")
        table.add_column("Status")
        for a in agents:
            intents_str = ", ".join(
                f"{k}×{v}" for k, v in a.stats["intents"].items())
            st = "[yellow]yielded[/yellow]" if a.yielded else "active"
            table.add_row(a.name, str(a.stats["turns"]), intents_str, st)
        console.print()
        console.print(table)
        console.print()
    else:
        print("─" * 72)
        print("SUMMARY")
        for a in agents:
            y = " (yielded)" if a.yielded else ""
            print(f"  {a.name}: {a.stats['turns']} turns, "
                  f"intents={a.stats['intents']}{y}")
        print("=" * 72)


def show_generating_decision():
    if RICH:
        console.print(
            Rule("[bold yellow]Generating Decision Summary[/bold yellow]",
                 style="yellow"))
    else:
        print("\n── Generating Decision Summary ──")


def show_decision(content: str):
    if RICH:
        console.print()
        console.print(Panel(
            Markdown(content),
            title="[green bold]DECISION SUMMARY[/green bold]",
            border_style="green", padding=(1, 2)))
    else:
        print(f"\n{'='*72}")
        print("DECISION SUMMARY")
        print(f"{'='*72}")
        print(content)
        print(f"{'='*72}")
