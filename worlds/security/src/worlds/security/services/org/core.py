"""The company directory: hosts, accounts, change records, scheduled jobs, key suppliers.

Read-only for the agent. Containment status (isolated, disabled) belongs to the
response service; the tools put the two together.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field

from kit.errors import ToolRejected
from kit.rng import SeededRNG
from kit.types import Strict

from ..common import UNKNOWN_RESOURCE, Result, Row, SqlCore, dumps

HOST_NOT_FOUND = "host_not_found"
USER_NOT_FOUND = "user_not_found"

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE host (
    id TEXT PRIMARY KEY, ip TEXT NOT NULL UNIQUE, role TEXT NOT NULL, os TEXT NOT NULL,
    owner TEXT, criticality TEXT NOT NULL, location TEXT NOT NULL, notes TEXT NOT NULL
);
CREATE TABLE account (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, title TEXT NOT NULL, department TEXT NOT NULL,
    kind TEXT NOT NULL, groups TEXT NOT NULL, privileged INTEGER NOT NULL,
    criticality TEXT NOT NULL, workstation TEXT, baseline TEXT NOT NULL, travel TEXT NOT NULL
);
CREATE TABLE change_record (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, actor TEXT NOT NULL, hosts TEXT NOT NULL,
    start_at TEXT NOT NULL, end_at TEXT NOT NULL, approved_by TEXT NOT NULL
);
CREATE TABLE job (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, host TEXT NOT NULL, user TEXT NOT NULL,
    process TEXT NOT NULL, schedule TEXT NOT NULL, destination TEXT NOT NULL
);
CREATE TABLE business_indicator (
    kind TEXT NOT NULL, value TEXT NOT NULL, purpose TEXT NOT NULL, criticality TEXT NOT NULL,
    PRIMARY KEY (kind, value)
);
"""

ADMIN_RESOURCES = ("state", "hosts", "accounts", "changes", "jobs", "business_indicators")


class LoadRequest(Strict):
    """A resolved company, times already absolute (the episode converts them)."""

    name: str
    domain: str
    office: dict[str, Any]
    hosts: list[dict[str, Any]]
    users: list[dict[str, Any]]
    changes: list[dict[str, Any]] = Field(default_factory=list)
    jobs: list[dict[str, Any]] = Field(default_factory=list)
    business_indicators: list[dict[str, Any]] = Field(default_factory=list)


class Directory(SqlCore):
    """The org service's rules."""

    service = "org"

    def __init__(self, rng: SeededRNG) -> None:
        super().__init__(rng, SCHEMA)

    def load(self, body: LoadRequest) -> Result:
        """Store the company. Once per episode."""
        self._refuse_reload()
        with self._transaction():
            for key in ("name", "domain"):
                self._insert("meta", {"key": key, "value": getattr(body, key)})
            self._insert("meta", {"key": "office", "value": dumps(body.office)})
            for host in body.hosts:
                self._insert("host", {**host, "notes": host.get("notes", "")})
            for user in body.users:
                self._insert(
                    "account",
                    {
                        **user,
                        "groups": dumps(user["groups"]),
                        "privileged": int(user["privileged"]),
                        "baseline": dumps(user["baseline"]),
                        "travel": dumps(user["travel"]),
                    },
                )
            for change in body.changes:
                self._insert(
                    "change_record",
                    {
                        "id": change["id"],
                        "title": change["title"],
                        "actor": change["actor"],
                        "hosts": dumps(change["hosts"]),
                        "start_at": change["start"],
                        "end_at": change["end"],
                        "approved_by": change["approved_by"],
                    },
                )
            for job in body.jobs:
                first = job["minute_of_day"]
                self._insert(
                    "job",
                    {
                        "id": job["id"],
                        "title": job["title"],
                        "host": job["host"],
                        "user": job["user"],
                        "process": job["process"],
                        "schedule": (
                            f"every {job['every_hours']} h"
                            f" from {first // 60:02d}:{first % 60:02d} UTC"
                        ),
                        "destination": f"{job['domain']} ({job['dst_ip']}:{job['dst_port']})",
                    },
                )
            for row in body.business_indicators:
                self._insert("business_indicator", dict(row))
        return Result(body={"loaded": True}, created=True)

    # ------------------------------------------------------------------ reads
    def host(self, host_id: str) -> Row:
        """A host, with the change records and jobs that concern it."""
        row = self._one("SELECT * FROM host WHERE id = ?", (host_id,))
        if row is None:
            msg = f"no host {host_id!r}"
            raise ToolRejected(HOST_NOT_FOUND, msg)
        row["changes"] = [change for change in self._changes() if host_id in change["hosts"]]
        row["jobs"] = self.store.query_all(
            "SELECT * FROM job WHERE host = ? ORDER BY id", (host_id,)
        )
        return row

    def host_by_ip(self, ip: str) -> Row | None:
        """The host with this address, if the company has one."""
        return self._one("SELECT * FROM host WHERE ip = ?", (ip,))

    def user(self, user_id: str) -> Row:
        """An account, with the change records it may act under."""
        row = self._one("SELECT * FROM account WHERE id = ?", (user_id,))
        if row is None:
            msg = f"no account {user_id!r}"
            raise ToolRejected(USER_NOT_FOUND, msg)
        row = self._decoded(row)
        row["changes"] = [change for change in self._changes() if change["actor"] == user_id]
        return row

    def business_indicator(self, kind: str, value: str) -> Row | None:
        """A supplier address or domain the business depends on, if this is one."""
        return self._one(
            "SELECT * FROM business_indicator WHERE kind = ? AND value = ?", (kind, value)
        )

    def _changes(self) -> list[Row]:
        rows = self.store.query_all("SELECT * FROM change_record ORDER BY start_at, id")
        for row in rows:
            row["hosts"] = json.loads(row["hosts"])
        return rows

    @staticmethod
    def _decoded(row: Row) -> Row:
        for column in ("groups", "baseline", "travel"):
            row[column] = json.loads(row[column])
        row["privileged"] = bool(row["privileged"])
        return row

    def admin(self, resource: str) -> Any:
        """The true state, for graders and tests only. Reads, never writes."""
        if resource not in ADMIN_RESOURCES:
            msg = f"unknown resource {resource!r}; org has {list(ADMIN_RESOURCES)}"
            raise ToolRejected(UNKNOWN_RESOURCE, msg)
        if resource == "state":
            return {name: self.admin(name) for name in ADMIN_RESOURCES[1:]}
        if resource == "accounts":
            return [
                self._decoded(r) for r in self.store.query_all("SELECT * FROM account ORDER BY id")
            ]
        if resource == "changes":
            return self._changes()
        table = {"hosts": "host", "jobs": "job", "business_indicators": "business_indicator"}
        order = "kind, value" if resource == "business_indicators" else "id"
        return self.store.query_all(f"SELECT * FROM {table[resource]} ORDER BY {order}")


__all__ = ["ADMIN_RESOURCES", "HOST_NOT_FOUND", "USER_NOT_FOUND", "Directory", "LoadRequest"]
