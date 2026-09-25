"""Eval 2 (PromptLint.md §12): is Jev's first_try_success calibrated against real outcomes?

For a sample of prompts:
  1. an LLM answers the prompt as-is;
  2. a judge model decides "Does this response fully satisfy the request without a follow-up?";
  3. we compare Jev's first_try_success probability with that label: AUROC, Brier score, reliability bins.

The only place an LLM is used in this project. Costs money, so it asks before spending.

    python eval/run_first_try.py --provider anthropic --n 120          # needs ANTHROPIC_API_KEY
    python eval/run_first_try.py --provider groq --model llama-3.3-70b-versatile --n 120   # needs GROQ_API_KEY

Results are cached per (provider, model, prompt) in eval/results/first_try_cache.jsonl, so re-runs
only pay for new prompts. Spot-check at least 50 judge labels by hand (PromptLint.md §12): the rows are
written to eval/results/first_try_rows.csv with the response and the judge's reason.
"""

import argparse
import asyncio
import csv
import hashlib
import json
import os
import random
import sys

import httpx

from common import RESULTS, auroc, brier, judge_all, load_jsonl, reliability, update_summary, write_result

JUDGE_INSTRUCTIONS = """You are grading whether an AI assistant's reply fully satisfied a user's request on the first try.

Answer satisfied=true only if a typical user who sent this request would accept the reply as complete and usable,
without needing to send a follow-up message to correct it, add missing details, or ask for a different format.
Answer satisfied=false if the reply had to guess at details that likely matter to the user, asked a clarifying
question instead of answering, ignored part of the request, or would probably need a correction.

<request>
{prompt}
</request>

<reply>
{reply}
</reply>"""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "satisfied": {"type": "boolean"},
        "reason": {"type": "string", "description": "One sentence explaining the decision."},
    },
    "required": ["satisfied", "reason"],
    "additionalProperties": False,
}

CACHE = RESULTS / "first_try_cache.jsonl"


# ------------------------------------------------------------------ providers
class AnthropicLLM:
    """Claude via the official SDK, with server-side refusal fallbacks enabled."""

    def __init__(self, model: str):
        import anthropic

        self.anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(max_retries=4)
        self.model = model

    async def answer(self, prompt: str) -> str:
        resp = await self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            return "[REFUSED]"
        return "".join(b.text for b in resp.content if b.type == "text")

    async def judge(self, prompt: str, reply: str) -> dict:
        resp = await self.client.beta.messages.create(
            model=self.model,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
            messages=[{"role": "user", "content": JUDGE_INSTRUCTIONS.format(prompt=prompt, reply=reply)}],
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError("judge refused")
        text = next(b.text for b in resp.content if b.type == "text")
        return json.loads(text)


class GroqLLM:
    """Groq's OpenAI-compatible chat API over plain HTTP (no extra SDK)."""

    URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, model: str):
        key = os.environ.get("GROQ_API_KEY")
        if not key:
            sys.exit("GROQ_API_KEY is not set.")
        self.model = model
        self.http = httpx.AsyncClient(timeout=120, headers={"Authorization": f"Bearer {key}"})

    async def _chat(self, content: str, *, json_mode: bool) -> str:
        body = {"model": self.model, "messages": [{"role": "user", "content": content}], "temperature": 0}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        for attempt in range(6):
            r = await self.http.post(self.URL, json=body)
            if r.status_code == 429 or r.status_code >= 500:
                await asyncio.sleep(float(r.headers.get("retry-after", 2 * (attempt + 1))))
                continue
            if r.status_code == 404 or (r.status_code == 400 and "model" in r.text):
                sys.exit(f"Groq rejected model {self.model!r}: {r.text[:300]}")
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        raise RuntimeError("Groq kept rate-limiting")

    async def answer(self, prompt: str) -> str:
        return await self._chat(prompt, json_mode=False)

    async def judge(self, prompt: str, reply: str) -> dict:
        text = await self._chat(
            JUDGE_INSTRUCTIONS.format(prompt=prompt, reply=reply)
            + '\n\nRespond with JSON only: {"satisfied": true|false, "reason": "<one sentence>"}',
            json_mode=True,
        )
        data = json.loads(text)
        return {"satisfied": bool(data["satisfied"]), "reason": str(data.get("reason", ""))}


# ------------------------------------------------------------------ main
def cache_key(provider: str, model: str, prompt: str) -> str:
    return hashlib.sha256(f"{provider}|{model}|{prompt}".encode()).hexdigest()[:24]


def load_cache() -> dict[str, dict]:
    if not CACHE.exists():
        return {}
    return {row["key"]: row for row in map(json.loads, CACHE.read_text().splitlines()) if row}


def sample_prompts(n: int, seed: int) -> list[dict]:
    """Stratified: equal weak and improved prompts, spread across task types."""
    pairs = load_jsonl("prompt_pairs.jsonl") + load_jsonl("prompt_pairs_hard.jsonl")
    rng = random.Random(seed)
    rng.shuffle(pairs)
    chosen = pairs[: max(1, n // 2)]
    out = []
    for p in chosen:
        out.append({"id": f"{p['id']}-weak", "prompt": p["weak"]})
        out.append({"id": f"{p['id']}-improved", "prompt": p["improved"]})
    return out


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", choices=["anthropic", "groq"], required=True)
    ap.add_argument("--model", help="Responder and judge model (default: claude-opus-5 for anthropic)")
    ap.add_argument("--n", type=int, default=120, help="Number of prompts (half weak, half improved)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--yes", action="store_true", help="Skip the cost confirmation")
    args = ap.parse_args()

    model = args.model or ("claude-opus-5" if args.provider == "anthropic" else None)
    if not model:
        sys.exit("--model is required for groq (e.g. llama-3.3-70b-versatile)")

    items = sample_prompts(args.n, args.seed)
    cache = load_cache()
    todo = [it for it in items if cache_key(args.provider, model, it["prompt"]) not in cache]
    print(
        f"{len(items)} prompts, {len(todo)} need LLM calls ({2 * len(todo)} requests to {args.provider}:{model})."
    )
    if todo and not args.yes:
        if input("This spends API credits. Continue? [y/N] ").strip().lower() != "y":
            sys.exit("Cancelled.")

    llm = AnthropicLLM(model) if args.provider == "anthropic" else GroqLLM(model)
    sem = asyncio.Semaphore(args.concurrency)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    done = 0

    async def run(it: dict) -> None:
        nonlocal done
        async with sem:
            reply = await llm.answer(it["prompt"])
            verdict = await llm.judge(it["prompt"], reply)
        row = {
            "key": cache_key(args.provider, model, it["prompt"]),
            "id": it["id"],
            "provider": args.provider,
            "model": model,
            "satisfied": bool(verdict["satisfied"]),
            "reason": verdict["reason"],
            "reply": reply,
        }
        cache[row["key"]] = row
        with CACHE.open("a") as f:
            f.write(json.dumps(row) + "\n")
        done += 1
        if done % 10 == 0 or done == len(todo):
            print(f"  {done}/{len(todo)} judged", file=sys.stderr)

    await asyncio.gather(*(run(it) for it in todo))

    answers, latencies = await judge_all([it["prompt"] for it in items], label="first-try")
    probs, labels, rows = [], [], []
    for it in items:
        c = cache[cache_key(args.provider, model, it["prompt"])]
        p = answers[it["prompt"]].nouls["first_try_success"]
        probs.append(p)
        labels.append(int(c["satisfied"]))
        rows.append(
            {
                "id": it["id"],
                "jev_first_try": round(p, 3),
                "judge_satisfied": c["satisfied"],
                "judge_reason": c["reason"],
                "prompt": it["prompt"],
                "reply": c["reply"][:2000],
            }
        )

    result = {
        "n": len(items),
        "provider": args.provider,
        "model": model,
        "positive_rate": sum(labels) / len(labels),
        "auroc": auroc(probs, labels),
        "brier": brier(probs, labels),
        "reliability": reliability(probs, labels),
    }
    write_result("first_try", result)
    with (RESULTS / "first_try_rows.csv").open("w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)
    update_summary(
        "first_try",
        {
            "auroc": result["auroc"],
            "brier": result["brier"],
            "n": len(items),
            "judge": f"{args.provider}:{model}",
        },
        latencies=latencies,
    )
    print(
        f"\nFirst-try: AUROC {result['auroc']:.3f}, Brier {result['brier']:.3f}, "
        f"judge said satisfied for {result['positive_rate']:.0%} of {len(items)} prompts"
    )
    for b in result["reliability"]:
        print(
            f"  predicted {b['bin']}: n={b['n']:3d}  mean pred {b['mean_predicted']:.2f}  observed {b['observed_rate']:.2f}"
        )


if __name__ == "__main__":
    asyncio.run(main())
