"""The client for the payments environment, over OpenEnv's WebSocket session."""

from typing import Any

from openenv.core import EnvClient
from openenv.core.client_types import StepResult

from .models import PaymentsAction, PaymentsObservation, PaymentsState

OBSERVATION_FIELDS = ("text", "data", "error_code", "sim_time", "tools")
STATE_FIELDS = ("episode_id", "step_count", "world", "task_id", "sim_time", "done")


class PaymentsEnv(EnvClient[PaymentsAction, PaymentsObservation, PaymentsState]):  # type: ignore[valid-type]
    """A session on the payments environment.

    Example::

        with PaymentsEnv(base_url="http://localhost:8000").sync() as env:
            result = env.reset(task_id="rails-gym-autopay-mandate")
            print(result.observation.text)                         # the user's request
            print([t["name"] for t in result.observation.tools])   # what the task allows
            result = env.step(PaymentsAction(tool="list_mandates", args={}))
    """

    def _step_payload(self, action: Any) -> dict[str, Any]:
        """A tool call as JSON."""
        return {"tool": action.tool, "args": dict(action.args)}

    def _parse_result(self, payload: dict[str, Any]) -> StepResult[Any]:
        """The server's reply as a ``StepResult`` of a ``PaymentsObservation``."""
        data = payload.get("observation", {})
        observation = PaymentsObservation(
            **{key: data[key] for key in OBSERVATION_FIELDS if key in data},
            done=payload.get("done", False),
            reward=payload.get("reward"),
            metadata=payload.get("metadata", data.get("metadata", {})),
        )
        return StepResult(
            observation=observation,
            reward=payload.get("reward"),
            done=payload.get("done", False),
            metadata=payload.get("metadata"),
        )

    def _parse_state(self, payload: dict[str, Any]) -> Any:
        """The episode's bookkeeping."""
        return PaymentsState(**{key: payload[key] for key in STATE_FIELDS if key in payload})


__all__ = ["PaymentsEnv"]
