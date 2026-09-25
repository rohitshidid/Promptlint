"""Output-token estimate and per-model cost ranges (PromptLint.md §8). Pure functions."""

from collections.abc import Sequence

from app.config import ModelPrice


def output_range(level_probs: Sequence[float], ranges: Sequence[tuple[int, int]]) -> tuple[int, int]:
    """Probability-weighted low and high output tokens from the `expected_length` distribution."""
    if len(level_probs) != len(ranges):
        raise ValueError("expected_length levels and output ranges differ in length")
    total = sum(level_probs)
    if total <= 0:
        # No usable distribution: fall back to the widest honest range.
        return ranges[0][0], ranges[-1][1]
    low = sum(p * lo for p, (lo, _) in zip(level_probs, ranges, strict=True)) / total
    high = sum(p * hi for p, (_, hi) in zip(level_probs, ranges, strict=True)) / total
    return round(low), round(high)


def cost_range(input_tokens: int, out_low: int, out_high: int, price: ModelPrice) -> tuple[float, float]:
    """USD cost range for one request; prices are per million tokens."""
    base = input_tokens * price.input
    low = (base + out_low * price.output) / 1_000_000
    high = (base + out_high * price.output) / 1_000_000
    return low, high
