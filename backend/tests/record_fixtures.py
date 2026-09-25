"""Record real Jev responses into tests/fixtures/ for the contract tests.

Re-run whenever questions.yaml or the pinned Jev model changes:

    TYPESAFE_API_KEY=... python tests/record_fixtures.py

Each fixture stores the prompt, the pinned model, and the raw response body exactly as Jev sent it.
"""

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config  # noqa: E402
from app.questions import build_questions  # noqa: E402
from app.settings import get_settings  # noqa: E402

PROMPTS = {
    "weak": "write me a poem",
    "strong": (
        "I'm a high-school chemistry teacher. Write a 12-line rhyming poem for my Year 10 class that "
        "explains ionic vs covalent bonding. Use one simple analogy per bond type, keep the vocabulary "
        "at a 15-year-old's level, and end with a two-line summary they can memorize. Return only the poem."
    ),
    "conflicting": "Write a detailed 2,000-word essay on climate policy. Keep it under 100 words. Use bullet points only, no lists.",
}

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def main() -> None:
    settings = get_settings()
    if not settings.typesafe_api_key:
        sys.exit("Set TYPESAFE_API_KEY first.")
    cfg = load_config(settings.config_dir)
    questions = {k: q.model_dump() for k, q in build_questions(cfg.questions).items()}
    FIXTURES.mkdir(exist_ok=True)
    with httpx.Client(timeout=30) as client:
        for name, prompt in PROMPTS.items():
            r = client.post(
                "https://api.typesafe.ai/v1/systemone",
                headers={"Authorization": f"Bearer {settings.typesafe_api_key}"},
                json={"model": settings.jev_model, "state": {"prompt": prompt}, "questions": questions},
            )
            r.raise_for_status()
            path = FIXTURES / f"jev_{name}.json"
            path.write_text(
                json.dumps({"prompt": prompt, "model": settings.jev_model, "response": r.json()}, indent=2)
                + "\n"
            )
            print(f"wrote {path.name}: {r.json()['model']}")


if __name__ == "__main__":
    main()
