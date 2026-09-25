# To-do

What's left to do on PromptLint, in plain words. Top of each list = do first.
Last updated: 25 September 2026.

## Before sharing it with the world

These matter because once it's public, strangers can sign up and every check they run costs you a little money on TypeSafe (the Jev AI judge).

- [ ] **Put it online.** Follow "Deploy (free)" in the README: Neon (free database) + Render (free hosting).
- [ ] **Commit and push the latest work to GitHub.** The public API, accounts, tester fixes and these docs aren't committed yet. Render deploys from GitHub, so it needs them.
- [ ] **Turn on the captcha for sign-ups.** Without it, a bot could create thousands of accounts. It's free: create a Cloudflare Turnstile widget and set `TURNSTILE_SITE_KEY` and `TURNSTILE_SECRET_KEY` on Render.
- [ ] **Decide how much Jev spending you're OK with.** Each free account gets 1,000 checks a day. With many friends, that adds up. Lower `daily` in `backend/config/plans.yaml` (for example to 100), and set a spending alert in your TypeSafe dashboard if it has one.
- [ ] **Revoke the test key that was shared in chat** (`pqs_live_d1gasQeF_…`) on your local account page. It only works locally, but it's good practice.
- [ ] **Add a short "Terms & privacy" page.** One paragraph: what's stored (email, hashed password, usage counts, no prompts unless someone opts in), that prompts are sent to TypeSafe for checking, and how to delete your account.

## Soon

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
- 128 automated tests; accuracy tests on 240 prompt pairs and 100 labeled prompts.
- Docker setup and a free hosting plan (Render + Neon).
