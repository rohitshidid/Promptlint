import pytest

from app import scoring, tips
from tests.conftest import make_answers

GOOD = {
    "task_clear": 0.95,
    "first_try_success": 0.9,
    "needs_clarification": 0.05,
    "has_constraints": 0.9,
    "has_output_format": 0.9,
    "has_audience": 0.9,
    "conflicting": 0.02,
    "multi_task": 0.1,
    "needs_current_info": 0.1,
}


def tips_for(cfg, nouls=None, scores=None):
    raw = scoring.raw_values(make_answers(cfg, nouls=dict(GOOD, **(nouls or {})), scores=scores))
    return tips.select_tips(raw, cfg.tips, cfg.weights)


def test_good_prompt_gets_no_tips(cfg):
    assert tips_for(cfg, scores={"specificity": 3, "context_given": 2, "ambiguity": 0}) == []


def test_missing_format_triggers_format_tip(cfg):
    out = tips_for(
        cfg, nouls={"has_output_format": 0.1}, scores={"specificity": 3, "context_given": 2, "ambiguity": 0}
    )
    assert [t.signal for t in out] == ["has_output_format"]
    assert "format" in out[0].text


def test_tips_are_ranked_by_weight_times_shortfall(cfg):
    out = tips_for(
        cfg,
        nouls={"task_clear": 0.2, "has_audience": 0.0},
        scores={"specificity": 0, "context_given": 2, "ambiguity": 0},
    )
    # task_clear: 0.15 × 0.8 = 0.12 ; specificity: 0.15 × 1.0 = 0.15 ; audience: 0.03 × 1.0 = 0.03
    assert [t.signal for t in out] == ["specificity", "task_clear", "has_audience"]
    assert out == sorted(out, key=lambda t: t.impact, reverse=True)


def test_at_most_five_tips(cfg):
    bad = {k: 0.0 for k in ("task_clear", "has_constraints", "has_output_format", "has_audience")}
    bad.update(conflicting=1.0, multi_task=1.0, needs_current_info=1.0)
    out = tips_for(cfg, nouls=bad, scores={"specificity": 0, "context_given": 0, "ambiguity": 2})
    assert len(out) == cfg.tips.max_tips == 5


def test_informational_tips_use_their_own_weight(cfg):
    out = tips_for(
        cfg, nouls={"needs_current_info": 0.9}, scores={"specificity": 3, "context_given": 2, "ambiguity": 0}
    )
    assert [t.signal for t in out] == ["needs_current_info"]
    assert out[0].impact == pytest.approx(0.04 * 0.9)


def test_thresholds_are_strict(cfg):
    out = tips_for(
        cfg, nouls={"multi_task": 0.6}, scores={"specificity": 3, "context_given": 2, "ambiguity": 0}
    )
    assert out == []
