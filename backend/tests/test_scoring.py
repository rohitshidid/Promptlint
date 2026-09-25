import pytest

from app import scoring
from tests.conftest import make_answers

PERFECT_NOULS = {
    "task_clear": 1.0,
    "first_try_success": 1.0,
    "needs_clarification": 0.0,
    "has_constraints": 1.0,
    "has_output_format": 1.0,
    "has_audience": 1.0,
    "conflicting": 0.0,
}
PERFECT_SCORES = {"specificity": 3, "context_given": 2, "ambiguity": 0}


def score_of(cfg, **kw):
    return scoring.lint_score(scoring.raw_values(make_answers(cfg, **kw)), cfg.weights)


def test_weights_sum_to_one(cfg):
    assert sum(w.weight for w in cfg.weights.weights.values()) == pytest.approx(1.0)


def test_perfect_prompt_scores_100(cfg):
    r = score_of(cfg, nouls=PERFECT_NOULS, scores=PERFECT_SCORES)
    assert r.lint_score == 100
    assert r.verdict == "ready_to_send"
    assert r.caps_applied == ()


def test_worst_prompt_scores_0_and_fails(cfg):
    nouls = {k: 1.0 - v for k, v in PERFECT_NOULS.items()}
    r = score_of(cfg, nouls=nouls, scores={"specificity": 0, "context_given": 0, "ambiguity": 2})
    assert r.uncapped == 0
    assert r.verdict == "likely_to_fail"


def test_inverted_signals_count_as_good_when_low(cfg):
    raw = scoring.raw_values(make_answers(cfg, nouls={"conflicting": 0.0}, scores={"ambiguity": 0}))
    sig = scoring.signals(raw, cfg.weights)
    assert sig["conflicting"] == 1.0
    assert sig["ambiguity"] == 1.0


def test_score_is_normalized_by_top_level(cfg):
    raw = scoring.raw_values(make_answers(cfg, scores={"specificity": 1.5}))
    assert raw["specificity"] == pytest.approx(0.5)


def test_conflict_cap_at_60(cfg):
    nouls = dict(PERFECT_NOULS, conflicting=0.8)
    r = score_of(cfg, nouls=nouls, scores=PERFECT_SCORES)
    assert r.uncapped > 60
    assert r.lint_score == 60
    assert r.caps_applied == ("conflicting",)


def test_conflict_at_threshold_is_not_capped(cfg):
    r = score_of(cfg, nouls=dict(PERFECT_NOULS, conflicting=0.7), scores=PERFECT_SCORES)
    assert "conflicting" not in r.caps_applied


def test_unclear_task_cap_at_40(cfg):
    r = score_of(cfg, nouls=dict(PERFECT_NOULS, task_clear=0.2), scores=PERFECT_SCORES)
    assert r.lint_score == 40
    assert r.caps_applied == ("task_clear",)


def test_both_caps_take_the_lower(cfg):
    r = score_of(cfg, nouls=dict(PERFECT_NOULS, task_clear=0.1, conflicting=0.9), scores=PERFECT_SCORES)
    assert r.lint_score == 40


def test_cap_never_raises_a_score(cfg):
    low = {k: 1.0 - v for k, v in PERFECT_NOULS.items()}
    r = score_of(cfg, nouls=low, scores={"specificity": 0, "context_given": 0, "ambiguity": 2})
    assert r.lint_score == r.uncapped == 0


@pytest.mark.parametrize(
    ("score", "verdict"),
    [
        (100, "ready_to_send"),
        (75, "ready_to_send"),
        (74, "needs_work"),
        (50, "needs_work"),
        (49, "likely_to_fail"),
        (0, "likely_to_fail"),
    ],
)
def test_verdict_boundaries(cfg, score, verdict):
    assert scoring.verdict(score, cfg.weights) == verdict


def test_informational_signals_do_not_move_the_score(cfg):
    a = score_of(cfg, nouls=dict(PERFECT_NOULS, has_examples=0.0, needs_current_info=1.0, multi_task=1.0))
    b = score_of(cfg, nouls=dict(PERFECT_NOULS, has_examples=1.0, needs_current_info=0.0, multi_task=0.0))
    assert a.lint_score == b.lint_score


@pytest.mark.parametrize(
    ("value", "label"),
    [(0.0, "Generic"), (0.3, "Mostly generic"), (0.6, "Fairly specific"), (1.0, "Specific")],
)
def test_specificity_labels(cfg, value, label):
    assert scoring.specificity_label(value, cfg.weights) == label


@pytest.mark.parametrize(
    ("complexity", "tier"), [(0.2, "small"), (1.4, "mid"), (2.6, "frontier"), (3.0, "frontier")]
)
def test_tier_hint(cfg, complexity, tier):
    assert scoring.tier_hint(complexity, cfg.weights) == tier


def test_low_confidence_flag(cfg):
    assert scoring.low_confidence(make_answers(cfg, confidence=0.3), cfg.weights) is True
    assert scoring.low_confidence(make_answers(cfg, confidence=0.8), cfg.weights) is False
