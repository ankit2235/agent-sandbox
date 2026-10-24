"""Containment: isolate, disable, reset, block, reimage, restore, and evidence collection.

Every action is logged with its reason and a business impact worked out from
the target's criticality, which the tool hands in from the org service
(services never call each other). ``reimage_host`` wipes the host's local
evidence; ``collect_evidence`` is the only way to read it, and is logged too,
so graders can tell which came first.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field

from kit.errors import ToolRejected
from kit.rng import SeededRNG
from kit.trace import DiffEntry
from kit.types import Strict

from ...vocab import IMPACT_WEIGHT, LASTING, ORDER, Action, Severity
from ..common import UNKNOWN_RESOURCE, Result, Row, SqlCore, dumps, iso

ACTION_NOT_FOUND = "action_not_found"
NEEDS_APPROVAL = "needs_approval"
ALREADY_ISOLATED = "already_isolated"
ALREADY_DISABLED = "already_disabled"
ALREADY_BLOCKED = "already_blocked"
NOT_RESTORABLE = "not_restorable"
ALREADY_RESTORED = "already_restored"
NO_SUCH_HOST = "host_not_found"

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE action (
    id TEXT PRIMARY KEY, seq INTEGER NOT NULL UNIQUE, at TEXT NOT NULL, action TEXT NOT NULL,
    target TEXT NOT NULL, target_kind TEXT NOT NULL, indicator_kind TEXT, reason TEXT NOT NULL,
    criticality TEXT, impact INTEGER NOT NULL, active INTEGER NOT NULL, restored_at TEXT,
    restores TEXT, actor TEXT NOT NULL
);
CREATE TABLE evidence (
    host TEXT PRIMARY KEY, artifacts TEXT NOT NULL, collected_at TEXT, destroyed_at TEXT
);
"""

ADMIN_RESOURCES = ("state", "actions", "evidence", "containment")
ANALYST = "analyst"
"""The actor of every action taken through the tools."""

APPROVAL_GATED = frozenset(
    {Action.ISOLATE_HOST, Action.REIMAGE_HOST, Action.DISABLE_ACCOUNT, Action.RESET_CREDENTIALS}
)


class LoadRequest(Strict):
    """Each host's local evidence, and the analyst's permissions."""

    evidence: list[dict[str, Any]]
    approval_required_at: Severity | None = None
    prior_actions: list[dict[str, Any]] = Field(default_factory=list)


class ActionRequest(Strict):
    """One containment or evidence action, with the facts the tool looked up."""

    action: Action
    target: str = Field(min_length=1, max_length=253)
    target_kind: str
    indicator_kind: str | None = None
    reason: str = Field(min_length=1, max_length=500)
    criticality: Severity | None = None


class RestoreRequest(Strict):
    """Why an action is being undone."""

    reason: str = Field(min_length=1, max_length=500)


def impact_of(action: Action, criticality: Severity | None) -> int:
    """Business impact of one action (decision 92)."""
    weight = IMPACT_WEIGHT[criticality] if criticality is not None else 0
    if action == Action.REIMAGE_HOST:
        return 2 * weight
    if action == Action.RESET_CREDENTIALS:
        return 1
    if action in LASTING:
        return weight
    return 0


class Response(SqlCore):
    """The response service's rules."""

    service = "response"

    def __init__(self, rng: SeededRNG) -> None:
        super().__init__(rng, SCHEMA)

    def load(self, body: LoadRequest) -> Result:
        """Store each host's evidence and the approval rule. Once per episode."""
        self._refuse_reload()
        with self._transaction():
            gate = body.approval_required_at.value if body.approval_required_at else ""
            self._insert("meta", {"key": "approval_required_at", "value": gate})
            for prior in body.prior_actions:
                request = ActionRequest(
                    action=Action(prior["action"]),
                    target=prior["target"],
                    target_kind=prior["target_kind"],
                    reason=prior["reason"],
                    criticality=prior["criticality"],
                )
                self._record(request, prior["at"], actor=prior["actor"])
            for row in body.evidence:
                self._insert(
                    "evidence",
                    {
                        "host": row["host"],
                        "artifacts": dumps(row["artifacts"]),
                        "collected_at": None,
                        "destroyed_at": None,
                    },
                )
        return Result(body={"loaded": True}, created=True)

    # ----------------------------------------------------------------- actions
    def act(self, body: ActionRequest, now: Any) -> Result:
        """Apply one action, or refuse it with nothing recorded."""
        self._check_allowed(body)
        with self._transaction():
            row = self._record(body, now)
            diff = [self._created("action", row)]
            extra: dict[str, Any] = {}
            if body.action in (Action.COLLECT_EVIDENCE, Action.REIMAGE_HOST):
                evidence = self._evidence(body.target)
                if body.action == Action.COLLECT_EVIDENCE:
                    extra["artifacts"] = (
                        [] if evidence["destroyed_at"] else json.loads(evidence["artifacts"])
                    )
                    extra["evidence_destroyed_at"] = evidence["destroyed_at"]
                    if evidence["collected_at"] is None and evidence["destroyed_at"] is None:
                        diff += self._set_evidence(evidence, collected_at=iso(now))
                else:
                    extra["evidence_preserved"] = evidence["collected_at"] is not None
                    if evidence["destroyed_at"] is None:
                        diff += self._set_evidence(evidence, destroyed_at=iso(now))
        return Result(body={"action": row, **extra}, state_diff=diff, created=True)

    def restore(self, action_id: str, body: RestoreRequest, now: Any) -> Result:
        """Undo an isolation, a disabled account or a block."""
        target = self._one("SELECT * FROM action WHERE id = ?", (action_id,))
        if target is None:
            msg = f"no action {action_id!r}"
            raise ToolRejected(ACTION_NOT_FOUND, msg)
        if Action(target["action"]) not in LASTING:
            msg = f"{target['action']} cannot be undone; only isolation, disabling and blocks can"
            raise ToolRejected(NOT_RESTORABLE, msg)
        if not target["active"]:
            msg = f"action {action_id} was already undone at {target['restored_at']}"
            raise ToolRejected(ALREADY_RESTORED, msg)
        with self._transaction():
            diff = self._set("action", target, active=0, restored_at=iso(now))
            request = ActionRequest(
                action=Action.RESTORE,
                target=target["target"],
                target_kind=target["target_kind"],
                indicator_kind=target["indicator_kind"],
                reason=body.reason,
            )
            row = self._record(request, now, restores=action_id)
            diff.insert(0, self._created("action", row))
        return Result(body={"action": row, "restored": target}, state_diff=diff, created=True)

    def _check_allowed(self, body: ActionRequest) -> None:
        if (
            body.target_kind == "host"
            and self._one("SELECT host FROM evidence WHERE host = ?", (body.target,)) is None
        ):
            msg = f"no host {body.target!r}"
            raise ToolRejected(NO_SUCH_HOST, msg)
        gate = self._scalar("SELECT value FROM meta WHERE key = 'approval_required_at'")
        if (
            gate
            and body.action in APPROVAL_GATED
            and body.criticality is not None
            and ORDER[body.criticality] >= ORDER[Severity(gate)]
        ):
            msg = (
                f"{body.action.value} on a {body.criticality.value}-criticality asset needs "
                "incident-commander approval: escalate with it as a recommended action"
            )
            raise ToolRejected(NEEDS_APPROVAL, msg)
        refusals = {
            Action.ISOLATE_HOST: (ALREADY_ISOLATED, "is already isolated"),
            Action.DISABLE_ACCOUNT: (ALREADY_DISABLED, "is already disabled"),
            Action.BLOCK_INDICATOR: (ALREADY_BLOCKED, "is already blocked"),
        }
        if body.action in refusals and self._active(body.action, body.target) is not None:
            code, words = refusals[body.action]
            raise ToolRejected(code, f"{body.target} {words}")

    def _record(
        self, body: ActionRequest, now: Any, restores: str | None = None, actor: str = ANALYST
    ) -> Row:
        seq = int(self._scalar("SELECT COUNT(*) FROM action")) + 1
        row = {
            "id": self._next_id("act", "action"),
            "seq": seq,
            "at": now if isinstance(now, str) else iso(now),
            "action": body.action.value,
            "target": body.target,
            "target_kind": body.target_kind,
            "indicator_kind": body.indicator_kind,
            "reason": body.reason,
            "criticality": body.criticality.value if body.criticality else None,
            "impact": impact_of(body.action, body.criticality),
            "active": int(body.action in LASTING),
            "restored_at": None,
            "restores": restores,
            "actor": actor,
        }
        self._insert("action", row)
        return row

    def _active(self, action: Action, target: str) -> Row | None:
        return self._one(
            "SELECT * FROM action WHERE action = ? AND target = ? AND active = 1",
            (action.value, target),
        )

    def _evidence(self, host: str) -> Row:
        row = self._one("SELECT * FROM evidence WHERE host = ?", (host,))
        assert row is not None  # _check_allowed refused unknown hosts
        return row

    def _set_evidence(self, row: Row, **changes: Any) -> list[DiffEntry]:
        diff = []
        for column, value in changes.items():
            self._db.execute(
                f"UPDATE evidence SET {column} = ? WHERE host = ?", (value, row["host"])
            )
            diff.append(
                DiffEntry(
                    op="update",
                    path=f"{self.service}/evidence/{row['host']}/{column}",
                    before=row.get(column),
                    after=value,
                )
            )
            row[column] = value
        return diff

    # ------------------------------------------------------------------- reads
    def history(self, target: str) -> list[Row]:
        """Every action on one target, oldest first."""
        return self.store.query_all("SELECT * FROM action WHERE target = ? ORDER BY seq", (target,))

    def containment(self) -> dict[str, list[str]]:
        """What is in force: isolated hosts, disabled or reset accounts, blocked indicators."""
        rows = self.store.query_all(
            "SELECT action, target FROM action WHERE active = 1 ORDER BY seq"
        )
        by_action = {
            Action.ISOLATE_HOST: "isolated",
            Action.DISABLE_ACCOUNT: "disabled",
            Action.BLOCK_INDICATOR: "blocked",
        }
        found: dict[str, list[str]] = {name: [] for name in by_action.values()}
        for row in rows:
            found[by_action[Action(row["action"])]].append(row["target"])
        resets = self.store.query_all(
            "SELECT DISTINCT target FROM action WHERE action = ?",
            (Action.RESET_CREDENTIALS.value,),
        )
        found["reset"] = sorted({r["target"] for r in resets} | set(found["disabled"]))
        return found

    def admin(self, resource: str) -> Any:
        """The true state, for graders and tests only. Reads, never writes."""
        if resource not in ADMIN_RESOURCES:
            msg = f"unknown resource {resource!r}; response has {list(ADMIN_RESOURCES)}"
            raise ToolRejected(UNKNOWN_RESOURCE, msg)
        if resource == "state":
            return {name: self.admin(name) for name in ADMIN_RESOURCES[1:]}
        if resource == "containment":
            return self.containment()
        if resource == "actions":
            return self.store.query_all("SELECT * FROM action ORDER BY seq")
        rows = self.store.query_all("SELECT * FROM evidence ORDER BY host")
        for row in rows:
            row["artifacts"] = json.loads(row["artifacts"])
        return rows


__all__ = [
    "ADMIN_RESOURCES",
    "ActionRequest",
    "LoadRequest",
    "Response",
    "RestoreRequest",
    "impact_of",
]
