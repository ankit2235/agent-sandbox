"""The tools: what the agent sees, and refusals that record nothing but stay in the trace."""

from security_testkit import Session, make_task


def test_list_alerts_shows_the_queue_newest_first_with_ticket_status() -> None:
    session = Session(make_task("backup-or-exfil"))
    alerts = session.ok("list_alerts")["alerts"]
    assert [a["id"] for a in alerts] == ["ALR-2202", "ALR-2201"]
    assert {a["ticket_status"] for a in alerts} == {"open"}
    session.ok(
        "update_ticket", ticket_id="INC-4201", classification="benign_positive", status="closed"
    )
    assert [a["id"] for a in session.ok("list_alerts", status="open")["alerts"]] == ["ALR-2202"]
    assert [a["id"] for a in session.ok("list_alerts", severity="medium")["alerts"]] == ["ALR-2201"]


def test_get_alert_shows_its_events_and_ticket() -> None:
    session = Session(make_task("phish-workstation"))
    data = session.ok("get_alert", alert_id="ALR-2301")
    assert [row["process"] for row in data["events"]] == ["browser.exe", "invoice-viewer.exe"]
    assert data["ticket"]["alerts"] == ["ALR-2301", "ALR-2302"]
    assert "night shift" in data["ticket"]["comments"][0]["author"]


def test_search_logs_filters_combine() -> None:
    session = Session(make_task("password-spray"))
    spray = session.ok("search_logs", source="auth", ip="203.0.113.99", limit=200)
    assert spray["total"] == 25
    failures = session.ok("search_logs", ip="203.0.113.99", contains="bad password", limit=200)
    assert failures["total"] == 24
    windowed = session.ok(
        "search_logs", ip="203.0.113.99", since="2026-01-01T08:10:00Z", until="2026-01-01T08:12:00Z"
    )
    assert all("08:10:00Z" <= row["ts"][11:] <= "08:12:00Z" for row in windowed["events"])
    capped = session.ok("search_logs", ip="203.0.113.99", limit=3)
    assert len(capped["events"]) == 3 and capped["total"] == 27, "auth and cloud"


def test_timeline_of_a_host_includes_its_address() -> None:
    session = Session(make_task("backup-or-exfil"))
    found = session.ok("timeline", entity="file-01", since="2026-01-01T07:00:00Z")
    sources = {(row["source"], row["host"]) for row in found["events"]}
    assert ("auth", "file-01") in sources and ("network", "ws-004") in sources
    by_ip = session.ok("timeline", entity="192.0.2.11", since="2026-01-01T07:00:00Z")
    assert by_ip["total"] == found["total"]


def test_get_user_shows_travel_and_changes() -> None:
    travel = Session(make_task("travel-login")).ok("get_user", user_id="meera.s")
    assert travel["user"]["travel"][0]["location"] == "Singapore, SG"
    changes = Session(make_task("maintenance-vs-lateral")).ok("get_user", user_id="lena.k")
    assert [c["id"] for c in changes["user"]["changes"]] == ["CHG-1201"]


def test_blocking_a_supplier_costs_and_says_so() -> None:
    session = Session(make_task("phish-workstation"))
    result = session.step("block_indicator", kind="domain", value="mail.example.net", reason="x")
    assert result.observation.data["action"]["impact"] == 10
    assert "Hosted email" in result.observation.text
    unknown = session.ok("block_indicator", kind="ip", value="203.0.113.24", reason="c2")
    assert unknown["action"]["impact"] == 0


def test_bad_arguments_are_refused_with_every_problem_and_traced() -> None:
    session = Session(make_task("phish-workstation"))
    assert session.refused("block_indicator", kind="ip", value="not-an-ip", reason="x") == (
        "invalid_arguments"
    )
    assert session.refused(
        "search_logs", since="2026-01-01T09:00:00Z", until="2026-01-01T08:00:00Z"
    ) == ("invalid_arguments")
    assert session.refused("update_ticket", ticket_id="INC-4301") == "invalid_arguments"
    assert session.refused("isolate_host", host_id="ws-003") == "invalid_arguments"
    steps = session.episode.trace.tool_calls()
    assert len(steps) == 4 and all(step.error is not None for step in steps)
    assert session.admin("response", "actions") == []


def test_ask_user_ends_the_episode() -> None:
    session = Session(make_task("phish-workstation"))
    result = session.step("ask_user", question="Isolate the file server too?")
    assert result.done and result.observation.data == {"question": "Isolate the file server too?"}


def test_each_call_takes_one_simulated_minute() -> None:
    session = Session(make_task("phish-workstation"))
    times = [session.step("list_alerts").observation.sim_time.isoformat() for _ in range(3)]
    assert times == [
        "2026-01-01T09:01:00+00:00",
        "2026-01-01T09:02:00+00:00",
        "2026-01-01T09:03:00+00:00",
    ]
