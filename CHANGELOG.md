# Changelog

## 0.3.2 (2026-04-17)

### Fixed
- URL-encoded Turkish characters in debate directory names (detail.html `decodeURIComponent`)
- Sticky timeline rail with scroll on debate view
- Hardcoded `#1e293b` background replaced with `var(--bg-mid)` token
- Intervene bar overlap — added bottom padding to debate pane (90px)
- Pre-debate quiz is now optional via confirm dialog (was forced)

### Changed
- `AGENT_COLORS` rewritten as Proxy for live CSS-var resolution
- Minimal language selector — EN/TR labels (smaller, no native checkmark)

### Added
- New i18n keys: `msg.quiz_confirm`, `toast.quiz_wait`, `toast.quiz_no_questions`, `toast.quiz_error`, `btn.generating_quiz`
- `.claude/` in `.gitignore`

## 0.3.1 (2026-04-16)

### Added
- **Web UI overhaul** — 5-page dashboard with design system (base.css/base.js)
- **Live debate streaming** via SSE with chat-style bubble layout
- **Pause / Resume / Intervene** — inject messages into running debates
- **Debate continuation** — resume finished debates with new context and turns
- **Pre-debate knowledge check** — quiz system to enrich agent context
- **i18n system** — English and Turkish translations, per-language JSON files, localStorage persistence
- **Compare mode** — side-by-side comparison of two debates
- **A2A JSON export** — serialize debates as JSON-RPC envelopes
- **PDF export** — print-friendly debate output
- **Interaction map** — canvas-based agent communication visualization
- **Agent status bar** — real-time speaking/yielded indicators
- **Progress ring** — turn counter with phase labels
- **Config editor** — YAML and form-based editing with live sync
- **Mobile-optimized** — responsive layout with safe-area support
- **Accessibility** — skip links, ARIA, focus traps, keyboard navigation
- **Test suite** — 36 tests covering protocol, event bus, and server endpoints
- **pyproject.toml** — proper package structure with entry points

### Changed
- `DebateServer` class replaces global state for thread safety
- Backend noise filtering improved (Codex headers, CLI startup logs)
- Yield protection: agents cannot yield before minimum fair share of turns
- Max retries increased from 2 to 3 with better failure detection

### Removed
- Structured output mode (`@claim/@counter/@confidence`) — simplified back to three core directives

## 0.2.0 (2026-04-15)

- Initial public release
- Core protocol with three directives: `@intent`, `@addressed`, `@next`
- CLI, MLX, Ollama, and API backends
- Basic web UI with SSE streaming
- Transcript and decision saving
