# PromptLint — Build Plan

> Lint your prompt before you send it.

PromptLint is a web app where a user pastes a prompt, picks a target model, and instantly gets a report card: how good the prompt is, whether an LLM is likely to get it right on the first try, how generic or specific it is, what's missing, and how many tokens and dollars it will cost.

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

---

## 2. Features (v1)

1. **Lint Score (0–100)** with a verdict: *Ready to send* (≥ 75), *Needs work* (50–74), *Likely to fail* (< 50).
2. **First-try success prediction.** The probability that an LLM understands the task and gives an answer that won't need another round.
3. **Generic ↔ Specific meter.**
4. **Checklist:** task clarity, context, constraints, output format, audience, examples, conflicting instructions, multiple tasks, needs current info.
5. **Tokens and cost:** exact input tokens, an estimated output-token range, and a cost range for each selected model.
6. **Fix-it tips:** 2–5 concrete suggestions generated from the failed checks.
7. **Model-tier hint:** "a small model is enough" vs "needs a frontier model" (reuses the Cost-aware LLM Router logic).

---

## 3. Architecture

```
┌──────────────────────┐        POST /api/analyze        ┌─────────────────────────────┐
│ Frontend (Next.js)   │ ──────────────────────────────▶ │ Backend (FastAPI)           │
│ - prompt editor      │                                 │                             │
│ - model picker       │ ◀────────────────────────────── │ 1. validate + size limits   │
│ - report card UI     │        JSON report              │ 2. Jev: ONE call, all Qs    │──▶ TypeSafe API
└──────────────────────┘                                 │ 3. token counter            │
                                                         │ 4. cost engine (price table)│
                                                         │ 5. scorer (weights config)  │
                                                         │ 6. tip generator (rules)    │
                                                         │ 7. rate limiter + logging   │
                                                         └─────────────────────────────┘
```

The request path is one Jev call (about 100–500 ms) plus local computation. There's no second model hop.

---

## 4. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Backend | Python 3.11, FastAPI, Pydantic | Known stack, typed request/response models |
| Jev client | `typesafe-sdk` (`TypeSafeClient` / `AsyncTypeSafeClient`) | Official SDK with built-in retries |
| Tokenizers | `tiktoken` (OpenAI); provider token-count endpoints (Anthropic, Gemini); char/4 fallback | Exact where possible, labeled "approx." otherwise |
| Config | YAML files (questions, weights, prices, tips) | Tune without code changes |
| Rate limiting | `slowapi` (+ Redis in prod) | Protects the Jev key and budget |
| Frontend | Next.js + Tailwind | Fast to build, deploys to Vercel |
| Charts | Recharts | Score gauge, radar of dimensions |
| Testing | pytest, httpx, recorded Jev fixtures | Deterministic CI |
| Deploy | Vercel (frontend), Render / Fly.io / AWS App Runner (backend), Docker | Cheap, simple |

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

**`complexity`** (model-tier hint): How much reasoning does a good answer to `prompt` require?
0. A fact, definition, or simple lookup
1. A short explanation or a routine rewrite or format task
2. Multi-step reasoning, non-trivial code, or combining several ideas
3. Hard problem: deep debugging, proofs, system design, or subtle trade-offs

### 6.3 Choice

**`task_type`**: What kind of task is `prompt`?
`coding` · `writing` · `factual` · `analysis` · `math_reasoning` · `creative` · `other`

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

`needs_current_info` and `has_examples` don't affect the score. They appear as informational flags.

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

---

## 15. Build phases

| Week | Deliverables |
|---|---|
| **1. Core backend** | FastAPI skeleton, `questions.yaml`, Jev client with pinned model and timeout, `/api/analyze` returning raw Jev answers, first Playground experiments to tune question wording |
| **2. Engines** | Token counter, cost engine and `prices.yaml`, scoring and caps, tip generator, full unit test suite |
| **3. Frontend** | Editor, report card, gauge, checklist, cost table, tips; connected to the backend; Docker Compose for local dev |
| **4. Eval and polish** | Build the 200-pair dataset, run Evals 1–3, tune weights, README with charts, deploy (Vercel + backend host), 60-second demo video |
| **v1.1** | Compare mode, shareable report links (opt-in storage) |
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

- [ ] `/api/analyze` returns the full report in < 800 ms p95
- [ ] All unit and API tests pass in CI
- [ ] Pairwise eval ≥ 85%, first-try AUROC reported with a calibration plot
- [ ] Deployed at a public URL with rate limiting
- [ ] README: problem, demo GIF, architecture diagram, eval results, limitations
- [ ] 60-second demo video: weak prompt → tips → improved prompt → higher score

---

## 18. Resume bullet (fill in real numbers after evaluation)

- Built PromptLint, a real-time prompt-quality analyzer that uses a calibrated decision model (TypeSafe Jev) to predict first-try LLM success, specificity, and missing context in a single ~200 ms call; validated at X% pairwise ranking accuracy and Y AUROC on a 200-pair benchmark, with deterministic per-model token and cost estimation.

---

## 19. Open questions

1. Final name check (GitHub and domain availability for "PromptLint").
2. Which ~6 models go in the cost table.
3. Whether to add opt-in prompt storage for shareable report links in v1.1.
