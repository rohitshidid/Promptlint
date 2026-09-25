"""Contract tests: recorded real Jev responses go through the real SDK and our parser.

If Jev changes its response shape, these fail before production does. Re-record with
`python tests/record_fixtures.py` after changing questions.yaml or the pinned model.
"""

import pytest

from app import scoring
from app.analyze import Analyzer
from app.tokens import TokenCounter
from tests.conftest import fixture_judge, load_fixture

NAMES = ["weak", "strong", "conflicting"]


@pytest.mark.parametrize("name", NAMES)
async def test_fixture_parses_into_every_expected_answer(cfg, name):
    judge = fixture_judge(cfg, name)
    answers = await judge.judge(load_fixture(name)["prompt"])
    assert set(answers.nouls) == set(cfg.questions.nouls)
    assert set(answers.scores) == set(cfg.questions.scores)
    assert set(answers.choices) == set(cfg.questions.choices)
    for p in answers.nouls.values():
        assert 0.0 <= p <= 1.0
    for name_, s in answers.scores.items():
        assert 0.0 <= s.score <= s.top, name_
        assert len(s.probabilities) == s.top + 1
        assert sum(s.probabilities) == pytest.approx(1.0, abs=0.02)
    assert answers.choices["task_type"].choice in cfg.questions.choices["task_type"][1]
    assert answers.model.startswith("jev-")
    await judge.aclose()


@pytest.mark.parametrize("name", NAMES)
def test_fixture_was_recorded_against_the_current_question_set(cfg, name):
    recorded = set(load_fixture(name)["response"]["answers"])
    expected = set(cfg.questions.nouls) | set(cfg.questions.scores) | set(cfg.questions.choices)
    assert recorded == expected, "questions.yaml changed: re-run tests/record_fixtures.py"


async def test_strong_prompt_outscores_weak_prompt(cfg):
    scores = {}
    for name in ("weak", "strong"):
        answers = await fixture_judge(cfg, name).judge("x")
        scores[name] = scoring.lint_score(scoring.raw_values(answers), cfg.weights).lint_score
    assert scores["strong"] > scores["weak"]


async def test_conflicting_prompt_fails(cfg):
    answers = await fixture_judge(cfg, "conflicting").judge("x")
    assert answers.nouls["conflicting"] > 0.7  # Jev spots the contradiction
    result = scoring.lint_score(scoring.raw_values(answers), cfg.weights)
    # Already below both caps, so no cap is recorded (caps only ever lower a score).
    assert result.lint_score <= 60
    assert result.verdict == "likely_to_fail"


async def test_full_report_from_a_real_response(cfg):
    fx = load_fixture("strong")
    analyzer = Analyzer(judge=fixture_judge(cfg, "strong"), counter=TokenCounter(), config=cfg)
    report = await analyzer.analyze(fx["prompt"])
    assert 0 <= report.lint_score <= 100
    assert report.meta.jev_model == fx["response"]["model"]
    lo, hi = report.tokens.output_range
    assert 0 < lo < hi
    assert all(c.low_usd < c.high_usd for c in report.costs)
    assert len(report.checks) == 9
    assert [d.id for d in report.dimensions] == [
        "task_clear",
        "specificity",
        "context_given",
        "has_output_format",
        "has_constraints",
    ]
