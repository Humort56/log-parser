"""Tests for the coverage algebra: merge_ranges and missing_ranges.

These are pure functions over inclusive integer-second ranges [start, end].
They decide what gets fetched, so the boundary cases matter: an off-by-one here
means either a phantom gap (a pointless remote fetch) or a swallowed gap
(silently missing events).
"""

from itertools import pairwise

import pytest

from log_parser import merge_ranges, missing_ranges

# --------------------------------------------------------------------------
# merge_ranges
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ranges, expected",
    [
        ([], []),
        ([(100, 200)], [(100, 200)]),
        # Disjoint ranges stay separate, and come back sorted.
        ([(300, 400), (100, 200)], [(100, 200), (300, 400)]),
        # Overlapping.
        ([(100, 200), (150, 250)], [(100, 250)]),
        # Adjacent on inclusive seconds: 140 and 141 touch, so these are one
        # contiguous range. Failing to merge would leave a zero-width gap.
        ([(100, 140), (141, 200)], [(100, 200)]),
        # A one-second hole is a real gap and must NOT be merged away.
        ([(100, 140), (142, 200)], [(100, 140), (142, 200)]),
        # Fully nested range is absorbed.
        ([(100, 300), (150, 200)], [(100, 300)]),
        # Chain that collapses transitively.
        ([(100, 150), (140, 190), (185, 220)], [(100, 220)]),
        # Identical ranges collapse to one.
        ([(100, 200), (100, 200)], [(100, 200)]),
        # Single-second ranges that touch.
        ([(100, 100), (101, 101)], [(100, 101)]),
    ],
)
def test_merge_ranges(ranges, expected):
    assert merge_ranges(ranges) == expected


def test_merge_ranges_does_not_mutate_input():
    ranges = [(100, 140), (141, 200)]
    merge_ranges(ranges)
    assert ranges == [(100, 140), (141, 200)]


# --------------------------------------------------------------------------
# missing_ranges
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "t1, t2, covered, expected",
    [
        # Nothing fetched yet -> the whole window is missing.
        (100, 200, [], [(100, 200)]),
        # Exactly covered, and covered by a wider range -> nothing missing.
        (100, 200, [(100, 200)], []),
        (100, 200, [(50, 300)], []),
        # Hole in the middle.
        (100, 200, [(100, 140), (160, 200)], [(141, 159)]),
        # Adjacent stored ranges leave no gap (the merge rule, end to end).
        (100, 200, [(100, 140), (141, 200)], []),
        # Partial coverage on each side.
        (100, 200, [(100, 150)], [(151, 200)]),
        (100, 200, [(150, 200)], [(100, 149)]),
        # Coverage entirely outside the window is irrelevant.
        (100, 200, [(300, 400)], [(100, 200)]),
        (100, 200, [(1, 50)], [(100, 200)]),
        # Coverage clipped to the window: gaps never extend past t1/t2.
        (100, 200, [(50, 120), (180, 400)], [(121, 179)]),
        # Multiple holes.
        (100, 300, [(120, 140), (200, 220)], [(100, 119), (141, 199), (221, 300)]),
        # Single-second window, covered and uncovered.
        (100, 100, [], [(100, 100)]),
        (100, 100, [(100, 100)], []),
        # Inverted window yields nothing rather than a bogus range.
        (200, 100, [], []),
    ],
)
def test_missing_ranges(t1, t2, covered, expected):
    assert missing_ranges(t1, t2, covered) == expected


def test_missing_ranges_gaps_are_disjoint_and_ordered():
    gaps = missing_ranges(0, 1000, [(100, 200), (500, 600), (800, 850)])
    assert gaps == [(0, 99), (201, 499), (601, 799), (851, 1000)]
    for (_, prev_end), (next_start, _) in pairwise(gaps):
        assert prev_end < next_start


def test_missing_ranges_refetching_a_gap_closes_it():
    """Recording the gaps returned must make the window fully covered."""
    covered = [(100, 140), (160, 200)]
    covered += missing_ranges(100, 200, covered)
    assert missing_ranges(100, 200, covered) == []
