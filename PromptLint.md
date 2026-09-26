# PromptLint — Build Plan

> Lint your prompt before you send it.

PromptLint is a web app **and a public API** where a user (or an app) sends a prompt and instantly gets a report card: how good the prompt is, whether an LLM is likely to get it right on the first try, how generic or specific it is, what's missing, and how many tokens and dollars it will cost.

> **v1.1 update (Sept 2026):** PromptLint now also implements the API side of the *Prompt Quality Scorer* design (`prompt-quality-scorer.md`, Phase 0–1): a public `/v1` API with self-serve accounts and hashed API keys, plans and quotas, usage metering, a pluggable backend layer with a heuristic baseline and automatic fallback, PQS's response shape and `pqs_score` alongside the Lint Score, and a free-forever hosting plan. See §20–§21. PQS Phase 2 (training an own model) is not part of this project.

**Core principle:** PromptLint never calls a generative LLM in the product. All judgments come from **one call to TypeSafe's Jev** (a System One decision model), and all math (tokens, cost, scoring) is deterministic code.

---

## 1. Decisions and assumptions

| Decision | Choice |
|---|---|
| Judgment engine | TypeSafe Jev only (Noul / Choice / Score questions) |
| LLM output shown to users | None. No answer previews, no prompt rewrites. |
| Token counting, cost math, scoring | Deterministic code, never Jev |
| Fix-it tips | Rule-based: mapped from failed checks |
| v1 audience | Everyday chat prompts (ChatGPT / Claude style). Developer / system-prompt mode is v2. |
| v1 deployment | Portfolio demo with a public URL, rate-limited |
| LLM usage allowed | Only in the offline evaluation script, never in the app |
| Public API (v1.1) | `/v1` endpoints from the PQS design, self-serve sign-up (email + password), keys stored as hashes |
| Scores (v1.1) | Both: `lint_score` (this plan, §7) and `pqs_score` (PQS §9.5), side by side; `overall_score` = `lint_score` |
| Backends (v1.1) | Pluggable: `jev` (default) and a rule-based `heuristic` baseline; `auto` falls back to the heuristic when Jev fails |
| Database (v1.1) | SQLite for local dev and tests; Postgres in production (Neon free tier); Alembic migrations |
| Hosting (v1.1) | Free forever as of Sept 2026: Render free web service + Neon free Postgres, no Redis (§21) |

---

## 2. Features (v1)

1. **Lint Score (0–100)** with a verdict: *Ready to send* (≥ 75), *Needs work* (50–74), *Likely to fail* (< 50).
2. **First-try success prediction.** The probability that an LLM understands the task and gives an answer that won't need another round.
3. **Generic ↔ Specific meter.**
4. **Checklist:** task clarity, context, constraints, output format, audience, examples, conflicting instructions, multiple tasks, needs current info.
5. **Tokens and cost:** exact input tokens, an estimated output-token range, and a cost range for each selected model.
6. **Fix-it tips:** 2–5 concrete suggestions generated from the failed checks.
7. **Model-tier hint:** "a small model is enough" vs "needs a frontier model" (reuses the Cost-aware LLM Router logic).
8. **Compare mode** (pulled forward from v1.1): two versions side by side with the score delta.
9. **Public API** (v1.1): `/v1/score`, `/v1/score/batch`, `/v1/pricing`, `/v1/usage`, `/v1/health`, with accounts, keys, quotas and a dashboard (§20).
10. **Backend toggle** (v1.1): choose Jev or the heuristic baseline in the analyzer; every report says which backend answered.

---

## 3. Architecture

```
┌────────────────────────┐  /api/analyze (per-IP)   ┌────────────────────────────────────┐
│ Static site            │ ───────────────────────▶ │ FastAPI                            │
│  landing · analyzer    │                          │  auth (key / session) · rate limit │
│  account · API docs    │                          │  · quota · validate · meter        │
└────────────────────────┘                          │                                    │
┌────────────────────────┐  /v1/* (Bearer key)      │  Backend router                    │
│ Your app               │ ───────────────────────▶ │   jev ── ONE call, all Qs ─────────┼──▶ TypeSafe API
└────────────────────────┘                          │   heuristic (baseline / fallback)  │
                                                    │  token counter · cost engine       │
                                                    │  lint_score + pqs_score · tips     │
                                                    └──────────────┬─────────────────────┘
                                                                   │ keys, sessions, usage,
                                                                   ▼ event metadata (no prompts)
                                                          Postgres (Neon free) / SQLite (dev)
```

The request path is one Jev call (about 100–500 ms; median 182 ms measured) plus local computation. There's no second model hop. If Jev fails, `auto` mode answers from the heuristic instead, marked `degraded`.

---

## 4. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Backend | Python 3.11, FastAPI, Pydantic | Known stack, typed request/response models |
| Jev client | `typesafe-sdk` (`TypeSafeClient` / `AsyncTypeSafeClient`) | Official SDK with built-in retries |
| Tokenizers | `tiktoken` (OpenAI); provider token-count endpoints (Anthropic, Gemini); char/4 fallback | Exact where possible, labeled "approx." otherwise |
| Config | YAML files (questions, weights, prices, tips) | Tune without code changes |
| Rate limiting | `slowapi` per IP (website, sign-up, login) + `limits` per API key; in memory | Protects the Jev key and budget; one free instance needs no Redis |
| Accounts & keys (v1.1) | SQLAlchemy 2 (async) + Alembic; stdlib `scrypt` passwords, SHA-256 key hashes | No extra auth service, works on SQLite and Postgres |
| Frontend | Plain HTML/CSS/JS served by FastAPI (was: Next.js + Tailwind) | Matches the portfolio sites' design exactly; one deploy, no build step |
| Charts | Hand-written SVG (was: Recharts) | No framework needed |
| Testing | pytest, httpx, recorded Jev fixtures | Deterministic CI |
| Deploy | One Docker image on Render's free tier + Neon free Postgres (was: Vercel + backend host) | Free forever as of Sept 2026 (§21) |

---

## 5. Repository structure

```
promptlint/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app, routes, CORS, rate limit
│   │   ├── schemas.py           # Pydantic request/response models
│   │   ├── jev_client.py        # builds questions, calls Jev, timeout/fallback
│   │   ├── questions.py         # loads config/questions.yaml → SDK objects
│   │   ├── tokens.py            # per-provider token counting
│   │   ├── cost.py              # price table + output-length ranges
│   │   ├── scoring.py           # composite Lint Score + verdict
│   │   └── tips.py              # failed checks → tips
│   ├── config/
│   │   ├── questions.yaml       # ALL Jev questions + criteria (single source of truth)
│   │   ├── weights.yaml         # scoring weights + thresholds
│   │   ├── prices.yaml          # model prices (with "last_updated" date)
│   │   └── tips.yaml            # tip templates
│   ├── tests/
│   │   ├── fixtures/            # recorded Jev responses
│   │   ├── test_scoring.py
│   │   ├── test_cost.py
│   │   ├── test_tips.py
│   │   └── test_api.py
│   ├── Dockerfile
│   └── pyproject.toml
├── frontend/
│   ├── app/page.tsx             # editor + report card
│   ├── components/              # ScoreGauge, Checklist, CostTable, Tips, SpecificityMeter
│   └── lib/api.ts
├── eval/
│   ├── data/prompt_pairs.jsonl  # weak/improved prompt pairs
│   ├── run_pairwise.py          # does the score rank improved > weak?
│   ├── run_first_try.py         # offline LLM ground truth for first-try success
│   └── report.ipynb             # plots: calibration, pairwise accuracy
├── docker-compose.yml
└── README.md
```

---

## 6. The Jev question set

All questions go in **one** request. The state is an object so questions can point at `prompt` explicitly:

```json
{ "prompt": "<user's prompt text>" }
```

Question-writing rules we follow:
- One judgment per question.
- Nouls phrased so that high means yes.
- Score levels describe *situations*, not degrees.
- Choices include an `other` option.
- Nothing about counting, dates, or arithmetic.

### 6.1 Nouls (yes/no probability)

| ID | Instructions |
|---|---|
| `task_clear` | Does `prompt` state clearly what the assistant is supposed to do? |
| `first_try_success` | Could a capable assistant produce a complete, usable answer to `prompt` on the first attempt, without asking questions and without the user needing to request changes? |
| `needs_clarification` | Would a careful assistant need to ask the user a clarifying question before it could answer `prompt` well? |
| `has_constraints` | Does `prompt` set any limits on the answer, such as length, scope, tone, tools, or deadline? |
| `has_output_format` | Does `prompt` say what form the answer should take, such as a list, table, code, email, or word count? |
| `has_audience` | Does `prompt` say who the answer is for or what level of expertise to write for? |
| `has_examples` | Does `prompt` include an example of the desired output or of the input to work on? |
| `conflicting` | Does `prompt` contain instructions that contradict each other? |
| `multi_task` | Does `prompt` ask for more than one distinct deliverable? |
| `needs_current_info` | Does a correct answer to `prompt` depend on recent events, live data, or information that changes over time? |
| `has_goal` (v1.1, PQS) | Does `prompt` say why the user wants this, or what they will use the answer for? |
| `has_success_criteria` (v1.1, PQS) | Does `prompt` say what a good or finished answer must achieve, so the user could check it? |

*v2 wording:* `has_constraints` and `multi_task` now carry explicit yes/no criteria. v1 counted an audience or format as a "constraint" and a list of items as "several tasks"; the criteria raised their precision from 59% → 76% and 53% → 83% (held-out checklist agreement 93.1% → 96.6%).

### 6.2 Scores (position on a described scale)

**`specificity`**: How specific is `prompt` to the user's actual situation?
0. Generic request anyone could send; no details about the user's situation or goal
1. Names the topic but not the situation, inputs, or goal
2. Describes the situation or goal, but key details are left for the assistant to guess
3. Names the exact situation, inputs, and goal; the assistant does not need to guess

**`ambiguity`**: How many reasonable interpretations does `prompt` have?
0. Only one reasonable reading
1. One main reading, with a minor detail open to interpretation
2. Two or more readings that would lead to noticeably different answers

**`context_given`**: How much background does `prompt` provide?
0. No background; the assistant must assume everything
1. Some background, but important facts are missing
2. Enough background to answer without guessing

**`expected_length`** (drives the output-cost estimate): How long would a good answer to `prompt` be?
0. One line or a single fact
1. One or two short paragraphs
2. About a page, or a medium code snippet
3. A long document, many sections, or a large program

**`complexity`** (model-tier hint; v1.1: three levels to match PQS's `low` / `medium` / `high`): How much reasoning does a good answer to `prompt` require?
0. A fact, a definition, a simple lookup, or a routine rewrite or formatting task
1. Multi-step reasoning, non-trivial code, or combining several ideas
2. A hard problem: deep debugging, proofs, system design, or subtle trade-offs

### 6.3 Choice

**`task_type`** (v1.1: PQS's nine types): What kind of task is `prompt`?
`coding` · `writing` · `analysis` · `math` · `factual_qa` · `brainstorming` · `extraction_transformation` · `conversation` · `other`

### 6.4 Example (Python SDK)

```python
from typesafe_sdk import AsyncTypeSafeClient, Noul, Score, Choice

client = AsyncTypeSafeClient()  # reads TYPESAFE_API_KEY

async def judge(prompt_text: str) -> dict:
    resp = await client.system_one(
        state={"prompt": prompt_text},
        questions=load_questions("config/questions.yaml"),
        model="jev-1.13.0",   # pin the version; log resp.model
    )
    return resp.answers
```

**Versioning rule:** pin the Jev model ID, store it in every log line, and re-run the evaluation whenever the model, questions, criteria, or weights change.

---

## 7. Scoring model

Normalize every answer to 0–1:
- Noul → probability, inverted for negative checks (`conflicting`, `needs_clarification`, `multi_task`).
- Score → `score / top_level`, inverted for `ambiguity`.

Starting weights (`weights.yaml`, tuned later using the eval set):

| Signal | Weight |
|---|---|
| first_try_success | 0.25 |
| task_clear | 0.15 |
| specificity | 0.15 |
| context_given | 0.10 |
| 1 − ambiguity | 0.10 |
| 1 − needs_clarification | 0.08 |
| has_output_format | 0.05 |
| has_constraints | 0.04 |
| has_audience | 0.03 |
| 1 − conflicting | 0.05 |

```
lint_score = round(100 × Σ weight_i × signal_i)
```

**Confidence handling:** if the average `confidence` of the Score and Choice answers is below 0.5, show a small "low confidence" badge rather than hiding the result.

**Hard caps** (applied after weighting):
- `conflicting` > 0.7 → cap the score at 60
- `task_clear` < 0.3 → cap the score at 40

`needs_current_info`, `has_examples`, `has_goal` and `has_success_criteria` don't affect the Lint Score. They appear as informational flags (goal and success criteria do feed `pqs_score`).

### 7.1 `pqs_score` (v1.1, from PQS §9.5)

Computed from the same answers, returned next to `lint_score`:

```
clarity      = mean(task_clear, 1 − ambiguity)
completeness = 1 − Σ w_c · P(missing c) / Σ w_c        c ∈ goal, context, constraints, output_format,
                                                        audience, examples, success_criteria
pqs_score    = round(100 × (0.35·clarity + 0.25·specificity + 0.25·completeness + 0.15·(1 − reiteration_risk)))
```

`reiteration_risk = 1 − first_try_success`. Component weights live in `config/pqs_scoring.yaml` and shift by task type (for example, output format matters more for extraction than for brainstorming).

---

## 8. Token and cost engine

**Input tokens (exact where possible):**
- OpenAI models: `tiktoken` with the model's encoding
- Anthropic / Gemini: the provider's token-count endpoint (cached per prompt hash); if unavailable, `len(text) / 4`, labeled "approx."

**Output tokens (estimated):** mapped from Jev's `expected_length` probabilities:

| Level | Token range |
|---|---|
| 0 | 10–60 |
| 1 | 60–300 |
| 2 | 300–900 |
| 3 | 900–3,000 |

Low and high estimates use the probability-weighted range. We show a range, never a single number.

**Cost:**
```
cost_low  = in_tokens × in_price + out_low  × out_price
cost_high = in_tokens × in_price + out_high × out_price
```

`prices.yaml` holds per-million-token prices for about 6 popular models, with a `last_updated` date shown in the UI. Prices are filled in from the providers' official pricing pages and never hard-coded in code.

---

## 9. Tip generator (no LLM)

`tips.yaml` maps each failed check to a tip. We show the 2–5 highest-impact tips, ordered by weight × shortfall.

| Trigger | Tip |
|---|---|
| `task_clear` < 0.5 | Start with one sentence saying exactly what you want done. |
| `specificity` < 0.5 | Add the specifics: your exact situation, the inputs, and what "done" looks like. |
| `context_given` < 0.5 | Give background the model can't guess: who you are, what you've tried, why you need it. |
| `ambiguity` > 0.5 | Your prompt can be read more than one way. Say which interpretation you mean. |
| `has_output_format` < 0.5 | Say what format you want: a bullet list, a table, code only, or a word limit. |
| `has_constraints` < 0.5 | Add limits such as length, tone, scope, or tools to use or avoid. |
| `has_audience` < 0.5 | Say who the answer is for (for example, "explain for a beginner"). |
| `conflicting` > 0.5 | Some instructions contradict each other. Pick one. |
| `multi_task` > 0.6 | You're asking for several things. Split them up or number them. |
| `needs_current_info` > 0.6 | This needs up-to-date info. Use a model with web search or paste the data in. |

---

## 10. API contract

**Request:** `POST /api/analyze`
```json
{
  "prompt": "string (1–20,000 chars)",
  "models": ["gpt-x", "claude-y"]
}
```

**Response:**
```json
{
  "lint_score": 68,
  "verdict": "needs_work",
  "first_try_success": 0.41,
  "specificity": { "value": 0.33, "label": "Mostly generic" },
  "checks": [
    { "id": "has_output_format", "passed": false, "probability": 0.12 },
    { "id": "conflicting", "passed": true, "probability": 0.03 }
  ],
  "task_type": { "choice": "coding", "confidence": 0.88 },
  "tier_hint": "mid",
  "tokens": { "input": 142, "input_exact": true, "output_range": [300, 900] },
  "costs": [
    { "model": "gpt-x", "low_usd": 0.0012, "high_usd": 0.0041 }
  ],
  "tips": ["Say what format you want…", "Add the specifics…"],
  "meta": { "jev_model": "jev-1.13.0", "latency_ms": 212, "low_confidence": false }
}
```

---

## 11. Frontend screens

1. **Home / editor:** a large prompt box, a model multi-select, and an "Analyze" button. A small counter shows live input tokens as you type (computed locally).
2. **Report card:**
   - Lint Score gauge and verdict badge
   - First-try success bar
   - Generic ↔ Specific slider
   - Radar chart of the dimensions (clarity, specificity, context, format, constraints)
   - Checklist with ✓ / ✗ and each probability shown on hover
   - Cost table per model (a range, with the prices' "last updated" date)
   - Fix-it tips
3. **Compare mode (v1.1):** analyze two versions of a prompt side by side and show the score delta. This is the best demo moment.
4. **About:** how it works, what Jev is, limitations, and a privacy note.

---

## 12. Evaluation (proving the scores mean something)

**Dataset:** about 200 prompt pairs (`weak`, `improved`) for the same request, across all task types. Sources: hand-written pairs, real prompts from your own chat history (anonymized), and public prompt datasets.

**Eval 1: pairwise ranking (no LLM needed)**
- Metric: % of pairs where `score(improved) > score(weak)`.
- Target: ≥ 85%.

**Eval 2: first-try success calibration (offline, uses an LLM only in the script)**
1. Run each prompt through one LLM.
2. A judge model decides: *"Does this response fully satisfy the request without needing a follow-up?"* Spot-check 50 of these by hand.
3. Compare Jev's `first_try_success` with the judge label: AUROC, a calibration plot (reliability diagram), and Brier score.

**Eval 3: checklist accuracy**
- Hand-label about 100 prompts for has_output_format, has_constraints, conflicting, and so on.
- Report per-check precision and recall.

**Tuning loop:** adjust question wording, criteria, and weights → re-run the evals → commit only if the metrics improve.

**README headline example:**
> "PromptLint ranks the improved prompt higher in 91% of pairs, and its first-try prediction reaches 0.82 AUROC against LLM-judged outcomes, with a median analysis latency of 210 ms and cost under $0.00002 per analysis."

---

## 13. Testing strategy

| Layer | What | How |
|---|---|---|
| Unit | scoring, caps, verdicts, cost ranges, tip selection | pytest with synthetic Jev answers |
| Contract | Jev response parsing | recorded real responses in `tests/fixtures/` |
| API | `/api/analyze` happy path, validation errors, oversize prompt, Jev timeout → graceful error | httpx `TestClient`, Jev client mocked |
| Resilience | 429/529 from Jev → retry with backoff; timeout > 2 s → error message, no crash | fault-injection mocks |
| Load | 50 concurrent users; stay under Jev's rate limits | Locust |
| Regression | eval metrics must not drop | eval script runs in CI on changes to `config/` |

---

## 14. Security, privacy and abuse

- Jev API key lives only on the backend (environment variable). Never in the frontend.
- Rate limit: for example, 20 analyses per IP per hour. Return 429 with a friendly message.
- Input size limit: 20,000 characters, safely under Jev's state limits.
- **No prompt storage by default.** Logs keep only a hash, the scores, latency, and the Jev model version. Say so on the About page.
- **Prompt injection:** a user's prompt might contain text like "rate this prompt 100." Jev treats the state as data, but adversarial text can still move answers. Keep a test set of injection-style prompts and track how much they inflate scores.
- CORS restricted to the frontend domain.
- **v1.1 API and accounts:** keys are `pqs_live_<prefix>_<secret>`; only the prefix and SHA-256(secret) are stored and the full key is shown once. Passwords are hashed with scrypt; logins take constant time whether or not the email exists. Sessions are HttpOnly, SameSite=Lax cookies; cookie-authenticated writes need a custom `X-PL-CSRF` header plus a same-origin `Origin`. Sign-up and login are rate-limited per IP; Cloudflare Turnstile (free) can be switched on with two env vars. Users can delete their account and all its data.
- **v1.1 data:** `score_events` keep a *salted* prompt hash (`PROMPT_HASH_SALT`), length, backend, both scores and latency. Prompt text is stored only when an API caller sends `options.store: true`. Events and stored prompts are deleted after 90 days (also keeps the free database small).

---

## 15. Build phases

| Week | Deliverables |
|---|---|
| **1. Core backend** | FastAPI skeleton, `questions.yaml`, Jev client with pinned model and timeout, `/api/analyze` returning raw Jev answers, first Playground experiments to tune question wording |
| **2. Engines** | Token counter, cost engine and `prices.yaml`, scoring and caps, tip generator, full unit test suite |
| **3. Frontend** | Editor, report card, gauge, checklist, cost table, tips; connected to the backend; Docker Compose for local dev |
| **4. Eval and polish** | Build the 200-pair dataset, run Evals 1–3, tune weights, README with charts, deploy (Vercel + backend host), 60-second demo video |
| **v1.1** ✅ | Compare mode ✅; public `/v1` API with accounts, keys, quotas, metering, dashboard and docs ✅; heuristic backend + fallback ✅; `pqs_score` ✅; free hosting plan ✅. Still open: shareable report links (opt-in storage) |
| **v2** | Developer mode (system-prompt rubric), browser extension that scores prompts inside ChatGPT or Claude before sending, plug into the Cost-aware LLM Router |

---

## 16. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Jev reads questions literally and gives surprising answers | Tune wording in the Playground first; keep all questions in one YAML file; regression-test with the eval set |
| Scores feel arbitrary to users | Show the per-dimension breakdown and probabilities; publish eval results on the About page |
| Output cost estimate is wrong | Always show a range, labeled "estimate" |
| Prices go stale | `last_updated` date in `prices.yaml`, shown in the UI; monthly check |
| Jev outage or rate limits | Retries with backoff, a friendly error, and a cached result for identical prompts (by hash) |
| Jev model update changes behavior | Pin the version, log `resp.model`, re-run evals before upgrading |
| Budget abuse | Per-IP rate limit, input size cap, optional CAPTCHA if traffic grows |

---

## 17. Definition of done (v1)

- [x] `/api/analyze` returns the full report in < 800 ms p95 (Jev p95 379 ms across eval runs)
- [x] All unit and API tests pass in CI (124 tests)
- [ ] Pairwise eval ≥ 85% ✅ (100%), first-try AUROC reported with a calibration plot ❌ (script ready, needs a working LLM key)
- [ ] Deployed at a public URL with rate limiting (config ready: `render.yaml` + Neon, §21)
- [ ] README: problem, demo GIF, architecture diagram, eval results, limitations (done except the GIF: screenshots instead)
- [ ] 60-second demo video: weak prompt → tips → improved prompt → higher score

---

## 18. Resume bullet (fill in real numbers after evaluation)

- Built PromptLint, a real-time prompt-quality analyzer that uses a calibrated decision model (TypeSafe Jev) to predict first-try LLM success, specificity, and missing context in a single ~200 ms call; validated at X% pairwise ranking accuracy and Y AUROC on a 200-pair benchmark, with deterministic per-model token and cost estimation.

---

## 19. Open questions

1. Final name check (GitHub and domain availability for "PromptLint").
2. ~~Which ~6 models go in the cost table.~~ Resolved: seven (three Claude, OpenAI and Google flagship + small).
3. Whether to add opt-in prompt storage for shareable report links in v1.1. (API-side opt-in storage exists: `options.store`.)
4. Paid plans: `dev` and `pro` exist in `plans.yaml` but are assigned by hand (`python -m app.cli set-plan`). Billing (Stripe) is not built.
5. Email verification and password reset need an email provider. Not built; decide on a free-tier provider before opening sign-ups widely.

---

## 20. Public API (v1.1, from the Prompt Quality Scorer design)

Everything under `/v1` follows `prompt-quality-scorer.md` §11–12, adapted where noted.

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /v1/score` | API key | Score one prompt (`prompt`, optional `system`, `models`, `backend`, `options`) |
| `POST /v1/score/batch` | API key | Up to 10 (free) or 50 prompts; per-item errors don't fail the batch |
| `GET /v1/pricing` | public | Price table with `last_verified` |
| `GET /v1/usage` | key or session | Requests, prompts and fallbacks per UTC day; used today / this month |
| `GET /v1/health` | public | Database, backends, pinned Jev model |
| `POST /v1/account/signup` · `login` · `logout` · `delete`, `GET /v1/account/me` | session | Self-serve accounts |
| `GET/POST /v1/keys`, `DELETE /v1/keys/{id}` | session | Create (shown once), list, revoke; max 5 active |

**Response** (PQS shape): `id`, `overall_score` (= `lint_score`), `scores {lint_score, pqs_score, verdict}`, `signals {clarity, specificity, completeness, reiteration_risk, first_try_success, task_type, complexity, missing_components, missing_detail}`, `checks`, `tokens {input, input_exact, output_p50, output_p90, output_range}`, `cost_estimates[] {usd_p50, usd_p90, usd_low, usd_high, pricing_verified}`, `suggestions[] {id, text, impact}`, `confidence`, `tier_hint`, `suggested_model`, `backend {name, version, degraded, fallback_reason}`, `latency_ms`.
Output p50/p90 come from the `expected_length` bucket CDF, interpolated inside the bucket (PQS §7).

**Errors:** `{"error": {"type", "message", "request_id"}}` with 400 / 401 / 403 / 413 / 429 / 503. Every response has `X-Request-Id`; scoring responses have `X-RateLimit-Limit|Remaining|Reset` and `X-Quota-Limit|Remaining|Period`.

**Plans** (`config/plans.yaml`): free 20 req/min per key, 1,000 prompts/day per account, batch 10 · dev 120/min, 50,000/month, batch 50 · pro 600/min, no quota. Quotas are counted in the database, so restarts don't reset them.

**Backends** (PQS §5): `jev` and `heuristic` implement one `Judge` interface returning the same typed answers. `backend: "auto"` (default) = Jev with automatic heuristic fallback on timeout, rate limit, 5xx or missing key (`degraded: true`); `"jev"` returns 503 instead; `"heuristic"` never calls Jev (prompt stays on the server).

**Baseline results** (PQS §10.2, same data):

| | Pairwise main (200) | Pairwise hard (40) | Checklist (700 labels) |
|---|---|---|---|
| Jev · lint_score | 100% | 100% | 95.9% |
| Jev · pqs_score | 100% | 100% | – |
| Heuristic · lint_score | 99.5% | 95.0% | 91.4% |
| Heuristic · pqs_score | 99.5% | 100% | – |

The pairs are too easy to separate the backends; the checklist does (the heuristic's recall on audience is 47% vs Jev's 95%). A harder, human-labeled gold set (PQS §8.3c) is the next step before any backend claims.

**Not adopted from PQS:** Phase 2 (own ModernBERT model, data pipeline, human gold set, ONNX), shadow mode, `/v1/keys` via an admin-only dashboard (keys are self-serve instead), Stripe billing, Sentry, and per-tokenizer counts beyond `o200k_base` + per-model counts.

---

## 21. Free hosting (checked Sept 2026)

| Piece | Service | Free-tier facts |
|---|---|---|
| App (API + static site) | Render free web service, Docker | 750 instance-hours/month (one service runs all month); sleeps after 15 min idle, ~1 min cold start; no persistent disk |
| Database | Neon free Postgres | 0.5 GB storage, 100 compute-hours/month, scales to zero; hitting a limit suspends compute but never deletes data |
| Rate limits | In process memory | No Redis: one instance. Render's free Key Value loses data on restart anyway |
| Captcha (optional) | Cloudflare Turnstile | Free |
| CI | GitHub Actions | Free for public repos |

Avoided: Render free Postgres (deleted 30 days after creation), Fly.io (no free tier), SQLite on Render (disk wiped on deploy).
Keeping it inside the free limits: metadata-only tables, 90-day retention with a daily prune, and quotas per account. **Not free:** TypeSafe Jev itself is usage-billed; `backend: "heuristic"` runs at zero Jev cost if ever needed. Free tiers change — re-check before relying on them.

## 22. Model routing and the quiz (v1.2)

PromptLint is also a **routing middle layer**: it recommends which model should answer each prompt, and can call it.

- **Tier needed.** Jev's three-level complexity question gives P(low), P(medium), P(high). The needed tier is the smallest one whose cumulative probability reaches the strategy's confidence: `cheapest` 0.5, `balanced` 0.75 (default), `quality` 0.9. low → small, medium → mid, high → frontier.
- **Pick.** Candidates (the price table, the caller's `candidates`, and saved custom endpoints) that reach the tier are *capable*. `cheapest`/`balanced` pick the capable model with the lowest expected cost (input tokens × price + p50 output × price); `quality` picks the cheapest model in the strongest tier needed. The fallback is the next capable model, from another provider when possible. `max_cost_usd` removes models by p90 cost.
- **Clarify first.** `likely_to_fail` and first-try < 0.4 → `action: clarify_first`; `/v1/route` then doesn't call a model unless `send_anyway: true`.
- **Savings** are measured against `baseline_model` (default: the priciest candidate). `eval/run_routing.py` routes the 480 pair prompts: balanced averages $0.000943/request, 93.4% cheaper than always GPT-6 Astra, 83.0% cheaper than the frontier average, but 564% more than always GPT-6 Luna (the smallest model). 23.5% of prompts (47.1% of weak ones) get `clarify_first`.
- **Execution** (`POST /v1/route`): keys come from `provider_keys` (per request, never stored), then saved keys (`/v1/providers`, Fernet-encrypted with `PROVIDER_KEY_SECRET`). Adapters: Anthropic (official SDK), OpenAI chat completions, Gemini `generateContent`, and any OpenAI-compatible `/chat/completions` (Ollama, Groq, OpenRouter…). Up to three models are tried in order; non-retryable errors (bad key) move on immediately. Custom endpoint URLs must be public HTTPS in production (SSRF guard). No keys, or `execute: false` → recommendation only. No streaming in v1.2.
- **Quiz** (`/quiz.html`, `/api/quiz`): five weak prompts from `config/quiz.yaml` to rewrite. Each rewrite gets the Lint Score, capped at 20 if a one-question Jev check says it no longer asks for the round's task. Grades A+ ≥ 90 … F < 50. Only runs fully judged by Jev are ranked; the leaderboard shows each nickname's best (all time or this week). 5 submissions per IP per hour.
