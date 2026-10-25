"""The payments world's environment: the kit's OpenEnv adapter over the packaged task set.

Reached through the kit's world registry (the ``agent_sandbox.worlds`` entry point of the
installed agent-sandbox-bench package). ``reset(task_id=...)`` picks a task, or
``reset(split="v0.1", index=n)``; the default is the first task.

Most payments tasks end when the agent simply stops, which an OpenEnv client cannot say with a
world tool, so this environment adds one action of its own: ``finish_episode`` (no arguments)
grades the episode and ends it. The grader's score is the reward of the step that ends the
episode: ``finish_episode``, a terminal tool such as ``ask_user``, or the task's step cap.
"""

from typing import Any

from kit.openenv_adapter import environment_class
from kit.world import worlds

WORLD = "payments"
FINISH = "finish_episode"
FINISH_TOOL = {
    "name": FINISH,
    "description": "End the episode: you have done what the user asked. The episode is graded.",
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}


class PaymentsEnvironment(environment_class()):  # type: ignore[misc]
    """The kit's environment for the payments world, with ``finish_episode``."""

    def __init__(self) -> None:
        spec = worlds.get(WORLD)
        if FINISH in spec.tool_names:
            msg = f"{WORLD} has a tool named {FINISH!r}; the environment's own action would hide it"
            raise ValueError(msg)
        super().__init__(spec, spec.task_source())
        self._finished = False

    def reset(self, seed: int | None = None, episode_id: str | None = None, **kwargs: Any) -> Any:
        """Start an episode; the tools it lists end with ``finish_episode``."""
        self._finished = False
        observation = super().reset(seed=seed, episode_id=episode_id, **kwargs)
        observation.tools = [*observation.tools, FINISH_TOOL]
        return observation

    def step(self, action: Any, timeout_s: float | None = None, **kwargs: Any) -> Any:
        """A world tool call, or ``finish_episode``, which grades and ends the episode."""
        if self._finished:
            msg = "the episode has finished; call reset() to start another"
            raise RuntimeError(msg)
        if action.tool != FINISH:
            observation = super().step(action, timeout_s=timeout_s, **kwargs)
            self._finished = bool(observation.done)
            return observation
        grade = self.grade()
        self._finished = True
        return self.Observation(
            text=f"Episode finished. Score {grade.score:.2f}, passed: {grade.passed}.",
            sim_time=self.state.sim_time,
            done=True,
            reward=grade.score,
            metadata={"grade": grade.model_dump(mode="json")},
        )


def create_payments_environment() -> PaymentsEnvironment:
    """A fresh environment for one session, over the public payments-v0.1 tasks."""
    return PaymentsEnvironment()


__all__ = ["FINISH", "WORLD", "PaymentsEnvironment", "create_payments_environment"]
