# How PromptLint is built

This file explains the whole project in plain words: what each part does, how a request travels through the system, and where to find things. Read it top to bottom once, then use it as a map.

## The idea in one paragraph

People send AI chatbots vague prompts like "write me a poem", get a so-so answer, and have to ask again. PromptLint checks a prompt **before** it's sent. It asks an AI judge called **Jev** (made by TypeSafe) eighteen quick questions about the prompt: *Is the task clear? Does it say who it's for? Is there a word limit?* Jev answers each with a probability (for example "91% yes"). PromptLint then turns those answers into scores, a checklist, tips and cost estimates using ordinary code, with no chatbot involved. You can use it on the website or from your own code through the API.

## The big picture

```
          Browser                                   Your app / script
   (website pages, tester)                      (with an API key: pqs_live_…)
             │                                               │
             ▼                                               ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │                         PromptLint server                            │
 │                     (Python, FastAPI, one program)                   │
 │                                                                      │
 │  1. Who are you?        website visitor, API key, or signed-in user  │
 │  2. Are you allowed?    rate limits and daily quotas                 │
 │  3. Ask the judge  ───────────────────────────────▶  Jev (TypeSafe)  │
 │       (if Jev is down, simple keyword rules answer instead)          │
 │  4. Do the math         scores, checklist, tips, token count, cost   │
 │  5. Record usage        counts only, never the prompt text           │
 └──────────────────────────────────┬───────────────────────────────────┘
                                    │
                                    ▼
                   Database: accounts, key fingerprints, usage
                   (a file on your computer; Neon Postgres online)
```

The same server also sends the website's pages (HTML, CSS, JavaScript) to the browser, so there's only one thing to run and host.

## What happens when you check a prompt

Following "write me a poem" from start to finish:

1. **You click "Check".** The page sends the prompt to the server (`/api/analyze` on the website, `/v1/score` through the API with your key).
2. **The server checks who you are.** On the website it only looks at your IP address, to limit how often you can check. Through the API it looks up your key's fingerprint in the database.
3. **The server checks your limits.** For example, 20 checks a minute and 1,000 a day on the free plan.
4. **One call to Jev.** The prompt plus all eighteen questions go to Jev in a single request (about 0.2 seconds). The questions are written in `backend/config/questions.yaml`.
5. **The math.** Jev's answers become:
   - **Lint Score** (0–100): a weighted mix where "will it work first time?" counts most.
   - **PQS score** (0–100): a second formula that weighs clarity, specificity and completeness.
   - **Checklist**: goal, background, format, audience, limits and more, each passed or failed.
   - **Tips**: fixed sentences chosen by which checks failed, most helpful first.
   - **Cost**: prompt length in tokens × each AI company's price, plus an estimate of the answer's length.
6. **Recording.** For API calls, the server adds one to your usage count and saves a scrambled fingerprint (hash) of the prompt, the scores and the time taken. It does **not** save the prompt itself unless you ask it to.
7. **The answer comes back** as JSON (API) or as the report card (website).

If Jev is slow or down, step 4 uses the **heuristic** instead: simple keyword rules that run instantly on the server. The answer then says `"degraded": true`, so you know it's rougher.

## Folder map

```
Jev Ai/
├── README.md             Start here: what it is, results, how to run, API, deploy
├── STRUCTURE.md          This file
├── TODO.md               What's left to do
├── PromptLint.md         The original build plan (+ §20–21: the public API and free hosting)
├── prompt-quality-scorer.md   The design doc the public API follows
│
├── backend/              The server (Python)
│   ├── app/              The code
│   ├── config/           Settings you can change without touching code
│   ├── migrations/       Database table definitions, versioned
│   ├── tests/            Automated tests
│   ├── scripts/          Helper scripts
│   └── Dockerfile        Recipe for the container that runs online
│
├── frontend/             The website (plain HTML, CSS, JavaScript, no build step)
├── eval/                 Accuracy tests: do the scores mean anything?
├── docs/                 Screenshots used in the README
│
├── docker-compose.yml    Run the server + a database on your computer with Docker
├── render.yaml           Settings for free hosting on Render
├── .github/workflows/    GitHub robots: ci.yml (tests on every push), keep-awake.yml (pings the site)
├── .env.example          List of secret settings (copy to .env and fill in)
└── ruff.toml             Code-style rules
```

### `backend/app/`: the server code

| File | What it does, in plain words |
|---|---|
| `main.py` | Starts everything. Connects the pieces, serves the website pages, handles the website's `/api/...` requests and the Jev playground. |
| `api_v1.py` | The public API: `/v1/score`, batch, pricing, usage, health, plus sign-up, log-in and key management. |
| `auth.py` | Checks who's calling: API keys, log-in cookies, and protection against other websites acting as you. |
| `analyze.py` | The heart. Asks the judge, runs the math, and builds the report for the website or the API. |
| `backends.py` | The two judges: **Jev** and the **heuristic** (keyword rules), plus the automatic switch to the heuristic when Jev fails. |
| `jev_client.py` | Talks to TypeSafe's Jev: sends the questions, retries on hiccups, gives up politely if it's too slow. |
| `questions.py` | Turns `questions.yaml` into the format Jev expects. |
| `scoring.py` | The formulas: Lint Score, PQS score, verdicts, answer-length estimates. |
| `cost.py` | Money math: tokens × price. |
| `tokens.py` | Counts tokens (the chunks AI models read text in). |
| `tips.py` | Picks the fix-it tips. |
| `db.py` | The database tables and helpers (users, keys, usage, events). |
| `security.py` | Makes API keys and hashes passwords and keys, so the database never holds the real secrets. |
| `schemas.py` | The exact shape of every request and response. |
| `settings.py` | Reads settings like `TYPESAFE_API_KEY` and `DATABASE_URL` from the environment or `.env`. |
| `config.py` | Loads and double-checks the YAML files in `config/`. |
| `cli.py` | Admin commands: list users, change someone's plan, disable an account. |

### `backend/config/`: change behavior without code

| File | Controls |
|---|---|
| `questions.yaml` | The eighteen questions asked to Jev, word for word. Changing wording changes results, so re-run the accuracy tests afterwards. |
| `weights.yaml` | How the Lint Score is weighted, the score caps, and the verdict cut-offs (75 = ready, 50 = needs work). |
| `pqs_scoring.yaml` | How the PQS score is weighted, including by task type. |
| `tips.yaml` | Every tip's text, ID and when it appears. |
| `prices.yaml` | AI model prices (with the date checked) and answer-length ranges. |
| `plans.yaml` | Free, dev and pro plans: checks per minute, per day and per batch. |

### `frontend/`: the website

| File | Page |
|---|---|
| `index.html` | The landing page: what PromptLint is, a replayed demo, accuracy numbers, API intro. |
| `app.html` | The analyzer: paste a prompt, get the report card. Has compare mode and the Jev/heuristic switch. |
| `account.html` | Sign up, log in, create and revoke API keys, see usage. |
| `docs.html` | API reference for developers, with a real example response. |
| `playground/` | A raw Jev playground: send any questions to Jev and see its answers. |
| `assets/` | Shared styling (`site.css`, `report.css`), the report card drawing (`report.js`), page logic (`app.js`, `landing.js`, `account.js`), the icon, and generated data (`demo-data.js`, `api-example.json`, `eval-summary.json`). |

The friendly **API tester** you use lives outside this folder, in `../promptlint api testing/index.html`. It calls `/v1/score` with your key and explains the answer in plain words (see TODO.md about bringing it in).

### `backend/tests/`: automated checks (128 of them)

Run with `cd backend && .venv/bin/pytest -q`. No internet or secrets needed.

- `test_scoring.py`, `test_cost.py`, `test_tips.py`, `test_pqs.py`: the math and the security helpers.
- `test_contract.py`: replays **real recorded Jev answers** (`fixtures/`) to catch changes in Jev's format.
- `test_api.py`: the website's endpoints, fallbacks and retries.
- `test_v1.py`: the whole public-API journey (sign up → key → score → usage → revoke), limits, and safety checks.
- `load/locustfile.py`: an optional stress test with 50 simulated users.

### `eval/`: proving the scores mean something

| Test | Question it answers | Result |
|---|---|---|
| `run_pairwise.py` | If you improve a prompt, does its score go up? (240 before/after pairs) | 100% with Jev |
| `run_checklist.py` | Do the checks agree with a person's labels? (100 prompts × 7 checks) | 95.9% with Jev, 91.4% with keyword rules |
| `run_injection.py` | Can someone cheat by writing "rate this 100" in the prompt? | +3 points on average, never "ready" |
| `run_first_try.py` | Does "chance it works first time" match reality? | Not run yet (needs an LLM key) |

Results are saved in `eval/results/`. `build_report.py` turns them into charts in `report.ipynb`, and the landing page shows the headline numbers.

## Where data lives

| Table | Holds | Never holds |
|---|---|---|
| `users` | Email, scrambled (hashed) password, plan | The real password |
| `sessions` | Which browser is logged in (fingerprint only) | The actual cookie value |
| `api_keys` | Key name, first 8 characters, fingerprint of the rest | The full key (shown once, then gone) |
| `usage_daily` | Checks per key per day | Prompts |
| `score_events` | Scrambled prompt fingerprint, length, scores, time taken (deleted after 90 days) | The prompt text |
| `stored_prompts` | Prompt text, **only** if the caller sent `store: true` (deleted after 90 days) | n/a |

On your computer the database is one file: `backend/data/promptlint.db`. Online it's a free Neon Postgres database. The tables are created automatically on start-up from `backend/migrations/`.

## Secrets and settings

Kept in `.env` on your computer and in Render's dashboard online, never in the code:

- `TYPESAFE_API_KEY`: lets the server ask Jev (required for AI checks).
- `DATABASE_URL`: where the database is (online: the Neon address).
- `PROMPT_HASH_SALT`: a random value that makes prompt fingerprints impossible to reverse (Render creates it).
- `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY`: optional free captcha on sign-up.

## Running it

- **On your computer:** `cd backend && .venv/bin/uvicorn app.main:app --port 8787`, then open http://localhost:8787.
- **With Docker** (server + real database): `docker compose up -d --build`.
- **Online, free:** Render runs the Docker container, Neon holds the database. The steps are in the README under "Deploy (free)".
- **Keeping it awake:** `.github/workflows/keep-awake.yml` is a free GitHub robot that visits `/api/health` every 10 minutes, so the free server never falls asleep and visitors don't wait. It uses that page on purpose because it doesn't touch the database, which can stay asleep and save its free hours.

## Words you'll see

| Word | Meaning |
|---|---|
| **Prompt** | The message you send to an AI chatbot. |
| **Jev** | TypeSafe's AI "judge". It doesn't write text; it answers yes/no and rating questions with probabilities. |
| **Heuristic** | Simple keyword rules used when Jev isn't available, or when you choose them. |
| **Degraded** | The answer came from the heuristic because Jev failed. |
| **Noul / Score / Choice** | Jev's three question types: yes/no, a rating on a scale, and pick-one-option. |
| **Token** | The chunk of text AI models read and charge by (about ¾ of a word). |
| **Lint Score** | PromptLint's main 0–100 score. "Lint" means checking something for mistakes before using it. |
| **PQS score** | A second 0–100 score from the Prompt Quality Scorer design. |
| **API key** | A secret password for programs (`pqs_live_…`). |
| **Rate limit / quota** | How many checks you may run per minute / per day. |
| **Hash / fingerprint** | A scrambled version of something that can be matched but not turned back into the original. |
