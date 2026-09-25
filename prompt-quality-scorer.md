# Prompt Quality Scorer (PQS)

> Design doc · v0.1 · September 2026 · Owner: Rohit Shidid
> Status: Planning

**Assumptions** (change these and the plan still holds, only the timelines/costs move):
- Goal is both a strong AI-engineer portfolio piece *and* a real, publicly usable API with its own keys.
- Compute is free-tier GPUs (Colab / Kaggle T4) or NYU HPC for training, and free/cheap hosting tiers for serving.
- ~8–10 hours/week of build time alongside coursework and TA duties.
- English prompts only for v1.

---

## 1. Summary

PQS is an API (plus a public web demo) that takes a prompt someone is about to send to an LLM and returns, **without calling an LLM**:

- how clear, specific and complete the prompt is,
- the probability the first answer will *not* solve the user's problem (they'll need to re-ask),
- which pieces of context are missing (goal, constraints, output format, …),
- exact input tokens and predicted output tokens,
- estimated cost across popular models,
- template-based suggestions for fixing the prompt.

It ships in two stages:

1. **Phase 1: Jev-powered MVP.** TypeSafe's Jev (a "System One" model that returns typed decisions and calibrated probabilities instead of text) does the judging. This path is fast to ship and validates that the product is useful.
2. **Phase 2: Own model.** A fine-tuned multi-head encoder (ModernBERT / DeBERTa-v3) trained on your own labeled dataset and served from CPU via ONNX. This becomes *your* model behind *your* API keys.

Both stages sit behind the same API, so switching backends is a config change.

---

## 2. Problem & users

**Problem.** Many LLM calls are wasted: the prompt is vague, the model guesses, and the user re-asks two or three times. That burns tokens, money and time. Nobody gives feedback on a prompt *before* it's sent.

**Users.**

| User | What they want from PQS |
|---|---|
| Developers building LLM apps | Pre-flight check on user prompts; flag vague ones and ask the user for detail before paying for an LLM call |
| Teams tracking LLM spend | Cost estimates per prompt/model; dashboards of prompt quality over time |
| Prompt-heavy individuals | Paste a prompt, get a score and fixes (the web demo) |
| The Cost-Aware LLM Router (sister project) | PQS signals (complexity, ambiguity, task type) as routing features |

---

## 3. Output specification

Every signal is either computed deterministically or predicted by the model. PQS generates no free-form text.

| Signal | Type / range | Meaning | Source |
|---|---|---|---|
| `overall_score` | int 0–100 | Weighted composite of the signals below | Deterministic formula (§9.5) |
| `clarity` | float 0–1 | Is it unambiguous what's being asked? | Model |
| `specificity` | float 0–1 | Generic ("write about AI") vs specific ("write a 300-word intro on RAG for PMs") | Model |
| `missing_components` | multi-label | Any of: `goal`, `context`, `constraints`, `output_format`, `audience`, `examples`, `success_criteria` | Model |
| `reiteration_risk` | probability 0–1 | P(user will need a follow-up because the first answer won't solve it) | Model |
| `task_type` | enum | `coding`, `writing`, `analysis`, `math`, `factual_qa`, `brainstorming`, `extraction_transformation`, `conversation`, `other` | Model |
| `complexity` | enum + probs | `low` / `medium` / `high` | Model |
| `tokens.input` | int per tokenizer | Exact input token count | Tokenizer (deterministic) |
| `tokens.output_p50`, `output_p90` | int | Predicted response length | Model (quantile heads) |
| `cost_estimates[]` | USD per model | `input_tokens × in_price + predicted_output × out_price` | Deterministic + `pricing.yaml` |
| `suggestions[]` | list of `{id, text}` | Fix-it tips chosen from a template library by the missing components | Deterministic (`suggestions.yaml`) |
| `confidence` | per signal | Calibrated probability / margin | Model |

**Scope decision (locked):** no LLM-generated rewrites or feedback. Suggestions come from a fixed, versioned template library that the flags select. This keeps the API fast, cheap and deterministic, and it means PQS can't hallucinate.

---

## 4. Scope

**In scope (v1)**
- `/v1/score` and `/v1/score/batch` endpoints with API-key auth, rate limits and usage metering
- Phase 1 Jev backend, Phase 2 own-model backend, heuristic baseline backend
- Public web demo
- Model card and eval report with real, reproducible numbers

**Out of scope / non-goals (v1)**
- Rewriting prompts with an LLM
- Non-English prompts
- Scoring multi-turn conversations (only the latest user message plus an optional system prompt)
- Guaranteeing the answer will be correct. PQS predicts *prompt-side* risk, not model quality

---

## 5. Architecture

```
                   ┌──────────────────────────────────────────────┐
  Client / Web ───▶│  FastAPI gateway                             │
  (Bearer key)     │   auth ─▶ rate limit ─▶ validate ─▶ meter    │
                   └───────────────┬──────────────────────────────┘
                                   │ ScoreRequest
                                   ▼
                   ┌──────────────────────────────────────────────┐
                   │  Scoring engine                              │
                   │   ├─ TokenCounter (tiktoken / HF tokenizers) │
                   │   ├─ Backend (pluggable, set in config):     │
                   │   │    • HeuristicBackend  (baseline)        │
                   │   │    • JevBackend        (Phase 1)         │
                   │   │    • ModelBackend      (Phase 2, ONNX)   │
                   │   ├─ Composite scorer (overall_score)        │
                   │   ├─ Cost calculator (pricing.yaml)          │
                   │   └─ Suggestion selector (suggestions.yaml)  │
                   └───────────────┬──────────────────────────────┘
                                   │ ScoreResult
                                   ▼
          Postgres (keys, usage rollups)      Redis (rate limits)
```

**Key design choice: a pluggable backend interface.**

```python
class ScoringBackend(Protocol):
    name: str
    version: str
    def score(self, prompt: str, system: str | None = None) -> RawSignals: ...
```

Benefits:
- Swap Jev → own model with one config line.
- **Shadow mode:** serve Jev results while silently running your model on the same traffic and logging agreement. This gives you a clean before/after story.
- The heuristic backend is always available as a fallback if Jev is down.

---

## 6. Phase plan

| Phase | Goal | Exit criteria |
|---|---|---|
| **0. Scaffold** | Repo, API skeleton, token counting, pricing, heuristic backend | `/v1/score` returns tokens, cost and heuristic scores behind an API key |
| **1. Jev MVP** | Jev-powered scoring + live web demo | Public demo URL; 50-prompt sanity set looks right |
| **2. Data** | Build labeled dataset + human gold set | ≥20k labeled prompts, 500-prompt gold set, judge–human agreement measured |
| **3. Own model** | Train, calibrate and export the multi-head model | Beats heuristic baseline on the gold set; p95 CPU latency within target |
| **4. Launch** | Shadow → switch, docs, model card, usage dashboard | Own model serving; eval report published |

---

## 7. Phase 1: Jev backend

Jev takes a state (the prompt) plus a list of typed questions, and returns a choice from a set you define, a rubric score, or a yes/no probability, all in one call. At launch, coverage reported input at about $0.042 per 1M tokens with output free, and 70–500 ms responses. **Verify the current pricing, request format and terms in TypeSafe's docs.** The snippet below shows the shape of the idea; it is not the real SDK syntax.

```python
# Illustrative only — check TypeSafe docs for the real request schema.
questions = [
  score("clarity",      "How unambiguous is what the user wants?", scale=(0, 10)),
  score("specificity",  "How specific vs generic is the request?", scale=(0, 10)),
  yes_no("needs_followup",
         "Would a typical assistant's first answer likely leave the user "
         "needing to clarify or re-ask?"),
  choice("task_type", ["coding","writing","analysis","math","factual_qa",
                       "brainstorming","extraction_transformation",
                       "conversation","other"]),
  choice("complexity", ["low","medium","high"]),
  yes_no("missing_goal",          "Is the end goal unstated?"),
  yes_no("missing_context",       "Is needed background context missing?"),
  yes_no("missing_constraints",   "Are constraints (length, scope, tools) missing?"),
  yes_no("missing_output_format", "Is the desired output format unspecified?"),
  yes_no("missing_audience",      "Is the target audience unstated where it matters?"),
  choice("output_length", ["<100", "100-300", "300-800", "800-2000", ">2000"]),  # tokens
]
result = jev.decide(state={"prompt": prompt, "system": system}, questions=questions)
```

**Mapping to the output spec**
- Rubric scores → normalize to 0–1.
- `needs_followup` probability → `reiteration_risk`.
- `output_length` bucket probabilities → p50/p90 via the bucket CDF (midpoint of the bucket where cumulative probability crosses 0.5 / 0.9).
- Missing-component yes/no → `missing_components` with probabilities kept as confidence.

**Question design notes**
- Keep question wording fixed and versioned (`jev_questions_v1.yaml`). A wording change is a model change, so bump the version.
- Build a 50-prompt sanity set (good/bad pairs of the same request) and check that Jev ranks the good version above the bad one. That pairwise accuracy is your first metric.

---

## 8. Data plan (Phase 2)

### 8.1 Sources

Check every license before use. Prefer permissive, well-documented datasets.

| Source | Why | Notes |
|---|---|---|
| OpenAssistant (oasst1/oasst2) | Real prompts, multi-turn trees, human ratings | Apache-2.0 |
| WildChat | Large set of real multi-turn user prompts | Verify current license and terms |
| LMSYS-Chat-1M | Huge variety of real prompts | Gated; license agreement required; check redistribution limits |
| Dolly-15k | Clean, human-written instructions (mostly "good" prompts) | CC BY-SA |
| Self-written contrast pairs | Controlled vague vs specific versions of the same request | Yours; great for eval |

**Target size:** 20–40k labeled prompts for training, 2k for validation, 2k for test, plus a separate 500-prompt **human gold set** that is never trained on.

### 8.2 Cleaning pipeline

1. English filter (fastText language ID).
2. Exact + near-duplicate removal (MinHash LSH).
3. PII scrubbing (Microsoft Presidio) before anything is stored in processed form.
4. Drop prompts < 3 tokens or > 8k tokens. Keep a long-prompt bucket for eval.
5. Stratified splits by source and task type; dedupe *across* splits.

### 8.3 Labels: three layers

**(a) Weak supervision from conversation behavior.** This is the most interesting and defensible part. In multi-turn logs, the user's *second* message tells you whether the first answer landed. Classify turn 2 into:

| Turn-2 intent | Label for turn-1 prompt |
|---|---|
| Correction / clarification / rephrase of the *same* goal ("no, I meant…", "that's not what I asked") | `reiteration = 1` |
| Thanks / acknowledgement / conversation ends with positive rating | `reiteration = 0` |
| New unrelated task | `reiteration = 0` (weak) |
| Deeper follow-up on the same topic ("now add tests") | Excluded (not a failure, not clean success) |
| Conversation ends after turn 1 with no signal | Unlabeled (user may have just left) |

Also, if the *assistant's* first turn asks a clarifying question, that's a strong signal of ambiguity (`clarity` low).

The turn-2 intent classifier is itself small: label ~1k examples by hand, then train or run a zero-shot judge on the rest.

**(b) Rubric labels from an LLM judge.** A strong LLM scores `clarity`, `specificity`, `missing_components`, `task_type` and `complexity` against a written rubric (`rubric_v1.md`). Use a fixed rubric, temperature 0, and a structured JSON output, and run it through a batch API to cut cost. **Check the judge provider's terms on using outputs to train models.** A prompt classifier isn't a competing LLM, but confirm it.

**(c) Human gold set.** 500 prompts, each labeled by you plus one other annotator (a classmate or a paid annotator) using the same rubric. Measure inter-annotator agreement (Cohen's κ for categorical signals, Spearman for scores). Then measure **judge–human agreement** on the same set. If the judge agrees with humans about as well as humans agree with each other, the judge labels are trustworthy enough to train on.

**Output-length labels.** Use real assistant response lengths from the datasets. These are specific to whatever model produced them, so predict a *model-agnostic verbosity* and apply per-model multipliers. To calibrate the multipliers, run ~300 prompts through each model in `pricing.yaml` and fit a ratio.

**Jev as a labeler?** Only if TypeSafe's terms explicitly allow training on outputs. Otherwise use Jev only as an *eval baseline*.

### 8.4 Label schema (one JSONL row)

```json
{
  "id": "oasst2-000123",
  "source": "oasst2",
  "prompt": "…",
  "system": null,
  "labels": {
    "clarity": 0.35, "specificity": 0.20,
    "missing": {"goal": 0, "context": 1, "constraints": 1, "output_format": 1,
                "audience": 0, "examples": 0, "success_criteria": 1},
    "task_type": "writing", "complexity": "medium",
    "reiteration": 1, "reiteration_source": "turn2_correction",
    "output_tokens": 612, "output_model": "unknown-gpt-class"
  },
  "label_meta": {"judge": "rubric_v1", "judge_model": "…", "human_verified": false}
}
```

---

## 9. Model (Phase 2)

### 9.1 Backbone

| Option | Why |
|---|---|
| **ModernBERT-base** (recommended) | Long context (up to 8k tokens), fast, strong on classification |
| DeBERTa-v3-base | Very strong classifier, but 512-token limit (truncate head + tail) |
| DeBERTa-v3-small / ModernBERT distilled | For the CPU-optimized serving variant |

### 9.2 Multi-task heads (shared encoder, [CLS]/mean-pooled embedding)

| Head | Output | Loss |
|---|---|---|
| `clarity`, `specificity` | sigmoid scalar | BCE on soft labels (or MSE) |
| `missing_components` | 7 sigmoids | BCE (multi-label) |
| `reiteration` | sigmoid | BCE with class weights |
| `task_type` | softmax(9) | Cross-entropy |
| `complexity` | softmax(3) or ordinal | Cross-entropy / CORAL ordinal loss |
| `output_len` | quantiles p50, p90 of log(1+tokens) | Pinball (quantile) loss |

Total loss = weighted sum. Start with hand-set weights, then try uncertainty weighting (learned per-task log-variance).

### 9.3 Training config (starting point)

```yaml
backbone: answerdotai/ModernBERT-base
max_length: 1024
batch_size: 32          # grad accumulation if memory-limited
lr: 2.0e-5
weight_decay: 0.01
epochs: 4
warmup_ratio: 0.06
precision: fp16
early_stopping: val_macro_score, patience 2
seed: [13, 42, 1337]    # report mean ± std across seeds
```

Hardware: a free T4 is enough for 20–40k short examples. Expect a training run in the hours, not days, range. Measure and record the actual time.

### 9.4 Calibration

- Temperature scaling for softmax heads, fit on validation.
- Isotonic regression for `reiteration` (it drives `overall_score`, so calibration matters).
- Report Expected Calibration Error (ECE) before and after.

### 9.5 Composite `overall_score`

```
completeness = 1 − (weighted count of missing components / weight total)
               # weights depend on task_type, e.g. output_format matters
               # more for extraction than for brainstorming (scoring.yaml)

overall = 100 × ( 0.35·clarity
                + 0.25·specificity
                + 0.25·completeness
                + 0.15·(1 − reiteration_risk) )
```

The weights live in `configs/scoring.yaml` and are documented publicly. Validate by checking that `overall` ranks the good version above the bad one in your contrast-pair set.

### 9.6 Export & serving

- Export to ONNX with Hugging Face Optimum, then apply dynamic INT8 quantization.
- Target: p95 < 150 ms on a 2-vCPU box for prompts ≤ 512 tokens (a target; measure and publish the real number).
- Publish the model on the Hugging Face Hub with a model card (data, labels, metrics, limitations).

---

## 10. Evaluation

### 10.1 Metrics

| Signal | Metric |
|---|---|
| `clarity`, `specificity`, `overall` | Spearman ρ vs human gold labels |
| `missing_components` | Per-label F1, macro-F1 |
| `reiteration_risk` | AUROC, AUPRC, ECE |
| `task_type`, `complexity` | Accuracy, macro-F1 |
| Output length | MAPE on p50; p90 coverage (should be ~90%) |
| Contrast pairs | Pairwise accuracy (good version scored higher) |
| System | p50/p95 latency, cost per 1k requests |

### 10.2 Baselines (all evaluated on the same gold + test sets)

1. **Heuristic:** length, question marks, presence of format words ("JSON", "bullet", "words"), vague-word lexicon.
2. **Jev zero-shot** (Phase 1 backend).
3. **LLM-as-judge** with the rubric (quality ceiling reference, but slow and expensive).
4. **Own model** (small and base variants).

### 10.3 Report template (fill with real numbers only)

| Backend | Spearman (overall) | Reiteration AUROC | Missing macro-F1 | Pairwise acc. | p95 latency | $/1k req |
|---|---|---|---|---|---|---|
| Heuristic | | | | | | |
| Jev | | | | | | |
| LLM judge | | | | | | |
| PQS-base (ours) | | | | | | |
| PQS-small ONNX int8 (ours) | | | | | | |

### 10.4 Eval hygiene
- Gold set is frozen and versioned (`gold_v1`); never used for training or threshold tuning.
- Report mean ± std across 3 seeds.
- Include a failure-analysis section: 20 worst errors, categorized.

---

## 11. API specification

Base URL: `https://api.<your-domain>/v1`
Auth: `Authorization: Bearer pqs_live_…`

### `POST /v1/score`

**Request**
```json
{
  "prompt": "write something about machine learning for my blog",
  "system": null,
  "models": ["claude-sonnet-5", "claude-haiku-4-5"],
  "options": {
    "include_suggestions": true,
    "include_confidence": true,
    "store": false
  }
}
```

**Response**
```json
{
  "id": "scr_01J9…",
  "overall_score": 31,
  "signals": {
    "clarity": 0.42,
    "specificity": 0.18,
    "reiteration_risk": 0.71,
    "task_type": {"label": "writing", "confidence": 0.93},
    "complexity": {"label": "medium", "probs": {"low": 0.22, "medium": 0.61, "high": 0.17}},
    "missing_components": ["audience", "constraints", "output_format", "goal"]
  },
  "tokens": {"input": {"cl100k": 10, "o200k": 10}, "output_p50": 650, "output_p90": 1400},
  "cost_estimates": [
    {"model": "claude-sonnet-5", "usd_p50": 0.0000, "usd_p90": 0.0000, "pricing_verified": "2026-09-25"}
  ],
  "suggestions": [
    {"id": "add_audience", "text": "Say who the post is for (e.g., beginners, hiring managers, ML engineers)."},
    {"id": "add_length",   "text": "Give a target length (e.g., ~800 words)."},
    {"id": "add_angle",    "text": "Name the specific ML topic or angle you want covered."}
  ],
  "backend": {"name": "pqs-model", "version": "1.0.0"},
  "latency_ms": 38
}
```
(Cost values above are placeholders; real values come from `pricing.yaml`.)

### Other endpoints

| Endpoint | Purpose |
|---|---|
| `POST /v1/score/batch` | Up to 50 prompts per call |
| `GET /v1/pricing` | Current pricing table + `last_verified` dates |
| `GET /v1/usage` | Caller's usage by day |
| `GET /v1/health` | Liveness/readiness; backend + model version |
| `POST /v1/keys` / `DELETE /v1/keys/{id}` | Dashboard-only key management (session auth, not API key) |

### Errors

```json
{"error": {"type": "rate_limit_exceeded", "message": "…", "request_id": "req_…"}}
```
Error codes: `400` bad input · `401` bad/missing key · `413` prompt too long (> 32k chars) · `429` rate limited · `503` backend unavailable (falls back to heuristic, with `"degraded": true` in the response).

Rate-limit headers on every response: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`.

---

## 12. Serving, keys & infrastructure

| Concern | Choice |
|---|---|
| API | FastAPI + Uvicorn, Pydantic v2 schemas |
| Inference | ONNX Runtime (CPU) in-process; model loaded once at startup |
| Tokenizers | `tiktoken` (OpenAI encodings); HF `tokenizers` for open models; provider token-count endpoints where exact counts need a network call (cache results, mark approximations) |
| DB | Postgres (Neon or Supabase free tier): `users`, `api_keys`, `usage_daily`, `score_events` (metadata only) |
| Rate limiting | Redis (Upstash free tier), sliding-window per key + per-IP for the demo |
| Migrations | Alembic |
| Hosting | Docker image → Fly.io / Render / Hugging Face Spaces / Modal (pick based on current free tiers) |
| Frontend | Next.js (or plain HTML + JS) on Vercel |
| Monitoring | Structured JSON logs, Sentry (errors), simple Prometheus metrics or a `/metrics` endpoint |

**API key design**
- Format: `pqs_live_<8-char public prefix>_<32-byte random secret, base62>`.
- Store only `prefix` + `SHA-256(secret)`; show the full key once at creation.
- Lookup by prefix, constant-time compare of hashes.
- Plans in config (example): `free`: 1,000 requests/day, 20 rpm · `dev`: 50k/month · `pro`: custom. Billing (Stripe metered) is optional and comes later.

**Privacy**
- Prompt text is **not stored by default**. Logs keep a salted hash, length and signals only.
- `store: true` is opt-in, for callers who want to contribute data to improve the model.
- The web demo shows a clear notice and a server-side key; the key never reaches the browser. Add a captcha and per-IP limits.

---

## 13. Web demo

- One page: prompt box → score gauge, signal bars, missing-component chips, token/cost table, suggestions list.
- A "Try a bad prompt" button cycles through contrast pairs, so visitors instantly see the difference.
- Backend toggle (Heuristic / Jev / PQS model) so viewers can compare. This is great in interviews.
- Link to docs, model card and eval report.

---

## 14. Repository structure

```
prompt-quality-scorer/
├── README.md                    # pitch, demo link, quickstart, eval table
├── MODEL_CARD.md
├── pyproject.toml               # uv / poetry
├── Makefile                     # make data | train | eval | serve | test
├── docker/
│   ├── Dockerfile.api
│   └── docker-compose.yml       # api + postgres + redis for local dev
├── configs/
│   ├── pricing.yaml             # model prices + last_verified dates
│   ├── scoring.yaml             # composite weights, task-type weights
│   ├── suggestions.yaml         # template library, keyed by missing component/task type
│   ├── jev_questions_v1.yaml
│   ├── rubric_v1.md             # labeling rubric for judge + humans
│   └── train/modernbert_base.yaml
├── data/                        # gitignored
│   ├── raw/  interim/  processed/
│   └── gold/gold_v1.jsonl       # tracked via DVC or HF dataset, not git
├── src/pqs/
│   ├── api/
│   │   ├── main.py
│   │   ├── routes/{score,usage,keys,health}.py
│   │   ├── auth.py  ratelimit.py  metering.py  schemas.py  errors.py
│   ├── scoring/
│   │   ├── engine.py            # orchestrates backend + tokens + cost + suggestions
│   │   ├── backends/{base,heuristic,jev,onnx_model}.py
│   │   ├── composite.py
│   │   └── suggestions.py
│   ├── tokens/{counters,cost}.py
│   ├── data/
│   │   ├── ingest_{oasst,wildchat,lmsys,dolly}.py
│   │   ├── clean.py  dedup.py  pii.py
│   │   ├── weak_labels.py       # turn-2 intent → reiteration labels
│   │   ├── judge_labeling.py    # batch LLM-judge with rubric
│   │   └── splits.py
│   ├── training/
│   │   ├── model.py  heads.py  losses.py
│   │   ├── train.py  calibrate.py  export_onnx.py
│   ├── eval/
│   │   ├── metrics.py  baselines.py  run_eval.py  report.py
│   └── db/{models.py, session.py, migrations/}
├── web/                         # demo frontend
├── notebooks/
│   ├── 01_eda.ipynb
│   ├── 02_weak_label_audit.ipynb
│   └── 03_error_analysis.ipynb
├── tests/
│   ├── unit/                    # composite math, cost calc, key hashing, suggestions
│   ├── integration/             # API with test DB/Redis, backend fallback
│   └── regression/              # golden prompts → expected score ranges
└── .github/workflows/
    ├── ci.yml                   # lint (ruff), types (mypy), tests
    ├── eval.yml                 # runs eval on a fixed small set on PRs touching the model
    └── deploy.yml
```

---

## 15. Testing & CI

- **Unit:** composite scoring math, cost calculator, suggestion selection, key hashing/verification, rate limiter.
- **Integration:** full request through the API with Postgres/Redis in docker-compose; Jev backend mocked with recorded fixtures; fallback to the heuristic when a backend errors.
- **Regression:** ~50 golden prompts with expected score *ranges*; CI fails if a model change moves them out of range.
- **Load:** k6 or Locust; publish p50/p95 latency at a fixed RPS.
- **Pricing freshness:** CI warns if any `pricing.yaml` entry's `last_verified` is older than 30 days.

---

## 16. Milestones (~8–10 hrs/week)

| Week | Deliverable |
|---|---|
| 1 | Repo scaffold, token counting, `pricing.yaml`, heuristic backend, API skeleton with key auth + rate limits |
| 2 | Jev backend + question set v1, sanity contrast set, **web demo live (Phase 1 ships)** |
| 3 | Data ingestion, cleaning, dedup, PII scrubbing; EDA notebook |
| 4 | Weak labels (turn-2 intent), rubric v1, judge labeling run; start gold set |
| 5 | Finish gold set (500, double-annotated); judge–human agreement report |
| 6 | Train v1 multi-head model; first eval vs baselines |
| 7 | Calibration, error analysis, v2 training; ONNX export + latency benchmarks |
| 8 | Shadow mode on the live API; switch default backend if it wins |
| 9 | Usage dashboard, docs page, model card, eval report |
| 10 | Launch write-up (blog / LinkedIn), README polish, HF Hub release |

---

## 17. Cost estimate

| Item | Rough cost |
|---|---|
| Jev scoring (Phase 1) | Negligible at launch pricing (input priced per *billion*-scale tokens; output free). Verify current pricing |
| LLM-judge labeling (20–40k prompts, batch API, small/mid model) | Roughly single to low double-digit USD; compute exactly as `N × (avg_in × in_price + avg_out × out_price)` before running |
| Output-length multiplier calibration (~300 prompts × N models) | A few USD |
| Training | $0 on Colab/Kaggle/NYU HPC |
| Hosting | $0–10/month on free/hobby tiers |

---

## 18. Risks & mitigations

| Risk | Mitigation |
|---|---|
| LLM-judge label noise / bias | Human gold set; report judge–human agreement; train on soft labels; audit notebook |
| Weak labels are noisy (turn 2 isn't always about failure) | Hand-label 1k turn-2 intents; exclude ambiguous cases; report label precision on a sample |
| Distribution shift (chat prompts ≠ agent/coding-tool prompts) | Separate eval slices by source; collect opt-in `store: true` data |
| Goodhart: people write to please the scorer | Keep scores tied to measurable outcomes (reiteration); don't reward length alone; contrast-pair eval |
| Provider tokenizers not all public | Use provider count endpoints where available; label other counts `"approximate": true` |
| Pricing goes stale | `last_verified` per entry + CI freshness warning |
| Jev early access / availability / terms | Pluggable backends; heuristic fallback; don't train on Jev outputs unless terms allow |
| Abuse of the free demo | Server-side key, captcha, per-IP limits, max prompt length |

---

## 19. Portfolio framing

What makes this interview-survivable:
- **A real labeling strategy**: weak supervision from user behavior + rubric judge + human gold set with measured agreement.
- **Calibrated probabilities**, with ECE reported rather than just accuracy.
- **Honest baselines**: heuristic, Jev, and LLM judge, all on the same frozen gold set.
- **Production concerns handled**: key hashing, rate limiting, privacy-by-default, fallback backends, shadow deployment.
- **Numbers you can defend**: every metric in the README comes from `make eval` on the frozen set, mean ± std across seeds. No invented figures.

---

## 20. Open questions

1. Should the free tier be public sign-up, or invite-only at launch?
2. Do we score the system prompt separately (useful for app developers), or only as context?
3. Which models go in `pricing.yaml` v1 (suggest 6–8 across Anthropic, OpenAI, Google, and one open-weights host)?
4. Should PQS be a standalone repo, or live in a monorepo with the Cost-Aware LLM Router sharing an `apikit` package for auth, keys, rate limits and metering?
