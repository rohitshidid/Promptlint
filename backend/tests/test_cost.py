import pytest

from app import cost
from app.config import ModelPrice

RANGES = ((10, 60), (60, 300), (300, 900), (900, 3000))
PRICE = ModelPrice(id="m", name="M", provider="P", input=2.0, output=10.0, tokenizer="gemini", tier="mid")


def test_certain_level_gives_that_range():
    assert cost.output_range((0, 0, 1, 0), RANGES) == (300, 900)


def test_probability_weighted_range():
    lo, hi = cost.output_range((0, 0.5, 0.5, 0), RANGES)
    assert (lo, hi) == (180, 600)


def test_range_is_always_ordered():
    for probs in [(1, 0, 0, 0), (0.25, 0.25, 0.25, 0.25), (0, 0, 0, 1)]:
        lo, hi = cost.output_range(probs, RANGES)
        assert lo < hi


def test_unnormalized_probabilities_are_rescaled():
    assert cost.output_range((0, 0, 2, 0), RANGES) == (300, 900)


def test_empty_distribution_falls_back_to_widest_range():
    assert cost.output_range((0, 0, 0, 0), RANGES) == (10, 3000)


def test_mismatched_levels_raise():
    with pytest.raises(ValueError):
        cost.output_range((1, 0), RANGES)


def test_cost_range_math():
    lo, hi = cost.cost_range(1000, 300, 900, PRICE)
    assert lo == pytest.approx((1000 * 2 + 300 * 10) / 1e6)
    assert hi == pytest.approx((1000 * 2 + 900 * 10) / 1e6)


def test_all_configured_models_have_positive_prices(cfg):
    for m in cfg.prices.models:
        assert m.input > 0 and m.output > 0, m.id
        assert m.tier in {"small", "mid", "frontier"}


def test_price_table_has_a_date(cfg):
    assert cfg.prices.last_updated
