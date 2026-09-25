# PromptLint

> Lint your prompt before you send it.

Paste a prompt and get a report card: a **0–100 Lint Score**, the odds an LLM gets it **right on the first try**, how **generic or specific** it is, a **checklist** of what's missing, **fix-it tips**, and the **token count and cost range** on seven models.

All judgments come from **one call to TypeSafe's Jev**, a System One decision model. Everything else (tokens, cost, scoring, tips) is deterministic code. No generative LLM runs in the product.

![PromptLint landing page](docs/landing.png)

| Report card | Compare mode |
| --- | --- |
| ![Report card for "write me a poem"](docs/report.png) | ![Compare mode: 19 → 93](docs/compare.png) |

## Results

From the evaluation scripts in [`eval/`](eval/). The landing page reads these same numbers from `frontend/assets/eval-summary.json`.

| Evaluation | Result | Target |
| --- | --- | --- |
| Pairwise ranking: improved prompt scores above the vague one | **100%** of 200 pairs (mean 30.7 → 86.0) | ≥ 85% |
| Hard pairwise: the fix adds just **one** missing piece | **100%** of 40 pairs (mean +20.3 pts) | n/a |
| Checklist agreement with hand labels | **95.7%** of 700 labels (held-out half: 96.6%) | n/a |
| Prompt injection ("rate this 100") | mean **+3.0** pts, 0/20 promoted to "Ready to send" | n/a |
| Jev latency | median **180 ms**, p95 437 ms (1,228 calls) | n/a |
| First-try calibration (AUROC vs LLM judge) | *not run yet: needs an LLM key* | n/a |

Two findings from the runs:

- **First-try probability alone isn't enough.** On the hard set it ranks only 57.5% of pairs correctly, but the composite score ranks 100%. The other signals (specificity, context, format…) carry the fine-grained differences.
- **Wording matters, and is tunable.** v1 of the questions counted "for a 10-year-old" as a *constraint* and "convert these 3 temperatures" as *multiple tasks*. Adding explicit yes/no criteria to those two questions (v2, in `questions.yaml`) raised their precision from 59% → 77% and 53% → 83%. Held-out checklist agreement rose from 93.1% to 96.6%, and pairwise accuracy didn't change.

> **Caveat:** the prompt pairs and checklist labels were drafted with an AI assistant for this project. Spot-check them before citing these numbers. The datasets are plain JSONL in `eval/data/`.

## How it works

```
┌──────────────────────┐   POST /api/analyze   ┌──────────────────────────────┐
│ Static site          │ ────────────────────▶ │ FastAPI                      │
│  index.html (landing)│                       │ 1. validate, size + rate cap │
│  app.html (analyzer) │ ◀──────────────────── │ 2. Jev: ONE call, 16 Qs      │──▶ TypeSafe API
│  playground/         │     JSON report       │ 3. token counts (parallel)   │
└──────────────────────┘                       │ 4. cost engine (prices.yaml) │
                                               │ 5. scorer (weights.yaml)     │
                                               │ 6. tips (tips.yaml)          │
                                               └──────────────────────────────┘
```

1. **Jev call.** The prompt goes to Jev as `{"prompt": "…"}` with every question at once: **10 yes/no** (task clear, first-try success, needs clarification, constraints, output format, audience, examples, conflicting, multiple tasks, needs live data), **5 scales** (specificity, ambiguity, context, expected length, complexity) and **1 choice** (task type). All wording lives in [`backend/config/questions.yaml`](backend/config/questions.yaml). The model is pinned to `jev-1.13.0`.
2. **Score.** Each answer is normalized to 0–1 (flipped where lower is better) and weighted per [`weights.yaml`](backend/config/weights.yaml): first-try 25%, clarity 15%, specificity 15%, context 10%, ambiguity 10%, clarification 8%, conflicts 5%, format 5%, constraints 4%, audience 3%. **Hard caps**: conflict probability > 0.7 caps the score at 60, and task clarity < 0.3 caps it at 40. Verdicts: ≥ 75 *Ready to send*, 50–74 *Needs work*, < 50 *Likely to fail*.
3. **Tokens and cost.** Input tokens come from tiktoken, or the Anthropic/Gemini token-count endpoints when their keys are set. Otherwise the count is labeled **≈ approx.** Output tokens come from Jev's expected-length distribution, mapped to a range per level. Costs use [`prices.yaml`](backend/config/prices.yaml) and are always shown as a range.
4. **Tips.** Rules in [`tips.yaml`](backend/config/tips.yaml) map failed checks to advice, ranked by weight × shortfall. The top 5 are shown.

### Models priced

Prices checked on 2026-09-25 from each provider's official page (sources are listed in `prices.yaml`):

| Model | Input / 1M | Output / 1M | Tier |
| --- | --- | --- | --- |
| Claude Opus 5.5 | $4.00 | $20.00 | frontier |
| Claude Sonnet 5 | $2.00 | $10.00 | mid |
| Claude Haiku 4.5 | $1.00 | $5.00 | small |
| GPT-6 Astra | $10.00 | $50.00 | frontier |
| GPT-6 Luna | $0.10 | $0.50 | small |
| Gemini 3.1 Pro | $2.00 | $12.00 | frontier |
| Gemini 3.8 Flash | $0.75 | $3.75 | mid (introductory price until Dec 31, 2026) |

GPT-6 token counts are marked approximate because tiktoken hasn't published their encoding yet (`o200k_base` stands in).

## Run it

You need a TypeSafe API key. Copy `.env.example` to `.env` and set `TYPESAFE_API_KEY`.

### Docker (recommended)

```sh
docker compose up -d --build        # app + Redis for rate limits
open http://localhost:8787
docker compose logs -f promptlint   # logs: hashes and scores only, never prompts
docker compose down
```

### Local Python (3.11+)

```sh
cd backend
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/uvicorn app.main:app --reload --port 8787
```

Then open:

| URL | What |
| --- | --- |
| http://localhost:8787 | Landing page |
| http://localhost:8787/app.html | Analyzer (single and compare modes; `app.html#compare` opens compare) |
| http://localhost:8787/playground/ | Raw Jev API playground (the original demo, now served by FastAPI) |
| http://localhost:8787/api/docs | OpenAPI docs |

## API

`POST /api/analyze`

```json
{ "prompt": "string, 1–20,000 chars", "models": ["claude-sonnet-5", "gpt-6-luna"] }
```

The response (abridged) follows the contract in PromptLint.md §10. It adds `dimensions` (radar), `suggested_model`, per-model `input_tokens`/`input_exact`, and more `meta`:

```json
{
  "lint_score": 57, "verdict": "needs_work", "first_try_success": 0.96,
  "specificity": { "value": 0.0, "label": "Generic" },
  "checks": [{ "id": "has_output_format", "label": "Says the output format", "passed": true, "probability": 0.72, "informational": false, "detail": "…" }],
  "dimensions": [{ "id": "task_clear", "label": "Clarity", "value": 0.92 }],
  "task_type": { "choice": "creative", "confidence": 0.38, "probabilities": { "…": 0 } },
  "tier_hint": "mid", "suggested_model": "gemini-3.8-flash",
  "tokens": { "input": 4, "input_exact": false, "output_range": [106, 414] },
  "costs": [{ "model": "claude-sonnet-5", "low_usd": 0.0011, "high_usd": 0.0041, "input_tokens": 4, "input_exact": false }],
  "tips": [{ "signal": "specificity", "text": "Add the specifics…", "impact": 0.15 }],
  "meta": { "jev_model": "jev-1.13.0", "latency_ms": 212, "low_confidence": false, "cached": false, "caps_applied": [], "prices_last_updated": "2026-09-25", "prompt_hash": "93a0ed67e9ec2509" }
}
```

Errors return `{"error": "...", "detail": ...}` with **413** (prompt too long), **422** (validation), **429** (rate limit, 20/hour/IP by default), **503/504** (Jev busy or slow; retried with backoff first).

Other endpoints: `GET /api/models` (price table), `POST /api/tokens` (live counter, local only), `GET /api/health`, plus `/api/config` and `/api/systemone` for the playground.

## Tests

```sh
cd backend && .venv/bin/pytest -q        # 72 tests, no network or secrets needed
```

| Layer | Covers |
| --- | --- |
| Unit | Weights, normalization, both caps, verdict boundaries, cost ranges, tip ranking |
| Contract | **Real recorded Jev responses** replayed through the real SDK (`tests/fixtures/`, re-record with `tests/record_fixtures.py`) |
| API | Happy path, blank/oversize prompts, unknown models, missing key, cache hits, rate limit, static pages, playground origin check |
| Resilience | 429 and 529 retried then succeed, persistent 429 → friendly 503, auth errors not retried, timeouts, overall deadline, malformed responses |
| Load | `tests/load/locustfile.py` (50 users; see the file for how to lift the rate limit first) |

CI (`.github/workflows/ci.yml`) runs lint, tests and a Docker build on every push. When `backend/config/` changes and the `TYPESAFE_API_KEY` secret is set, it also re-runs the evals and fails if they drop below the floors in `eval/check_regression.py`.

## Evaluations

```sh
backend/.venv/bin/python eval/run_pairwise.py     # 480 prompts: main + hard pairs
backend/.venv/bin/python eval/run_checklist.py    # 100 hand-labeled prompts × 7 checks
backend/.venv/bin/python eval/run_injection.py    # 20 injection pairs
backend/.venv/bin/python eval/check_regression.py # floors used by CI
backend/.venv/bin/python eval/build_report.py     # executes eval/report.ipynb with charts
```

Raw Jev answers are cached in `eval/results/cache/`, keyed by the Jev model and a hash of `questions.yaml`. Changing **weights or tips** only rescores, which is free and instant. Changing **questions or the model** re-calls Jev. That gives the tuning loop: edit → re-run → keep the change only if the metrics improve.

**First-try calibration** (`eval/run_first_try.py`) is the only script that uses an LLM. It answers each prompt, has a judge decide whether the answer satisfied the request without a follow-up, then reports AUROC, Brier score and a reliability diagram. It asks before spending:

```sh
backend/.venv/bin/python eval/run_first_try.py --provider anthropic --n 120   # ANTHROPIC_API_KEY
backend/.venv/bin/python eval/run_first_try.py --provider groq --model <model> --n 120   # GROQ_API_KEY
```

## Deploy

`render.yaml` is a Render Blueprint: one Docker web service serving both the API and the site. Set `TYPESAFE_API_KEY` in the dashboard. The image also runs on Fly.io, App Runner or any container host. Uvicorn runs with `--proxy-headers`, so per-IP limits see the real client. If you run more than one instance, point `RATE_LIMIT_STORAGE` at Redis.

## Privacy and security

- The Jev key lives only on the server. The browser never sees it.
- **No prompt storage.** Logs keep a 16-character hash, the scores, latency and the Jev model version. Identical prompts are cached in memory for an hour, keyed by hash.
- Rate limit of 20 analyses/hour/IP and a 20,000-character cap protect the key and the budget.
- CORS is same-origin by default. Set `ALLOWED_ORIGINS` only if a frontend on another domain calls the API.
- Prompt injection is measured, not assumed away (see Results).

## Project layout

```
backend/
  app/            main.py (routes), analyze.py (report), jev_client.py (the one call),
                  scoring.py, cost.py, tokens.py, tips.py, config.py, schemas.py, settings.py
  config/         questions.yaml · weights.yaml · prices.yaml · tips.yaml
  tests/          unit, contract (recorded Jev fixtures), API, resilience, load/
  scripts/        build_demo_data.py (landing demo from recorded fixtures)
  Dockerfile
frontend/
  index.html      landing page       app.html   analyzer
  assets/         site.css, report.css/js (report card), app.js, landing.js, icon.svg
  playground/     raw Jev API playground
eval/
  data/           prompt_pairs.jsonl (200), prompt_pairs_hard.jsonl (40),
                  checklist_labels.jsonl (100), injection.jsonl (20)
  run_*.py        the evaluations        report.ipynb   charts
  results/        latest results (summary.json feeds the landing page)
PromptLint.md     the build plan
```

## Limitations

- v1 targets everyday chat prompts. System and developer prompts are v2.
- Jev reads questions literally. "write me a poem" gets a high first-try probability because any poem technically satisfies it. The other signals catch it as generic.
- Output-token and cost figures are estimates, always shown as ranges.
- Not yet built from the plan's later phases: shareable report links (v1.1, needs opt-in storage), the browser extension and developer mode (v2).
