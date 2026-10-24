"""Background telemetry: what a normal day at the company looks like in the logs.

Generated from the company's accounts, workstations and scheduled jobs with a
seeded stream per account per day, so the same seed always gives the same
rows and one account's activity never shifts another's. Every row is before
the agent starts. The story's own beats are written on top of this.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from kit.rng import SeededRNG

from ..services.common import iso
from .format import NoiseConfig, Org, User

Row = dict[str, Any]

EVENT_FIELDS = (
    "host",
    "user",
    "src_ip",
    "dst_ip",
    "dst_port",
    "domain",
    "process",
    "parent_process",
    "action",
    "outcome",
    "bytes_out",
    "geo",
    "app",
    "sha256",
    "message",
)
"""Every column of a telemetry event besides ``id``, ``ts`` and ``source``."""

WORK_APPS = ("browser.exe", "mail-client.exe", "office-docs.exe", "chat-client.exe")
ADMIN_ACTIONS = ("list_users", "view_sign_in_logs", "view_audit_log")


def event(event_id: str, at: dt.datetime, source: str, **fields: Any) -> Row:
    """A telemetry row with every column present."""
    unknown = set(fields) - set(EVENT_FIELDS)
    if unknown:
        msg = f"unknown event fields {sorted(unknown)}"
        raise ValueError(msg)
    row: Row = {"id": event_id, "ts": iso(at), "source": source}
    for name in EVENT_FIELDS:
        row[name] = fields.get(name, "" if name == "message" else None)
    return row


class Background:
    """The background generator for one company, one window, one seed."""

    def __init__(
        self,
        org: Org,
        start: dt.datetime,
        config: NoiseConfig,
        rng: SeededRNG,
        skip_jobs: set[tuple[str, dt.datetime]],
    ) -> None:
        self.org = org
        self.start = start
        self.window_start = start - dt.timedelta(hours=config.window_hours)
        self.quiet = set(config.quiet)
        self.rng = rng
        self.skip_jobs = skip_jobs
        self.rows: list[Row] = []

    def generate(self) -> list[Row]:
        """Every background row, oldest first."""
        for day in self._days():
            for user in self.org.users:
                if self._active(user):
                    self._workday(user, day)
            for job in self.org.jobs:
                if job.host not in self.quiet and job.user not in self.quiet:
                    self._job_runs(job.id, day)
        return sorted(self.rows, key=lambda row: (row["ts"], row["id"]))

    def _days(self) -> list[dt.datetime]:
        first = self.window_start.replace(hour=0, minute=0, second=0, microsecond=0)
        days = []
        while first < self.start:
            days.append(first)
            first += dt.timedelta(days=1)
        return days

    def _active(self, user: User) -> bool:
        return (
            user.kind == "human"
            and user.workstation is not None
            and user.id not in self.quiet
            and user.workstation not in self.quiet
        )

    def _in_window(self, moment: dt.datetime) -> bool:
        return self.window_start <= moment < self.start

    def _add(self, label: str, at: dt.datetime, source: str, **fields: Any) -> None:
        if not self._in_window(at):
            return
        event_id = f"evt_{self.rng.child(f'event/{label}').token(10)}"
        self.rows.append(event(event_id, at, source, **fields))

    def _workday(self, user: User, day: dt.datetime) -> None:
        assert user.workstation is not None
        host = self.org.host(user.workstation)
        assert host is not None
        stream = self.rng.child(f"user/{user.id}/{day.date().isoformat()}")
        begin, end = user.baseline.hours_utc
        login = day + dt.timedelta(hours=begin, minutes=stream.randint(0, 45))
        label = f"{user.id}/{day.date().isoformat()}"
        office = self.org.office
        base = {"host": host.id, "user": user.id}
        if stream.chance(0.15):
            self._add(
                f"{label}/fail",
                login - dt.timedelta(minutes=1),
                "auth",
                **base,
                src_ip=host.ip,
                action="logon",
                outcome="failure",
                geo=office.location,
                app="windows",
                message="bad password",
            )
        self._add(
            f"{label}/logon",
            login,
            "auth",
            **base,
            src_ip=host.ip,
            action="logon",
            outcome="success",
            geo=office.location,
            app="windows",
        )
        self._add(
            f"{label}/sign-in",
            login + dt.timedelta(minutes=2),
            "auth",
            user=user.id,
            src_ip=office.egress_ip,
            action="sign_in",
            outcome="success",
            geo=office.location,
            app="mail",
        )
        for n, app in enumerate(stream.sample(WORK_APPS, 2)):
            self._add(
                f"{label}/app/{n}",
                login + dt.timedelta(minutes=3 + n),
                "process",
                **base,
                process=app,
                parent_process="explorer.exe",
                action="start",
            )
        day_end = day + dt.timedelta(hours=end)
        span = max(int((day_end - login).total_seconds()) - 600, 600)
        for n in range(stream.randint(3, 6)):
            site = stream.choice(self.org.common_domains)
            at = login + dt.timedelta(seconds=300 + stream.randint(0, span))
            self._add(
                f"{label}/dns/{n}",
                at,
                "dns",
                **base,
                domain=site.domain,
                dst_ip=site.ip,
                action="query",
                outcome="success",
            )
            self._add(
                f"{label}/web/{n}",
                at + dt.timedelta(seconds=1),
                "network",
                **base,
                src_ip=host.ip,
                dst_ip=site.ip,
                dst_port=443,
                domain=site.domain,
                process="browser.exe",
                action="connect",
                outcome="success",
                bytes_out=stream.randint(20_000, 2_000_000),
            )
        if "it-admins" in user.groups:
            for n, action in enumerate(stream.sample(ADMIN_ACTIONS, 2)):
                self._add(
                    f"{label}/admin/{n}",
                    login + dt.timedelta(minutes=20 + 47 * n),
                    "cloud",
                    user=user.id,
                    src_ip=office.egress_ip,
                    action=action,
                    outcome="success",
                    app="admin-console",
                )

    def _job_runs(self, job_id: str, day: dt.datetime) -> None:
        job = next(job for job in self.org.jobs if job.id == job_id)
        host = self.org.host(job.host)
        assert host is not None
        for n in range(24 // job.every_hours):
            at = day + dt.timedelta(minutes=job.minute_of_day + n * job.every_hours * 60)
            if (job.id, at) in self.skip_jobs:
                continue
            label = f"job/{job.id}/{iso(at)}"
            stream = self.rng.child(label)
            base = {"host": host.id, "user": job.user, "process": job.process}
            self._add(
                f"{label}/start", at, "process", **base, parent_process="scheduler", action="start"
            )
            self._add(
                f"{label}/dns",
                at + dt.timedelta(seconds=5),
                "dns",
                host=host.id,
                user=job.user,
                domain=job.domain,
                dst_ip=job.dst_ip,
                action="query",
                outcome="success",
            )
            jitter = stream.randint(-10, 10)
            self._add(
                f"{label}/transfer",
                at + dt.timedelta(seconds=10),
                "network",
                **base,
                src_ip=host.ip,
                dst_ip=job.dst_ip,
                dst_port=job.dst_port,
                domain=job.domain,
                action="connect",
                outcome="success",
                bytes_out=job.bytes_out * (100 + jitter) // 100,
            )


def job_run_at(org: Org, job_id: str, at: dt.datetime) -> bool:
    """Whether ``job_id`` runs at exactly ``at``."""
    job = next((job for job in org.jobs if job.id == job_id), None)
    if job is None:
        return False
    minutes = at.hour * 60 + at.minute
    return at.second == 0 and (minutes - job.minute_of_day) % (job.every_hours * 60) == 0


__all__ = ["EVENT_FIELDS", "Background", "event", "job_run_at"]
