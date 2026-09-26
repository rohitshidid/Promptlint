# To-do

What's left to do on PromptLint, in plain words. Top of each list = do first.
Last updated: 26 September 2026.

## Right now

PromptLint is live at https://promptlint.onrender.com and is **completely free**. Every check anyone runs costs you a little on TypeSafe (the Jev AI judge), so these come first.

- [ ] **Check the Jev key on Render works.** If the live `TYPESAFE_API_KEY` is rejected, every check falls back to simple rules and quiz runs aren't ranked. Paste a fresh key from TypeSafe into Render → Environment, then open `/v1/health` and look for `"last_error": null`.
- [ ] **Check `PROVIDER_KEY_SECRET` is set on Render.** Without it, people can't save LLM keys on their account page. Any long random string; never change it once keys are saved.
- [ ] **Turn on the captcha for sign-ups.** Without it, a bot could create thousands of free accounts. It's free: create a Cloudflare Turnstile widget and set `TURNSTILE_SITE_KEY` and `TURNSTILE_SECRET_KEY` on Render.
- [ ] **Watch Jev spending.** The free plan is 800 prompts a day per account and 10 requests a minute per key (`backend/config/plans.yaml`). Set a spending alert in your TypeSafe dashboard if it has one.
- [ ] **Add a short "Terms & privacy" page.** What's stored (email, hashed password, usage counts, which model answered and its cost, no prompts unless someone opts in), that prompts go to TypeSafe for checking, and how to delete your account.

## Soon

- [ ] **Raise the free limits.** The site promises limits will go up "on a rolling basis". When capacity allows, raise `daily` and `rpm` for the free plan in `backend/config/plans.yaml`, then update the numbers on the landing page (Pricing section and API section), `docs.html` (Limits), the account page sign-up perks, the tester's error messages, the README and `PromptLint.md` §20.
- [ ] **Answer limit requests.** The Pricing section sends people who need more to GitHub issues. Give them the `dev` plan with `python -m app.cli set-plan <email> dev` (still free).
- [ ] **Measure routing quality, not just cost.** The savings numbers assume the cheaper model is good enough. Run the "first try" test per model to check that the router's picks actually answer well.
- [ ] **Watch the quiz leaderboard for junk nicknames.** There's no moderation yet. Delete rows in the Neon SQL editor (`quiz_results` table).
- [ ] **Email check and password reset.** Right now anyone can sign up with a made-up email, and a forgotten password means a lost account. This needs an email-sending service with a free tier (compare Resend, Brevo, Mailgun).
- [ ] **Streaming answers from `/v1/route`.** Right now it waits for the full answer. Streaming would feel faster for long answers.
- [ ] **Make the two output-length numbers agree.** The API's `output_range` top can be lower than `output_p90`. Compute both from the same distribution.
- [ ] **Run the "first try" accuracy test.** The script is ready (`eval/run_first_try.py`) but needs a working Groq or Anthropic key.
- [ ] **Get a human to check the test data.** The prompts and labels in `eval/data/` were written with an AI's help. Have a person spot-check them before quoting the accuracy numbers.
- [ ] **A tiny admin page.** Changing someone's plan or disabling an account currently needs the command line (`python -m app.cli …`).

## Later

- [ ] **Shareable report links.** "Send this report to a friend" needs saving reports (opt-in).
- [ ] **Better checks for system prompts** (the instructions app builders write), and a browser extension that checks prompts inside ChatGPT or Claude.
- [ ] **Our own model** (Phase 2 of `prompt-quality-scorer.md`): collect and label real prompts, train a small model, and compare it with Jev. A multi-week project.

## Done (for reference)

- **Positioning:** the site sells PromptLint as an LLM router that saves money, with prompt linting built in. Completely free, with per-person limits for now. All examples, amounts and places are US (New York) and USD.
- **Router:** every check says which model to use (cheapest / balanced / quality), with a backup and the savings vs. always using one model. Task fit (`config/routing.yaml`) sends coding and writing to Claude by default.
- **Middle layer:** `/v1/route` calls the chosen model with the user's own keys (OpenAI, Anthropic, Gemini, or any OpenAI-compatible endpoint), with fallback. If the best fit isn't connected, the response and the account page say so and name the connected model used instead.
- **Stats:** per-key and per-model routing stats with money spent and saved on the account page (`GET /v1/usage/routing`).
- **Lint:** two scores (Lint and PQS), eleven checks, cost estimates on seven models, fix-it tips, "clarify first".
- **Website:** landing page with a savings calculator, analyzer, account page, API docs, API tester, prompt quiz with a leaderboard, Jev playground.
- **Public API** with sign-up, API keys, daily limits, usage tracking, and a fallback when Jev is down.
- **Endpoint quality:** users rate their own endpoints (Unknown … Excellent) on the account page and press Save; the router uses it as the model's task fit.
- **Quality:** 185 automated tests; evaluations on 480 prompts (240 pairs) and 100 labeled prompts, re-run on 26 Sept 2026 after the US localization.
- **Hosting:** Docker setup, live on Render + Neon, free.
