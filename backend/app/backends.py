"""Pluggable scoring backends (prompt-quality-scorer.md §5).

  jev        TypeSafe Jev, one call (app/jev_client.py)
  heuristic  transparent rules, no network: the baseline and the fallback when Jev is down

Both return `JevAnswers`, so scoring, cost and tips never care which backend ran.
`BackendRouter.judge(..., backend="auto")` uses Jev and falls back to the heuristic on a Jev
failure, marking the result `degraded`.
"""

import logging
import math
import re
import time
from dataclasses import dataclass

from app.config import QuestionConfig
from app.jev_client import ChoiceResult, JevAnswers, JevError, Judge, ScoreResult

log = logging.getLogger("promptlint.backends")

HEURISTIC_VERSION = "heuristic-1.0"


def _has(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE | re.MULTILINE) is not None


def _p(hit: bool, yes: float = 0.85, no: float = 0.15) -> float:
    return yes if hit else no


def _levels(value: float, top: int, sharpness: float = 4.0) -> tuple[float, ...]:
    """A soft distribution over 0..top centred on `value`, so expected score ≈ value."""
    weights = [math.exp(-sharpness * (i - value) ** 2) for i in range(top + 1)]
    total = sum(weights)
    return tuple(w / total for w in weights)


FORMAT = (
    r"\b(bullet|bulleted|list|table|json|csv|yaml|markdown|code only|numbered|outline|headings?|"
    r"subject line|one[- ]line|in \w+ (words|sentences|lines|paragraphs)|haiku|limerick|tweet|email|"
    r"letter|essay|script|return only|as a \w+|format)\b|\b\d+\s*(words?|lines?|sentences?|bullets?|points?|"
    r"items?|steps?|paragraphs?|slides?|questions?|ideas?|names?|options?)\b"
)
CONSTRAINT = (
    r"\b(under|at most|no more than|maximum|max|minimum|at least|only|without|avoid|don'?t|do not|never|"
    r"must|within|budget|deadline|by (monday|tuesday|wednesday|thursday|friday|tomorrow|tonight)|"
    r"tone|formal|casual|friendly|professional|playful|polite|firm|simple words|no jargon|limit)\b|"
    r"[₹$€£]\s?\d|\b\d+\s*(words?|characters?|minutes?|hours?|lines?|sentences?)\b"
)
AUDIENCE = (
    r"\b(for (a |an |my |our |the )?(\d+[- ]year[- ]olds?|beginners?|kids|children|students?|class|"
    r"team|manager|boss|ceo|clients?|customers?|readers?|audience|developers?|engineers?|parents?|"
    r"non-technical|experts?|seniors?|investors?)|year[- ]olds?|as if i'?m|explain (it )?(to|like)|"
    r"for someone|level|eli5|audience)\b"
)
EXAMPLE = (
    r"\b(example|e\.g\.|for instance|such as|like this|here is|here's|below)\b|```|\"[^\"]{12,}\"|:\s*\n"
)
GOAL = (
    r"\b(so (that|i can)|because|in order to|i want to|i need (it|this) (for|to)|for my|to help me|"
    r"goal|purpose|i'?m (preparing|trying|planning|building|writing)|to use (it|this))\b"
)
SUCCESS = (
    r"\b(must|should (return|include|end|start|be)|make sure|ensure|expected|criteria|verify|check|"
    r"test cases?|return only|end with|include \w+|show (the|each|your)|exactly|every)\b"
)
CURRENT = (
    r"\b(today|tonight|right now|current(ly)?|latest|this (week|month|year)|yesterday|trending|news|"
    r"stock price|price of|exchange rate|weather|forecast|score|live)\b"
)
PERSONAL = r"\b(i'?m|i am|i have|i've|my|our|we|we're|us)\b"
VAGUE = (
    r"^\s*(help|make it better|fix (it|this|my code)|give me ideas|write something|do my homework|"
    r"tell me about|explain this|improve this|what should i do)\b"
)
IMPERATIVE = (
    r"^\s*(write|create|make|build|explain|summari[sz]e|list|give|generate|translate|fix|debug|review|"
    r"compare|analy[sz]e|convert|extract|classify|solve|calculate|find|plan|design|draft|rewrite|edit|"
    r"proofread|suggest|recommend|describe|help|tell|show|what|why|how|when|where|who|which|is|are|can|"
    r"should|does|do)\b"
)
TWO_TASKS = r"\b(and also|also|then|as well as|plus|additionally|and (then )?(write|create|make|plan|design|suggest|build))\b"
CONFLICT_PAIRS = (
    (
        r"\b(detailed|comprehensive|in[- ]depth|full|thorough|long|\d{3,}[- ]words?)\b",
        r"\b(one sentence|one word|brief|short|under \d{1,3} words|no (longer|more) than \d{1,3})\b",
    ),
    (r"\bformal\b", r"\b(casual|slang|emojis?)\b"),
    (r"\b(brutally honest|harsh)\b", r"\bonly (say )?positive\b"),
    (r"\bsimple words\b", r"\bjargon throughout\b"),
    (r"\b(shorter|shorten)\b", r"\bmore detail\b"),
    (r"\bin \w+ only\b", r"\bevery word in\b"),
)
TASK_RULES = (
    (
        "coding",
        r"\b(code|function|script|bug|error|regex|sql|api|python|javascript|typescript|react|docker|git|compile|test cases?|kubernetes|bash)\b",
    ),
    (
        "math",
        r"\b(solve|equation|probability|integral|derivative|calculate|percentage|prove|how much|how many|interest|emi)\b|\d+\s*[x×*/+−-]\s*\d+",
    ),
    (
        "extraction_transformation",
        r"\b(summari[sz]e|extract|translate|convert|classify|categori[sz]e|reformat|proofread|rewrite|turn (this|these))\b",
    ),
    ("brainstorming", r"\b(ideas?|names?|brainstorm|suggest|options|slogans?|taglines?|plan)\b"),
    (
        "analysis",
        r"\b(compare|analy[sz]e|evaluate|pros and cons|review|critique|assess|should i|which is better|swot)\b",
    ),
    (
        "writing",
        r"\b(write|draft|email|letter|essay|post|poem|story|speech|bio|caption|message|description|cover letter)\b",
    ),
    ("factual_qa", r"^\s*(what|who|when|where|why|how|is|are|does|explain)\b"),
    ("conversation", r"^\s*(hi|hello|hey|thanks|thank you|how are you)\b"),
)


class HeuristicJudge:
    """Rule-based baseline: regexes and counts, no model. Deliberately simple and explainable."""

    name = "heuristic"

    def __init__(self, questions: QuestionConfig):
        self._q = questions
        self._tops = {k: q.top for k, q in questions.scores.items()}
        self._task_types = list(questions.choices["task_type"][1])

    async def judge(self, prompt: str, system: str | None = None) -> JevAnswers:
        start = time.perf_counter()
        text = prompt.strip()
        low = text.lower()
        words = len(re.findall(r"\w+", text))
        numbers = len(re.findall(r"\d+", text))
        proper = len(re.findall(r"(?<![.!?]\s)(?<!^)\b[A-Z][a-z]{2,}", text))
        vague = _has(VAGUE, low) or words <= 4

        fmt = _has(FORMAT, low)
        cons = _has(CONSTRAINT, low)
        aud = _has(AUDIENCE, low)
        ex = _has(EXAMPLE, text)
        goal = _has(GOAL, low)
        succ = _has(SUCCESS, low)
        personal = _has(PERSONAL, low)
        conflict = any(_has(a, low) and _has(b, low) for a, b in CONFLICT_PAIRS)
        multi = _has(TWO_TASKS, low) and words > 6
        current = _has(CURRENT, low)
        task_clear = _has(IMPERATIVE, low) and not vague and words >= 4

        detail = (
            sum([fmt, cons, aud, ex, goal, succ, personal]) + min(numbers, 3) * 0.5 + min(proper, 3) * 0.3
        )
        specificity = 0.0 if vague else min(3.0, 0.4 + detail * 0.45 + min(words, 80) / 60)
        context = (
            0.0
            if vague
            else min(2.0, 0.2 * personal + (words > 25) * 0.8 + (words > 50) * 0.5 + ex * 0.5 + goal * 0.4)
        )
        ambiguity = 2.0 if vague else max(0.0, 1.4 - detail * 0.3)
        first_try = 0.2 if vague else min(0.95, 0.35 + 0.08 * detail + 0.15 * task_clear - 0.3 * conflict)
        if conflict:
            first_try = min(first_try, 0.15)

        long_words = _has(
            r"\b(essay|guide|document|report|tutorial|program|app|article|blog post|\d{3,}[- ]words?)\b", low
        )
        short_words = _has(
            r"\b(one (line|word|sentence)|haiku|tweet|caption|slogan|name|yes or no|\d{1,2} words?)\b", low
        )
        length = 2.6 if long_words else 0.3 if short_words else 1.2 if words < 20 else 1.6
        hard = _has(
            r"\b(prove|debug|design|architecture|optimi[sz]e|trade-?offs?|concurren|distributed|algorithm)\b",
            low,
        )
        medium = _has(
            r"\b(code|function|compare|analy[sz]e|plan|step by step|calculate|solve|explain why)\b", low
        )
        complexity = 1.7 if hard else 1.0 if medium else 0.3

        task = next((t for t, pat in TASK_RULES if _has(pat, low) and t in self._task_types), "other")

        nouls = {
            "task_clear": _p(task_clear, 0.85, 0.3 if not vague else 0.2),
            "first_try_success": first_try,
            "needs_clarification": 1.0 - first_try,
            "has_constraints": _p(cons),
            "has_output_format": _p(fmt),
            "has_audience": _p(aud),
            "has_examples": _p(ex),
            "conflicting": _p(conflict, 0.8, 0.05),
            "multi_task": _p(multi, 0.75, 0.1),
            "needs_current_info": _p(current, 0.8, 0.05),
            "has_goal": _p(goal, 0.8, 0.15),
            "has_success_criteria": _p(succ, 0.75, 0.15),
        }
        values = {
            "specificity": specificity,
            "ambiguity": ambiguity,
            "context_given": context,
            "expected_length": length,
            "complexity": complexity,
        }
        scores = {}
        for name, top in self._tops.items():
            v = max(0.0, min(float(top), values.get(name, top / 2)))
            probs = _levels(v, top)
            scores[name] = ScoreResult(sum(i * p for i, p in enumerate(probs)), 0.55, probs, top)
        choice_probs = {
            t: (0.6 if t == task else 0.4 / (len(self._task_types) - 1)) for t in self._task_types
        }
        return JevAnswers(
            nouls={k: v for k, v in nouls.items() if k in self._q.nouls},
            scores=scores,
            choices={"task_type": ChoiceResult(task, 0.55, choice_probs)},
            model=HEURISTIC_VERSION,
            latency_ms=round((time.perf_counter() - start) * 1000),
        )


@dataclass(frozen=True)
class Judged:
    answers: JevAnswers
    backend: str  # which backend actually produced the answers
    degraded: bool  # True when the requested backend failed and the fallback ran
    fallback_reason: str | None = None


BACKENDS = ("auto", "jev", "heuristic")


class BackendRouter:
    """Chooses a backend per request; `auto` = Jev with heuristic fallback (PQS §11: 503 → degraded)."""

    def __init__(self, *, jev: Judge | None, heuristic: Judge):
        self.jev = jev
        self.heuristic = heuristic

    @property
    def jev_available(self) -> bool:
        return self.jev is not None

    async def judge(self, prompt: str, system: str | None = None, backend: str = "auto") -> Judged:
        if backend == "heuristic":
            return Judged(await self.heuristic.judge(prompt, system), "heuristic", False)
        if self.jev is None:
            if backend == "jev":
                raise JevError(503, "The Jev backend is not configured on this server.")
            return Judged(await self.heuristic.judge(prompt, system), "heuristic", True, "jev_not_configured")
        try:
            return Judged(await self.jev.judge(prompt, system), "jev", False)
        except JevError as e:
            if backend == "jev":
                raise
            log.warning("Jev failed (%s); serving the heuristic backend instead", e.status)
            return Judged(await self.heuristic.judge(prompt, system), "heuristic", True, f"jev_{e.status}")
