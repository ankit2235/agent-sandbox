import re

from hypothesis import given
from hypothesis import strategies as st

from kit.rng import SeededRNG, derive_seed

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@given(st.integers(min_value=0, max_value=2**32 - 1))
def test_same_seed_gives_the_same_stream(seed: int) -> None:
    first = SeededRNG(seed)
    second = SeededRNG(seed)
    assert [first.randint(0, 10_000) for _ in range(20)] == [
        second.randint(0, 10_000) for _ in range(20)
    ]


def test_child_streams_are_independent_of_draw_order() -> None:
    parent = SeededRNG(42)
    rails_first = parent.child("rails")
    commerce = parent.child("commerce")
    # Draining one child must not change what the other produces.
    [rails_first.random() for _ in range(100)]
    assert SeededRNG(42).child("commerce").random() == commerce.random()


def test_child_labels_compose() -> None:
    assert SeededRNG(1).child("a").child("b").label == "root/a/b"


def test_different_labels_give_different_streams() -> None:
    parent = SeededRNG(5)
    assert parent.child("rails").random() != parent.child("commerce").random()


def test_derive_seed_is_stable_and_bounded() -> None:
    assert derive_seed(1, "rails") == derive_seed(1, "rails")
    assert derive_seed(1, "rails") != derive_seed(1, "commerce")
    assert 0 <= derive_seed(2**63, "x") < 2**64


def test_uuid_is_uuid4_shaped_and_reproducible() -> None:
    value = SeededRNG(9).uuid()
    assert UUID_RE.match(value), value
    assert SeededRNG(9).uuid() == value


def test_token_is_reproducible_and_lowercase() -> None:
    token = SeededRNG(11).token(16)
    assert len(token) == 16
    assert token == token.lower()
    assert SeededRNG(11).token(16) == token


def test_chance_is_seeded_and_bounded() -> None:
    draws = [SeededRNG(3).child(str(index)).chance(0.5) for index in range(50)]
    assert any(draws) and not all(draws)


def test_bytes_and_shuffle_are_reproducible() -> None:
    assert SeededRNG(4).bytes(8) == SeededRNG(4).bytes(8)
    items_a, items_b = [1, 2, 3, 4, 5], [1, 2, 3, 4, 5]
    SeededRNG(6).shuffle(items_a)
    SeededRNG(6).shuffle(items_b)
    assert items_a == items_b
