# To-do

What's left to do on PromptLint, in plain words. Top of each list = do first.
Last updated: 25 September 2026.

## Before sharing it with the world

These matter because once it's public, strangers can sign up and every check they run costs you a little money on TypeSafe (the Jev AI judge).

- [ ] **Fix the Jev key on Render.** The live site's `TYPESAFE_API_KEY` is being rejected (every check falls back to simple rules). Paste a fresh key from TypeSafe into Render → Environment.
- [ ] **Check `PROVIDER_KEY_SECRET` is set on Render.** New Blueprint deploys generate it; an existing service needs it added by hand (any long random string). Without it, people can't save LLM keys on their account page.
- [ ] **Put it online.** Follow "Deploy (free)" in the README: Neon (free database) + Render (free hosting).
- [ ] **Turn on the captcha for sign-ups.** Without it, a bot could create thousands of accounts. It's free: create a Cloudflare Turnstile widget and set `TURNSTILE_SITE_KEY` and `TURNSTILE_SECRET_KEY` on Render.
- [ ] **Decide how much Jev spending you're OK with.** Each free account gets 1,000 checks a day. With many friends, that adds up. Lower `daily` in `backend/config/plans.yaml` (for example to 100), and set a spending alert in your TypeSafe dashboard if it has one.
- [ ] **Revoke the test key that was shared in chat** (`pqs_live_d1gasQeF_…`) on your local account page. It only works locally, but it's good practice.
- [ ] **Add a short "Terms & privacy" page.** One paragraph: what's stored (email, hashed password, usage counts, no prompts unless someone opts in), that prompts are sent to TypeSafe for checking, and how to delete your account.

## Soon

- [ ] **Watch the quiz leaderboard for junk nicknames.** There's no moderation yet. Deleting a row needs the database console (`quiz_results` table).
- [ ] **Measure routing quality, not just cost.** The savings numbers assume the cheaper model is good enough. Run the "first try" test per model to check that the router's picks actually answer well.
- [ ] **Streaming answers from `/v1/route`.** Right now it waits for the full answer. Streaming would feel faster for long answers.

- [ ] **Email check and password reset.** Right now anyone can sign up with a made-up email, and a forgotten password means a lost account. This needs an email-sending service with a free tier (compare Resend, Brevo, Mailgun and check their current free limits).
- [ ] **Make the two output-length numbers agree.** The API's `output_range` top can be lower than `output_p90`, which confuses people. Compute both from the same distribution.
- [ ] **Run the "first try" accuracy test.** The script is ready (`eval/run_first_try.py`), but the Groq key on this computer is invalid. Needs a working Groq or Anthropic key.
- [ ] **Get a human to check the test data.** The example prompts and their labels in `eval/data/` were written with an AI's help. Have a person spot-check them (and ideally write a harder set) before quoting the accuracy numbers.
- [ ] **A tiny admin page.** Changing someone's plan or disabling an account currently needs the command line (`python -m app.cli …`).

## Later

- [ ] **Shareable report links.** "Send this report to a friend" needs saving reports (opt-in).
- [ ] **Paid plans.** The `dev` and `pro` plans exist but are given out by hand. Charging for them needs Stripe or similar.
- [ ] **Better checks for system prompts** (the instructions app builders write), and a browser extension that checks prompts inside ChatGPT or Claude.
- [ ] **Our own model** (Phase 2 of `prompt-quality-scorer.md`): collect and label real prompts, train a small model, and compare it with Jev. This is a multi-week project.

## Done (for reference)

- Website: landing page, analyzer (single and compare), account page, API docs, API tester, Jev playground.
- Public API with sign-up, API keys, daily limits, usage tracking, and a fallback when Jev is down.
- Two scores (Lint and PQS), eleven checks, cost estimates on seven models, fix-it tips.
- Model router: every check says which AI model to use (cheapest / balanced / quality), with savings versus always using one model. `/v1/route` can also call that model with the user's own keys (OpenAI, Anthropic, Gemini, or any OpenAI-compatible endpoint like Ollama), with a backup if it fails.
- Savings stats and a calculator on the home page, measured on 480 test prompts.
- Prompt quiz with grades and a leaderboard.
- Task-fit routing (Claude for writing and coding by default) and per-key routing stats with money saved on the account page.
- 180 automated tests; accuracy tests on 240 prompt pairs and 100 labeled prompts.
- Docker setup and a free hosting plan (Render + Neon).
