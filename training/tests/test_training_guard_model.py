"""Our model's format, backend (with a fake scorer), export, configs, reward and scripts' checks.

Nothing here imports torch, transformers, PEFT or TRL: the training scripts are checked
with ``--check``, which validates their config and data and stops before training.
"""

import json
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

import guard
from guard.actions import actions
from guard.backends.guard_model.backend import GuardModelBackend
from guard.backends.guard_model.format import (
    completion,
    opening,
    prompt,
    reasons_after,
    reasons_for,
)
from training.data.build import read_split
from training.guard_model import grpo, sft
from training.guard_model.export import export
from training.guard_model.reward import (
    DECISIONS,
    RewardWeights,
    parse_completion,
    reward,
    trl_reward,
)

CONFIGS = Path(__file__).parents[1] / "src" / "training" / "guard_model" / "configs"
REQUIREMENTS = CONFIGS.parent / "requirements-train.txt"


class FakeScorer:
    """Log-probabilities by decision, and a fixed generation."""

    model_id = "fake/guard"
    revision = "abc123"

    def __init__(self, scores: dict[str, float], generated: str) -> None:
        self.scores = scores
        self.generated = generated
        self.prompts: list[str] = []

    def logprobs(self, text: str, continuations: Sequence[str]) -> list[float]:
        self.prompts.append(text)
        return [
            next(v for d, v in self.scores.items() if f'"{d}"' in item) for item in continuations
        ]

    def generate(self, text: str, max_new_tokens: int) -> str:
        return self.generated


def _request(small_build: Path):
    return read_split(small_build, "test")[0].request


def test_a_completion_starts_with_its_opening() -> None:
    for decision in ("allow", "block", "ask_user"):
        text = completion(decision, ["payments.overspend"] if decision != "allow" else [])
        assert text.startswith(opening(decision))
        assert reasons_after(decision, text[len(opening(decision)) :]) == (
            ["payments.overspend"] if decision != "allow" else []
        )


def test_malformed_generations_give_no_reasons() -> None:
    assert reasons_after("block", '"payments.overspend"') is None
    assert reasons_after("block", "1, 2]}") is None
    assert reasons_after("block", "not json]}") is None


def test_the_backend_picks_the_likeliest_decision_with_its_probability(small_build: Path) -> None:
    scorer = FakeScorer({"allow": -3.0, "block": -0.5, "ask_user": -2.0}, '"payments.overspend"]}')
    result = GuardModelBackend("guard", scorer).decide(_request(small_build))
    assert result.decision.decision == "block"
    assert result.decision.reason_codes == ["payments.overspend"]
    assert result.probabilities is not None
    assert abs(sum(result.probabilities.values()) - 1) < 1e-9
    assert result.decision.confidence == result.probabilities["block"]
    assert result.model_version == "abc123"
    assert "Reason codes for this world" in scorer.prompts[0]


def test_a_tie_goes_to_the_cautious_decision(small_build: Path) -> None:
    scorer = FakeScorer({"allow": -1.0, "block": -1.0, "ask_user": -1.0}, "]}")
    assert GuardModelBackend("g", scorer).decide(_request(small_build)).decision.decision == "block"


def test_allow_carries_no_codes_and_garbage_fails_closed(small_build: Path) -> None:
    allow = FakeScorer({"allow": 0.0, "block": -5.0, "ask_user": -5.0}, '"x.y"]}')
    assert GuardModelBackend("g", allow).decide(_request(small_build)).decision.reason_codes == []
    garbage = FakeScorer({"allow": -5.0, "block": 0.0, "ask_user": -5.0}, "<eos>")
    result = GuardModelBackend("g", garbage).decide(_request(small_build))
    assert result.error == "invalid_json"
    assert result.decision.decision == "ask_user"
    bad_code = FakeScorer({"allow": -5.0, "block": 0.0, "ask_user": -5.0}, '"NOT A CODE"]}')
    assert GuardModelBackend("g", bad_code).decide(_request(small_build)).error == "schema"


def test_the_prompt_lists_the_same_reasons_in_training_and_inference(small_build: Path) -> None:
    row = read_split(small_build, "train")[0]
    reasons = reasons_for(actions.get(row.world))
    assert set(reasons) == set(actions.get(row.world).caused_by_action)
    assert prompt(row.request, reasons).endswith("Answer (JSON):\n")


def test_export_writes_train_and_validation_only_and_skips_long_prompts(
    small_build: Path, tmp_path: Path
) -> None:
    counts = export(small_build, tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "grpo-train.jsonl",
        "sft-train.jsonl",
        "sft-validation.jsonl",
    ]
    assert counts["train"]["written"] == len(sft.read_rows(tmp_path / "sft-train.jsonl"))
    assert counts["train"]["written"] == len(grpo.read_rows(tmp_path / "grpo-train.jsonl"))
    tiny = export(small_build, tmp_path / "tiny", max_prompt_chars=10)
    assert tiny["train"]["written"] == 0
    assert tiny["train"]["skipped_too_long"] > 0


@pytest.mark.parametrize("name", sorted(p.name for p in CONFIGS.glob("sft-*.yaml")))
def test_the_sft_configs_validate_and_pin_a_revision(name: str) -> None:
    config = sft.SftConfig.load(CONFIGS / name)
    assert config.base_model in {"Qwen/Qwen2.5-0.5B", "Qwen/Qwen2.5-1.5B"}
    assert len(config.revision) == 40


def test_the_grpo_config_validates() -> None:
    config = grpo.GrpoConfig.load(CONFIGS / "grpo-qwen2.5-0.5b.yaml")
    assert config.weights.missed_mistake > config.weights.wrongful_block > 0


def test_the_scripts_check_their_data_without_importing_a_model_library(
    small_build: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    export(small_build, tmp_path / "sft")
    config = sft.SftConfig.load(CONFIGS / "sft-kaggle-t4-qwen2.5-0.5b.yaml")
    local = tmp_path / "sft.yaml"
    local.write_text(
        json.dumps(config.model_dump(mode="json") | {"data": str(tmp_path / "sft")}),
        encoding="utf-8",
    )
    assert sft.main(["--config", str(local), "--check"]) == 0
    rl = grpo.GrpoConfig.load(CONFIGS / "grpo-qwen2.5-0.5b.yaml")
    local_rl = tmp_path / "grpo.yaml"
    local_rl.write_text(
        json.dumps(rl.model_dump(mode="json") | {"data": str(tmp_path / "sft")}), encoding="utf-8"
    )
    assert grpo.main(["--config", str(local_rl), "--check"]) == 0
    assert "Qwen/Qwen2.5-0.5B@060db6499f32" in capsys.readouterr().out
    assert "torch" not in sys.modules and "trl" not in sys.modules


def test_training_rows_are_checked(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"
    path.write_text('{"prompt": "p"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="exactly prompt and completion"):
        sft.read_rows(path)
    path.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no rows"):
        sft.read_rows(path)


def test_the_training_requirements_pin_exact_versions() -> None:
    lines = [
        line for line in REQUIREMENTS.read_text().splitlines() if line and not line.startswith("#")
    ]
    pinned = {line.split("==")[0]: line.split("==")[1] for line in lines if "==" in line}
    assert pinned == {
        "torch": "2.14.1",
        "transformers": "5.20.2",
        "trl": "1.16.0",
        "peft": "0.21.2",
        "datasets": "5.1.0",
        "accelerate": "1.15.0",
    }


# ---------------------------------------------------------------- reward
W = RewardWeights()


@pytest.mark.parametrize(
    ("text", "label", "expected"),
    [
        ('{"decision": "block", "reason_codes": ["a.b"]}', "block", W.correct + W.reason_bonus),
        ('{"decision": "block", "reason_codes": []}', "block", W.correct),
        ('{"decision": "block", "reason_codes": []}', "allow", -W.wrongful_block),
        ('{"decision": "ask_user", "reason_codes": []}', "allow", -W.needless_ask),
        ('{"decision": "allow", "reason_codes": []}', "block", -W.missed_mistake),
        ('{"decision": "allow", "reason_codes": []}', "ask_user", -W.missed_mistake),
        ('{"decision": "ask_user", "reason_codes": []}', "block", -W.wrong_kind),
        ("block it", "block", -W.invalid),
        ('{"decision": "maybe", "reason_codes": []}', "block", -W.invalid),
        ('{"decision": "block", "reason_codes": [1]}', "block", -W.invalid),
    ],
)
def test_the_reward_penalises_wrongful_blocks_and_missed_mistakes_apart(
    text: str, label: str, expected: float
) -> None:
    assert reward(text, label, ["a.b"], W) == pytest.approx(expected)


def test_the_trl_reward_takes_the_label_columns() -> None:
    scores = trl_reward(W)(
        completions=['{"decision": "allow", "reason_codes": []}'] * 2,
        decision=["allow", "block"],
        reason_codes=["[]", '["a.b"]'],
        prompts=["p", "p"],
    )
    assert scores == [pytest.approx(W.correct + W.reason_bonus), -W.missed_mistake]


def test_the_reward_knows_the_same_decisions_as_the_guard() -> None:
    assert DECISIONS == guard.DECISIONS
    assert parse_completion('  {"decision": "allow", "reason_codes": ["x.y", "x.y"]} trailing') == (
        "allow",
        ["x.y"],
    )
