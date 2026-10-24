"""FastAPI app serving the payments environment (``uvicorn server.app:app --port 8000``).

Endpoints come from OpenEnv's ``create_app``: ``/reset``, ``/step``, ``/state``, ``/schema``,
``/health``, the task API (``/payments_env/splits`` and friends) and the ``/ws`` session.
"""

from openenv.core.env_server.http_server import create_app

try:
    from ..models import PaymentsAction, PaymentsObservation, PaymentsState
    from .payments_env_environment import create_payments_environment
except ImportError:  # run from inside the folder, as in the Docker image
    from models import PaymentsAction, PaymentsObservation, PaymentsState  # type: ignore[no-redef]
    from server.payments_env_environment import create_payments_environment  # type: ignore[no-redef]

app = create_app(
    create_payments_environment,
    PaymentsAction,
    PaymentsObservation,
    env_name="payments_env",
    max_concurrent_envs=1,
    state_cls=PaymentsState,
)


def main() -> None:
    """``uv run --project . server [--port 8001]``."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
