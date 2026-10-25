"""Near-duplicate detection and splitting by family."""

import pytest

from training.data.similarity import (
    NearDuplicateConfig,
    near_pairs,
    shingles,
    signature,
    signatures,
    similarity,
)
from training.data.splits import Families, SplitConfig, assign

TEXT = (
    "World: payments. The user asked to pay the electricity bill from GridCo Power exactly, to "
    "the handle on the bill, and the agent read the bill, which says the amount due is 1,840 "
    "rupees by 15 January, payable to gridco-power at sandbox, quoting bill GP-2512-0042."
)
CONFIG = NearDuplicateConfig()


def test_identical_texts_are_one_and_different_ones_are_far() -> None:
    a, b = signatures([TEXT, TEXT], CONFIG)
    assert similarity(a, b) == 1.0
    other = "Security world: isolate the host web-prod-01 after reviewing alert ALR-4201 now."
    c, d = signatures([TEXT, other], CONFIG)
    assert similarity(c, d) < 0.2


def test_a_one_word_change_is_still_a_near_duplicate() -> None:
    edited = TEXT.replace("15 January", "16 January")
    pairs = near_pairs(signatures([TEXT, edited], CONFIG), ["train", "test"], CONFIG)
    assert len(pairs) == 1
    assert pairs[0].similarity >= CONFIG.threshold


def test_rows_of_one_group_are_never_compared() -> None:
    assert near_pairs(signatures([TEXT, TEXT], CONFIG), ["train", "train"], CONFIG) == []


def test_skip_and_queries_narrow_the_pairs() -> None:
    sigs = signatures([TEXT, TEXT, TEXT], CONFIG)
    groups = ["a", "b", "c"]
    assert len(near_pairs(sigs, groups, CONFIG)) == 3
    assert len(near_pairs(sigs, groups, CONFIG, skip=lambda x, y: {x, y} == {"a", "b"})) == 2
    assert len(near_pairs(sigs, groups, CONFIG, queries=[2])) == 2


def test_short_and_empty_texts_still_get_signatures() -> None:
    assert shingles("", 5)
    assert len(signature(shingles("just two", 5), 16)) == 16


def test_bins_must_divide_into_bands() -> None:
    with pytest.raises(ValueError, match="multiple"):
        signatures([TEXT], NearDuplicateConfig(bins=130, rows=4))


def test_families_merge_into_groups_named_after_their_smallest_member() -> None:
    families = Families(["c", "a", "b", "d"])
    assert families.union("c", "b")
    assert not families.union("b", "c")
    assert families.union("d", "c")
    assert families.find("d") == "b"
    assert families.groups() == {"a": ["a"], "b": ["b", "c", "d"]}


def test_splits_are_seeded_and_hold_out_every_category() -> None:
    groups = {f"g{i:02d}": ("payments", "rails" if i < 10 else "multi") for i in range(13)}
    once = assign(groups, seed=0, config=SplitConfig())
    assert once == assign(groups, seed=0, config=SplitConfig())
    assert once != assign(groups, seed=1, config=SplitConfig())
    for category in ("rails", "multi"):
        held = {once[g] for g, cell in groups.items() if cell[1] == category}
        assert {"train", "validation", "test"} <= held


def test_a_category_with_fewer_than_three_groups_stays_in_train_mostly() -> None:
    once = assign({"only": ("security", "triage")}, seed=0, config=SplitConfig())
    assert once == {"only": "train"}
