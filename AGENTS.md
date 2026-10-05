# AGENTS.md

Guidance for AI agents and contributors working in this repo.

## What this is

A local browser agent: **Browser Use** reads the page, **Jev** (via OpenRouter's
Decisions API) picks one bounded operation + target, **Browser Harness** validates
and executes it. A small **Jev-vs-Qwen3-32B** benchmark ships as evidence.

## Run a task

```sh
uv sync
cp .env.example .env      # add OPENROUTER_API_KEY; never commit .env
uv run --env-file .env browser-agent \
  --url 'https://www.wikipedia.org/' \
  --goal 'Open the Wikipedia article about Marie Curie.'
```

## Layout

- `jev_ultrafast/agent.py` — the complete agent loop (typed choices, observable state, bounded execution)
- `jev_ultrafast/browser.py` — Browser Use observes structure; Browser Harness validates and executes
- `jev_ultrafast/page_reader.py` — structured, read-only page observation powered by Browser Use
- `jev_ultrafast/model.py` — OpenRouter-hosted Jev decisions with an independent model fallback
- `jev_ultrafast/questions.py` — instructions for the dynamic operation/element policy and the text helper
- `jev_ultrafast/cli.py` / `jev_ultrafast/demo.py` — CLI and loopback web-UI entry points
- `benchmark.py` — the Jev-vs-Qwen benchmark runner (two arms, five tasks)
- `benchmark_results/<run>/report.md` — per-case results and root-cause attribution
- `ARCHITECTURE.md` — full candidate, freshness, and hit-test rules

## Conventions

- `.env` holds real API keys and is git-ignored; never commit it or paste keys into chat.
- Benchmark findings live in `render_report()` inside `benchmark.py`; edit that function, not the generated markdown.
- The benchmark's comparison model is Qwen3-32B, not Claude. `BLOCKED` is a model decision, not a failure.
- License: MIT, adapted from `browser-use/jev-ultrafast`.
