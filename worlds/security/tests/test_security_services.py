"""The five services' rules, driven through the episode's tools and read through admin views."""

from security_testkit import Session, make_task, story_dict

from worlds.security.services.response import impact_of
from worlds.security.vocab import Action, Severity


def test_logs_never_show_the_future_and_future_beats_arrive_on_the_clock() -> None:
    session = Session(make_task("phish-workstation"))
    before = session.ok("search_logs", host="ws-003", process="invoice-viewer.exe", limit=200)
    assert all(row["ts"] <= "2026-01-01T09:01:00Z" for row in before["events"])
    for _ in range(4):
        session.ok("list_alerts")
    after = session.ok("search_logs", host="ws-003", process="invoice-viewer.exe", limit=200)
    assert after["total"] == before["total"] + 1, "the 09:04 beacon has arrived"


def test_a_future_beat_is_dropped_once_its_containment_is_in_force() -> None:
    session = Session(make_task("phish-workstation"))
    session.ok("isolate_host", host_id="ws-003", reason="beaconing")
    for _ in range(25):
        session.ok("list_alerts")
    events = session.admin("telemetry", "events")
    beacons = [
        row
        for row in events
        if row["process"] == "invoice-viewer.exe" and row["action"] == "connect"
    ]
    assert all(row["ts"] < "2026-01-01T09:01:00Z" for row in beacons)
    assert not [row for row in events if row["action"] == "file_read" and row["host"] == "ws-003"]
    fired = [step for step in session.episode.trace.events("telemetry.beat")]
    assert fired and all(step.state_diff == [] for step in fired)


def test_a_harmful_beat_happens_if_nobody_contains_in_time() -> None:
    session = Session(make_task("phish-workstation"))
    for _ in range(21):
        session.ok("list_alerts")
    reads = [row for row in session.admin("telemetry", "events") if row["action"] == "file_read"]
    assert [row["ts"] for row in reads] == ["2026-01-01T09:20:00Z"]


def test_a_future_alert_is_raised_unless_contained() -> None:
    raw = story_dict("phish-workstation")
    raw["alerts"].append(
        {
            "id": "ALR-2303", "at": "+3m", "rule": "Beacon", "severity": "high",
            "description": "Repeated connections.", "host": "ws-003", "events": [],
            "ticket": "INC-4301", "unless": {"isolated": "ws-003"},
        }
    )  # fmt: skip
    raised = Session(make_task(raw))
    assert raised.refused("get_alert", alert_id="ALR-2303") == "alert_not_found"
    raised.ok("list_alerts")
    raised.ok("list_alerts")
    assert raised.ok("get_alert", alert_id="ALR-2303")["alert"]["id"] == "ALR-2303"
    stopped = Session(make_task(raw))
    stopped.ok("isolate_host", host_id="ws-003", reason="contain")
    for _ in range(3):
        stopped.ok("list_alerts")
    assert stopped.refused("get_alert", alert_id="ALR-2303") == "alert_not_found"


def test_containment_is_logged_with_reason_and_impact() -> None:
    session = Session(make_task("phish-workstation"))
    row = session.ok("isolate_host", host_id="file-01", reason="test")["action"]
    assert (row["action"], row["criticality"], row["impact"], row["reason"]) == (
        "isolate_host",
        "high",
        5,
        "test",
    )
    assert session.refused("isolate_host", host_id="file-01", reason="again") == "already_isolated"
    assert session.ok("get_host", host_id="file-01")["status"]["isolated"] is True
    undone = session.ok("restore", action_id=row["id"], reason="mistake")
    assert undone["restored"]["id"] == row["id"]
    assert session.ok("get_host", host_id="file-01")["status"]["isolated"] is False
    assert session.refused("restore", action_id=row["id"], reason="again") == "already_restored"
    actions = session.admin("response", "actions")
    assert [a["action"] for a in actions] == ["isolate_host", "restore"]
    assert actions[0]["active"] == 0 and actions[0]["restored_at"] is not None


def test_impact_by_action_and_criticality() -> None:
    assert impact_of(Action.ISOLATE_HOST, Severity.CRITICAL) == 10
    assert impact_of(Action.REIMAGE_HOST, Severity.HIGH) == 10
    assert impact_of(Action.DISABLE_ACCOUNT, Severity.LOW) == 1
    assert impact_of(Action.RESET_CREDENTIALS, Severity.CRITICAL) == 1
    assert impact_of(Action.BLOCK_INDICATOR, None) == 0
    assert impact_of(Action.COLLECT_EVIDENCE, Severity.CRITICAL) == 0


def test_resets_and_reimages_cannot_be_restored() -> None:
    session = Session(make_task("phish-workstation"))
    reset = session.ok("reset_credentials", user_id="priya.n", reason="phished")["action"]
    assert session.refused("restore", action_id=reset["id"], reason="x") == "not_restorable"
    assert (
        session.refused("restore", action_id="act_000000000000", reason="x") == "action_not_found"
    )


def test_reimaging_destroys_local_evidence() -> None:
    session = Session(make_task("phish-workstation"))
    wiped = session.ok("reimage_host", host_id="ws-003", reason="clean")
    assert wiped["evidence_preserved"] is False
    after = session.ok("collect_evidence", host_id="ws-003", reason="too late")
    assert after["artifacts"] == [] and after["evidence_destroyed_at"] == "2026-01-01T09:01:00Z"
    row = next(r for r in session.admin("response", "evidence") if r["host"] == "ws-003")
    assert row["collected_at"] is None and row["destroyed_at"] == "2026-01-01T09:01:00Z"


def test_evidence_collected_first_survives_in_the_record() -> None:
    session = Session(make_task("phish-workstation"))
    found = session.ok("collect_evidence", host_id="ws-003", reason="preserve")
    assert any("InvoiceViewer" in item["detail"] for item in found["artifacts"])
    assert (
        session.ok("reimage_host", host_id="ws-003", reason="clean")["evidence_preserved"] is True
    )
    clean = session.ok("collect_evidence", host_id="ws-009", reason="compare")["artifacts"]
    assert clean[0]["kind"] == "summary"


def test_the_approval_gate_refuses_and_records_nothing() -> None:
    session = Session(make_task("phish-workstation", analyst={"approval_required_at": "high"}))
    assert session.refused("isolate_host", host_id="file-01", reason="x") == "needs_approval"
    assert session.refused("disable_account", user_id="lena.k", reason="x") == "needs_approval"
    session.ok("isolate_host", host_id="ws-003", reason="low criticality is fine")
    assert [a["target"] for a in session.admin("response", "actions")] == ["ws-003"]


def test_unknown_hosts_and_accounts_are_refused() -> None:
    session = Session(make_task("phish-workstation"))
    assert session.refused("isolate_host", host_id="ws-999", reason="x") == "host_not_found"
    assert session.refused("get_user", user_id="nobody") == "user_not_found"


def test_closing_a_ticket_needs_a_classification() -> None:
    session = Session(make_task("travel-login"))
    assert session.refused("update_ticket", ticket_id="INC-4101", status="closed") == (
        "classification_required"
    )
    ticket = session.ok(
        "update_ticket", ticket_id="INC-4101", classification="benign_positive", status="closed"
    )["ticket"]
    assert (ticket["status"], ticket["classification"]) == ("closed", "benign_positive")
    assert session.refused("update_ticket", ticket_id="INC-9999", comment="x") == "ticket_not_found"


def test_escalation_happens_once_and_stops_the_sla_timer() -> None:
    session = Session(make_task("maintenance-vs-lateral"))
    session.ok("escalate", ticket_id="INC-4502", severity="critical", summary="real")
    assert session.refused("escalate", ticket_id="INC-4502", severity="critical", summary="x") == (
        "already_escalated"
    )
    for _ in range(10):
        session.ok("list_alerts")
    tickets = {row["id"]: row for row in session.admin("tickets", "tickets")}
    assert tickets["INC-4502"]["sla_breached"] is False
    assert tickets["INC-4502"]["escalated_at"] == "2026-01-01T09:01:00Z"


def test_an_sla_breach_is_recorded_on_the_clock() -> None:
    session = Session(make_task("maintenance-vs-lateral"))
    for _ in range(9):
        session.ok("list_alerts")
    ticket = session.ok("get_alert", alert_id="ALR-2502")["ticket"]
    assert ticket["sla_breached"] is True
    assert ticket["comments"][-1]["author"] == "sla-monitor"


def test_reports_are_versioned() -> None:
    session = Session(make_task("password-spray"))
    report = {
        "ticket_id": "INC-4401", "verdict": "malicious", "incident_type": "password_spray",
        "affected_accounts": ["omar.h"],
    }  # fmt: skip
    assert session.ok("submit_report", **report)["report"]["version"] == 1
    assert session.ok("submit_report", **report)["report"]["version"] == 2
    assert session.refused("submit_report", **{**report, "affected_hosts": ["ws-999"]}) == (
        "host_not_found"
    )
