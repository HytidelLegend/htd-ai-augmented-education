"""Exact, reusable spelling similarity based on longest common subsequences."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator


def longest_common_subsequence_length(left: str, right: str) -> int:
    """Return the LCS length; matching characters need not be contiguous."""
    if len(left) > len(right):
        left, right = right, left
    masks: dict[str, int] = {}
    for position, character in enumerate(left):
        masks[character] = masks.get(character, 0) | (1 << position)
    state = 0
    for character in right:
        matches = masks.get(character, 0) | state
        advanced = (state << 1) | 1
        state = matches & (matches ^ (matches - advanced))
    return state.bit_count()


def subsequence_similarity(left: str, right: str) -> float:
    """Return 2 * LCS(left, right) / (len(left) + len(right))."""
    total_length = len(left) + len(right)
    if total_length == 0:
        return 1.0
    return 2 * longest_common_subsequence_length(left, right) / total_length


def meets_subsequence_threshold(
    left: str, right: str, *, numerator: int = 3, denominator: int = 4,
) -> bool:
    """Compare the score to a rational threshold without float rounding."""
    if not 0 < numerator <= denominator:
        raise ValueError("相似度阈值必须在 (0, 1] 内")
    total_length = len(left) + len(right)
    if total_length == 0:
        return False
    return (2 * denominator * longest_common_subsequence_length(left, right)
            >= numerator * total_length)


def iter_similar_pairs(
    words: Iterable[str], *, numerator: int = 3, denominator: int = 4,
) -> Iterator[tuple[str, str]]:
    """Yield every pair at or above the threshold using safe LCS upper bounds."""
    if not 0 < numerator <= denominator:
        raise ValueError("相似度阈值必须在 (0, 1] 内")
    ordered = sorted({word for word in words if word}, key=lambda word: (len(word), word))
    counts = {word: Counter(word) for word in ordered}
    for index, left in enumerate(ordered):
        left_length = len(left)
        for right in ordered[index + 1:]:
            total_length = left_length + len(right)
            if 2 * denominator * left_length < numerator * total_length:
                break
            overlap = sum((counts[left] & counts[right]).values())
            if 2 * denominator * overlap < numerator * total_length:
                continue
            if meets_subsequence_threshold(left, right, numerator=numerator, denominator=denominator):
                yield tuple(sorted((left, right)))
