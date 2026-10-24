"""The scenario format: stories validate, expand deterministically, and use reserved data only."""

import datetime as dt
import ipaddress
import json
import re

import pytest
from pydantic import ValidationError
from security_testkit import story_dict

from kit.clock import EPOCH
from kit.rng import SeededRNG
from worlds.security.scenarios import expand, load_org, load_story, story_ids
from worlds.security.scenarios.format import Story, parse_offset
from worlds.security.vocab import DEFAULT_SLA_MINUTES, is_documentation_ip, is_example_domain

IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
DOMAIN = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b")
FILE_NAMES = (".exe", ".docx", ".docm", ".zip", ".dat", ".csv", ".php", ".txt", ".dll")


def expanded(story: Story, seed: int = 1):
    return expand(story, EPOCH, SeededRNG(seed).child("scenario"), DEFAULT_SLA_MINUTES)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("-2h10m", -7_800), ("+25m", 1_500), ("0", 0), ("-1d", -86_400), ("-30s", -30), (90, 90)],
)
def test_offsets_read_as_seconds(text, seconds) -> None:
    assert parse_offset(text) == seconds


@pytest.mark.parametrize("text", ["", "-", "2 hours", "1h-5m", "10x"])
def test_bad_offsets_are_refused(text) -> None:
    with pytest.raises(ValueError, match="offset"):
        parse_offset(text)


@pytest.mark.parametrize("story_id", story_ids())
def test_every_packaged_story_expands(story_id) -> None:
    world = expanded(load_story(story_id))
    assert world.events and world.alerts and world.tickets
    assert all(row["ts"] <= "2026-01-01T09:00:00Z" for row in world.events)
    assert all(item["at"] > "2026-01-01T09:00:00Z" for item in world.future_events)
    assert len({row["id"] for row in world.events}) == len(world.events)


def _strings(value) -> list[str]:
    return [text for text in json.dumps(value).split('"') if text]


@pytest.mark.parametrize("story_id", story_ids())
def test_every_address_and_domain_is_reserved(story_id) -> None:
    """CONTRIBUTING rule 5 as a test: RFC 5737 addresses and RFC 2606 domains only."""
    world = expanded(load_story(story_id))
    payload = [world.org.model_dump(mode="json"), world.events, world.future_events, world.alerts]
    payload += [world.tickets, world.comments, world.evidence]
    for text in _strings(payload):
        for address in IPV4.findall(text):
            ipaddress.ip_address(address)
            assert is_documentation_ip(address), f"{story_id}: {address} in {text!r}"
        for domain in DOMAIN.findall(text.lower()):
            if domain.endswith(FILE_NAMES) or re.fullmatch(r"[\d.]+", domain):
                continue
            assert is_example_domain(domain), f"{story_id}: {domain} in {text!r}"


def test_the_same_seed_gives_identical_rows_and_another_seed_other_noise() -> None:
    story = load_story("phish-workstation")
    first, again, other = expanded(story, 1), expanded(story, 1), expanded(story, 2)
    assert first == again
    assert first.events != other.events


def test_story_events_keep_their_ids_whatever_the_noise() -> None:
    raw = story_dict("phish-workstation")
    busier = {**raw, "noise": {"window_hours": 48, "quiet": []}}
    first = expanded(Story.model_validate(raw))
    second = expanded(Story.model_validate(busier))
    assert len(second.events) > len(first.events)
    assert first.alerts == second.alerts


def test_quiet_accounts_get_no_background_activity() -> None:
    world = expanded(load_story("travel-login"))
    mine = [row for row in world.events if row["user"] == "meera.s"]
    assert len(mine) == 5, "only the story's own five events"


def test_a_job_run_the_story_replaces_is_left_out_of_the_background() -> None:
    world = expanded(load_story("backup-or-exfil"))
    at_six = [
        row for row in world.events
        if row["host"] == "backup-01" and row["ts"].startswith("2026-01-01T06:00")
    ]  # fmt: skip
    transfers = [row for row in at_six if row["source"] == "network"]
    assert len(transfers) == 1 and transfers[0]["bytes_out"] == 38_000_000_000


def test_the_company_uses_reserved_data_and_is_consistent() -> None:
    org = load_org("default")
    assert org.domain == "corp.example"
    assert all(is_documentation_ip(host.ip) for host in org.hosts)


# ------------------------------------------------------------------ refusals
def _edit(story_id: str, change) -> dict:
    raw = story_dict(story_id)
    change(raw)
    return raw


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda s: s["alerts"][0]["events"].append("nope"), "unknown beat nope"),
        (lambda s: s["beats"][0].update(src_ip="8.8.8.8"), "RFC 5737"),
        (lambda s: s["beats"][2].update(domain="login.real-bank.com"), "RFC 2606"),
        (
            lambda s: s["beats"][0].update(unless={"isolated": "ws-003"}),
            "only applies to the future",
        ),
        (lambda s: s["beats"][0].update(harmful=True), "harmful beat must be in the future"),
        (lambda s: s["beats"].append(dict(s["beats"][0])), "duplicate beat key"),
        (
            lambda s: s["ground_truth"]["tickets"].update({"INC-9998": "benign_positive"}),
            "classify exactly the story's tickets",
        ),
        (lambda s: s["alerts"][0].update(ticket="INC-9999"), "unknown ticket INC-9999"),
        (
            lambda s: s["ground_truth"].update(verdict="benign"),
            "a benign story has no true positive",
        ),
        (lambda s: s["ground_truth"].update(escalate=["INC-9999"]), "only true positives"),
    ],
)
def test_a_broken_story_does_not_load(change, message) -> None:
    with pytest.raises(ValidationError, match=message):
        Story.model_validate(_edit("phish-workstation", change))


def test_a_story_naming_an_unknown_host_does_not_expand() -> None:
    raw = _edit("phish-workstation", lambda s: s["beats"][0].update(host="ws-999"))
    with pytest.raises(ValueError, match="unknown host ws-999"):
        expanded(Story.model_validate(raw))


def test_an_sla_already_run_out_at_load_must_be_intended() -> None:
    raw = _edit("travel-login", lambda s: s["alerts"][0].update(at="-5h"))
    with pytest.raises(ValueError, match="has run out when the agent starts"):
        expanded(Story.model_validate(raw))
    raw["tickets"][0]["breached_at_load"] = True
    world = expanded(Story.model_validate(raw))
    due = dt.datetime.fromisoformat(world.tickets[0]["sla_due_at"])
    assert due < EPOCH


def test_saying_an_sla_ran_out_when_it_did_not_is_refused() -> None:
    raw = _edit("travel-login", lambda s: s["tickets"][0].update(breached_at_load=True))
    with pytest.raises(ValueError, match="has not run out"):
        expanded(Story.model_validate(raw))


def test_a_beat_claiming_a_job_run_must_be_at_one() -> None:
    def shift(s):
        s["beats"][3]["at"] = "-2h"

    with pytest.raises(ValueError, match="does not run at"):
        expanded(Story.model_validate(_edit("backup-or-exfil", shift)))
