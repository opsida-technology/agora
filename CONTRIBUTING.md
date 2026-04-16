# Contributing to Agora Protocol

Thanks for your interest in contributing! Here's how to get started.

## Development Setup

```bash
git clone https://github.com/opsida/agora-protocol.git
cd agora-protocol
pip install -e ".[dev]"
```

## Running Tests

```bash
pytest tests/ -v
```

All tests must pass before submitting a PR.

## Project Structure

```
agora/
  protocol.py    # Core protocol: directives, message parsing, A2A
  agent.py       # Agent dataclass and turn execution
  backends.py    # Model backends (CLI, MLX, Ollama, API)
  orchestrator.py# Config loading, agent construction
  web.py         # HTTP server, SSE streaming, all API endpoints
  health.py      # Pre-debate health checks
  save.py        # Transcript/decision persistence
  static/        # CSS, JS, i18n files
  templates/     # HTML templates
```

## Guidelines

- Keep dependencies minimal. Core requires only `pyyaml`.
- Write tests for new features. Place them in `tests/`.
- Follow existing code style (no linter enforced yet, but be consistent).
- Update `lang/en.json` and `lang/tr.json` if you add UI strings.
- Don't break the three-directive protocol (`@intent`, `@addressed`, `@next`).

## Adding a Language

1. Copy `agora/static/lang/en.json` to `agora/static/lang/{code}.json`
2. Translate all values (keys stay the same)
3. Add the language code to `I18N.AVAILABLE` in `agora/static/i18n.js`
4. Add an `<option>` to the language selector in `dashboard.html`

## Submitting Changes

1. Fork the repo and create a feature branch
2. Make your changes
3. Run `pytest tests/ -v` and ensure all tests pass
4. Submit a pull request with a clear description

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
