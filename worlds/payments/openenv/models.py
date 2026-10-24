"""The action, observation and state types: the kit's OpenEnv adapter's, named for this world.

One action type, a tool call: ``PaymentsAction(tool="get_balance", args={})``. ``reset`` returns
the task's instruction and, in ``tools``, every tool the task allows with its JSON Schema.
"""

from kit.openenv_adapter import environment_class

_ENVIRONMENT = environment_class()

PaymentsAction = _ENVIRONMENT.Action
PaymentsObservation = _ENVIRONMENT.Observation
PaymentsState = _ENVIRONMENT.State

__all__ = ["PaymentsAction", "PaymentsObservation", "PaymentsState"]
