"""Same seed and same calls give byte-identical state in all five services; reset is exact."""

from security_testkit import Session, make_task

from kit.scripted import run_script
from kit.types import ToolCall
from worlds.security import world_spec

SERVICES = ["alerts", "org", "response", "telemetry", "tickets"]


def calls() -> list[ToolCall]:
    """A fixed agent that touches every service and lets scheduled events fire."""
    raw = [
        ("list_alerts", {}),
        ("get_alert", {"alert_id": "ALR-2502"}),
        ("search_logs", {"user": "tomas.v", "limit": 20}),
        ("timeline", {"entity": "ws-011"}),
        ("collect_evidence", {"host_id": "ws-011", "reason": "preserve"}),
        ("isolate_host", {"host_id": "ws-011", "reason": "contain"}),
        ("isolate_host", {"host_id": "jump-01", "reason": "mistake"}),
        ("update_ticket", {"ticket_id": "INC-4501", "comment": "checking"}),
        ("get_host", {"host_id": "jump-01"}),
        ("list_alerts", {}),
        ("list_alerts", {}),
        ("reimage_host", {"host_id": "ws-011", "reason": "clean"}),
        ("escalate", {"ticket_id": "INC-4502", "severity": "critical", "summary": "late"}),
        ("submit_report", {"ticket_id": "INC-4502", "verdict": "malicious",
                           "incident_type": "lateral_movement"}),
    ]  # fmt: skip
    return [ToolCall(tool=tool, args=args) for tool, args in raw]


def run(seed: int) -> tuple[dict[str, bytes], str]:
    task = make_task("maintenance-vs-lateral", seed=seed)
    result = run_script(world_spec(), task, calls(), grade=False)
    return result.final_state, "\n".join(step.model_dump_json() for step in result.trace.steps)


def test_same_seed_same_calls_give_identical_bytes_in_every_service() -> None:
    first_state, first_trace = run(11)
    second_state, second_trace = run(11)
    assert sorted(first_state) == SERVICES
    assert first_state == second_state
    assert first_trace == second_trace


def test_a_different_seed_gives_a_different_run() -> None:
    assert run(11)[0]["telemetry"] != run(12)[0]["telemetry"]


def test_the_script_changes_every_writable_service_and_fires_events() -> None:
    session = Session(make_task("maintenance-vs-lateral", seed=11))
    for call in calls():
        session.episode.step(call)
    paths = {d.path.split("/")[0] for step in session.episode.trace.steps for d in step.state_diff}
    assert paths == {"response", "tickets", "telemetry"}
    events = {step.event for step in session.episode.trace.events()}
    assert events == {"telemetry.beat", "tickets.sla_due"}


def test_close_restores_every_service_exactly() -> None:
    session = Session(make_task("maintenance-vs-lateral", seed=11))
    baseline = session.episode.snapshot_all()
    for call in calls():
        session.episode.step(call)
    changed = session.episode.snapshot_all()
    assert {name for name in baseline if changed[name] != baseline[name]} == {
        "response", "telemetry", "tickets"
    }  # fmt: skip
    session.episode.close()
    assert session.episode.snapshot_all() == baseline


def test_admin_reads_change_nothing() -> None:
    session = Session(make_task("maintenance-vs-lateral", seed=11))
    for call in calls()[:8]:
        session.episode.step(call)
    before = session.episode.snapshot_all()
    for service in SERVICES:
        session.admin(service)
    assert session.episode.snapshot_all() == before
