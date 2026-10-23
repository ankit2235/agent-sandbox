"""After-payment operations over one SQLite database per episode.

Refund requests to merchants, disputes, and the user's inbox. Like commerce,
disputes never moves money: when a merchant agrees to a refund or a dispute is
won, the event's result carries a ``refund_due`` that the episode pays through
the rails, then reports back with :meth:`Disputes.record_refund`.

Rules that depend on other services' state (is the order really the wrong
item? is this payment really a duplicate?) are given those facts by the episode
at the moment they are decided, so every rule here reads its own database plus
the facts it was handed, and nothing else.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from kit.clock import ScheduledEvent
from kit.errors import ToolRejected
from kit.rng import SeededRNG
from kit.service import SqliteStore
from kit.trace import DiffEntry

from ...merchants import Behaviour
from ..commerce.pages import money
from ..common import Result, Row, SqlCore, iso, parse
from .models import (
    DisputeCreate,
    DisputeReason,
    DisputesConfig,
    DisputeStatus,
    EvidenceCreate,
    EvidenceKind,
    LoadRequest,
    MessageCreate,
    NoticeBatch,
    RefundOutcome,
    RefundRequestCreate,
    RefundRequestStatus,
    Rejection,
)

EVENT_REFUND_RESPONSE = "disputes.refund_response"
EVENT_REFUND_DEADLINE = "disputes.refund_deadline"
EVENT_DECIDE = "disputes.decide"
EVENT_MERCHANT_REPLY = "disputes.merchant_reply"
EVENT_DELIVER_MESSAGE = "disputes.deliver_message"

FACT_EVENTS = (EVENT_REFUND_RESPONSE, EVENT_DECIDE)
"""Events whose rule needs facts from the rails and commerce."""

ADMIN_RESOURCES = ("state", "merchants", "refund_requests", "disputes", "evidence", "messages")
SYSTEM_SENDER = "Sandbox Pay"

SCHEMA = """
CREATE TABLE disputes_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE merchant (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    handle TEXT NOT NULL,
    behaviour TEXT NOT NULL
) STRICT;

CREATE TABLE refund_request (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    merchant_id TEXT NOT NULL REFERENCES merchant (id),
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'open', 'accepted', 'partially_accepted', 'declined', 'expired', 'refund_failed'
    )),
    offered_paise INTEGER,
    refund_id TEXT,
    created_at TEXT NOT NULL,
    respond_by TEXT NOT NULL,
    responded_at TEXT
) STRICT;

CREATE TABLE dispute (
    id TEXT PRIMARY KEY,
    payment_id TEXT NOT NULL UNIQUE,
    order_id TEXT,
    merchant_id TEXT NOT NULL REFERENCES merchant (id),
    reason TEXT NOT NULL CHECK (reason IN ('wrong_item', 'not_received', 'duplicate_charge')),
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    description TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'won', 'lost')),
    decision TEXT,
    awarded_paise INTEGER,
    refund_id TEXT,
    created_at TEXT NOT NULL,
    respond_by TEXT NOT NULL,
    decided_at TEXT
) STRICT;

CREATE TABLE evidence (
    id TEXT PRIMARY KEY,
    dispute_id TEXT NOT NULL REFERENCES dispute (id),
    kind TEXT NOT NULL CHECK (kind IN ('order', 'payment', 'message', 'refund_request')),
    reference_id TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE message (
    id TEXT PRIMARY KEY,
    direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    sender TEXT NOT NULL,
    merchant_id TEXT,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    order_id TEXT,
    reply_to TEXT,
    created_at TEXT NOT NULL,
    read INTEGER NOT NULL CHECK (read IN (0, 1))
) STRICT;
"""


class Disputes(SqlCore):
    """Refund requests, disputes and the support inbox."""

    service = "disputes"

    def __init__(self, rng: SeededRNG, store: SqliteStore | None = None) -> None:
        super().__init__(rng, SCHEMA, store)
        self._config_cache: tuple[str, DisputesConfig] | None = None

    # ------------------------------------------------------------- plumbing
    def _config(self) -> DisputesConfig:
        row = self._one("SELECT value FROM disputes_config WHERE key = 'config'")
        if row is None:
            msg = "disputes has not been loaded with a task yet"
            raise ToolRejected(Rejection.INVALID_REQUEST, msg)
        raw = str(row["value"])
        if self._config_cache is None or self._config_cache[0] != raw:
            self._config_cache = (raw, DisputesConfig.model_validate_json(raw))
        return self._config_cache[1]

    def _merchant(self, merchant_id: str) -> Row:
        row = self._one("SELECT * FROM merchant WHERE id = ?", (merchant_id,))
        if row is None:
            msg = f"no merchant {merchant_id!r}"
            raise ToolRejected(Rejection.MERCHANT_NOT_FOUND, msg)
        return row

    @staticmethod
    def _behaviour(merchant: Row) -> Behaviour:
        return Behaviour.model_validate_json(str(merchant["behaviour"]))

    def _message(
        self,
        now: dt.datetime,
        *,
        direction: str,
        sender: str,
        subject: str,
        body: str,
        merchant_id: str | None = None,
        order_id: str | None = None,
        reply_to: str | None = None,
    ) -> list[DiffEntry]:
        row: Row = {
            "id": self._next_id("msg", "message"),
            "direction": direction,
            "sender": sender,
            "merchant_id": merchant_id,
            "subject": subject,
            "body": body,
            "order_id": order_id,
            "reply_to": reply_to,
            "created_at": iso(now),
            "read": int(direction == "out"),
        }
        self._insert("message", row)
        return [self._created("message", row)]

    # ----------------------------------------------------------------- load
    def load(self, request: LoadRequest, now: dt.datetime) -> Result:
        """Set up the inbox and the merchants' after-sale behaviour. Loaded once."""
        scheduled: list[ScheduledEvent] = []
        with self._transaction():
            if self._scalar("SELECT COUNT(*) FROM disputes_config"):
                msg = "disputes is already loaded; build a new service for a new task"
                raise ToolRejected(Rejection.ALREADY_LOADED, msg)
            self._db.executemany(
                "INSERT INTO disputes_config VALUES (?, ?)",
                [("config", request.config.model_dump_json()), ("user", request.user)],
            )
            for merchant in request.merchants:
                self._insert(
                    "merchant",
                    {
                        "id": merchant.id,
                        "name": merchant.name,
                        "handle": merchant.handle,
                        "behaviour": merchant.resolved().model_dump_json(),
                    },
                )
            known = {merchant.id for merchant in request.merchants}
            for index, seed in enumerate(request.config.inbox):
                if seed.merchant_id is not None and seed.merchant_id not in known:
                    msg = f"inbox message {index} names unknown merchant {seed.merchant_id!r}"
                    raise ToolRejected(Rejection.MERCHANT_NOT_FOUND, msg)
                if seed.after_seconds == 0:
                    self._deliver_seed(index, now)
                else:
                    scheduled.append(
                        ScheduledEvent(
                            at=now + dt.timedelta(seconds=seed.after_seconds),
                            name=EVENT_DELIVER_MESSAGE,
                            payload={"index": index},
                        )
                    )
        return Result(body={"loaded": {"inbox": len(request.config.inbox)}}, scheduled=scheduled)

    def _deliver_seed(self, index: int, now: dt.datetime) -> list[DiffEntry]:
        seed = self._config().inbox[index]
        return self._message(
            now,
            direction="in",
            sender=seed.sender,
            subject=seed.subject,
            body=seed.body,
            merchant_id=seed.merchant_id,
            order_id=seed.order_id,
        )

    # -------------------------------------------------------- refund requests
    def create_refund_request(self, request: RefundRequestCreate, now: dt.datetime) -> Result:
        """Ask a merchant for money back. One open request per order at a time."""
        if request.amount_paise <= 0:
            msg = "amount_paise must be a positive whole number of paise"
            raise ToolRejected(Rejection.INVALID_AMOUNT, msg)
        if request.amount_paise > request.refundable_paise:
            msg = f"at most {request.refundable_paise} paise of this payment can still be refunded"
            raise ToolRejected(Rejection.NOT_REFUNDABLE, msg)
        with self._transaction():
            settings = self._config().settings
            merchant = self._merchant(request.merchant_id)
            open_request = self._one(
                "SELECT id FROM refund_request WHERE order_id = ? AND status = 'open'",
                (request.order_id,),
            )
            if open_request is not None:
                msg = f"refund request {open_request['id']} for this order is still open"
                raise ToolRejected(Rejection.REFUND_REQUEST_OPEN, msg)
            respond_by = now + dt.timedelta(seconds=settings.refund_response_seconds)
            row: Row = {
                "id": self._next_id("rr", "refund_request"),
                "order_id": request.order_id,
                "payment_id": request.payment_id,
                "merchant_id": request.merchant_id,
                "amount_paise": request.amount_paise,
                "reason": request.reason,
                "status": RefundRequestStatus.OPEN.value,
                "offered_paise": None,
                "refund_id": None,
                "created_at": iso(now),
                "respond_by": iso(respond_by),
                "responded_at": None,
            }
            self._insert("refund_request", row)
            diff = [self._created("refund_request", row)]
            payload = {"request_id": row["id"]}
            scheduled = [ScheduledEvent(at=respond_by, name=EVENT_REFUND_DEADLINE, payload=payload)]
            behaviour = self._behaviour(merchant)
            reply_at = now + dt.timedelta(seconds=behaviour.refund_reply_after_seconds)
            if behaviour.refund_response != "ignore" and reply_at < respond_by:
                scheduled.insert(
                    0, ScheduledEvent(at=reply_at, name=EVENT_REFUND_RESPONSE, payload=payload)
                )
        return Result(
            body={"refund_request": row}, state_diff=diff, scheduled=scheduled, created=True
        )

    def _refund_response(
        self, request_id: str, facts: dict[str, Any], now: dt.datetime
    ) -> tuple[list[DiffEntry], Row | None]:
        """The merchant answers: pays back valid claims (in full or in part), declines others."""
        row = self._one("SELECT * FROM refund_request WHERE id = ?", (request_id,))
        if row is None or row["status"] != RefundRequestStatus.OPEN:
            return [], None
        merchant = self._merchant(str(row["merchant_id"]))
        behaviour = self._behaviour(merchant)
        valid = bool(
            facts.get("order_status") == "wrong_item"
            or facts.get("not_received")
            or facts.get("duplicate_of")
        )
        offered = int(row["amount_paise"])
        if behaviour.refund_response == "partial":
            offered = offered * behaviour.partial_refund_bps // 10_000
        offered = min(offered, int(facts.get("refundable_paise", 0)))
        if not valid or offered <= 0:
            why = (
                "the order was delivered as ordered"
                if not valid
                else "the payment has already been refunded"
            )
            diff = self._set(
                "refund_request",
                row,
                status=RefundRequestStatus.DECLINED.value,
                offered_paise=0,
                responded_at=iso(now),
            )
            diff += self._message(
                now,
                direction="in",
                sender=str(merchant["name"]),
                merchant_id=str(merchant["id"]),
                order_id=str(row["order_id"]),
                subject=f"Refund request {row['id']} declined",
                body=f"We cannot refund order {row['order_id']}: {why}.",
            )
            return diff, None
        status = (
            RefundRequestStatus.ACCEPTED
            if offered == row["amount_paise"]
            else RefundRequestStatus.PARTIALLY_ACCEPTED
        )
        diff = self._set(
            "refund_request", row, status=status.value, offered_paise=offered, responded_at=iso(now)
        )
        due = {
            "kind": "refund_request",
            "id": row["id"],
            "payment_id": row["payment_id"],
            "amount_paise": offered,
            "idempotency_key": f"refund-{row['id']}",
            "reason": f"refund request {row['id']}",
        }
        return diff, due

    def _refund_deadline(self, request_id: str, now: dt.datetime) -> list[DiffEntry]:
        row = self._one("SELECT * FROM refund_request WHERE id = ?", (request_id,))
        if row is None or row["status"] != RefundRequestStatus.OPEN:
            return []
        merchant = self._merchant(str(row["merchant_id"]))
        diff = self._set("refund_request", row, status=RefundRequestStatus.EXPIRED.value)
        diff += self._message(
            now,
            direction="in",
            sender=SYSTEM_SENDER,
            merchant_id=str(merchant["id"]),
            order_id=str(row["order_id"]),
            subject=f"Refund request {row['id']} expired",
            body=(
                f"{merchant['name']} did not answer refund request {row['id']} by "
                f"{row['respond_by']}. You can raise a dispute on the payment."
            ),
        )
        return diff

    def record_refund(self, outcome: RefundOutcome, now: dt.datetime) -> Result:
        """What the rails did with a refund this service asked for."""
        with self._transaction():
            self._config()
            table = "refund_request" if outcome.kind == "refund_request" else "dispute"
            row = self._one(f"SELECT * FROM {table} WHERE id = ?", (outcome.id,))
            if row is None:
                code = (
                    Rejection.REFUND_REQUEST_NOT_FOUND
                    if table == "refund_request"
                    else Rejection.DISPUTE_NOT_FOUND
                )
                raise ToolRejected(code, f"no {table.replace('_', ' ')} {outcome.id!r}")
            diff = self._set(table, row, refund_id=outcome.refund_id)
            if outcome.status == "failed" and table == "refund_request":
                diff += self._set(table, row, status=RefundRequestStatus.REFUND_FAILED.value)
            merchant = self._merchant(str(row["merchant_id"]))
            what = "Refund" if table == "refund_request" else "Chargeback"
            if outcome.status == "success":
                body = (
                    f"{money(outcome.amount_paise)} was refunded to your account for "
                    f"payment {row['payment_id']} (refund {outcome.refund_id})."
                )
            else:
                body = f"The refund of {money(outcome.amount_paise)} could not be paid."
            diff += self._message(
                now,
                direction="in",
                sender=str(merchant["name"]) if table == "refund_request" else SYSTEM_SENDER,
                merchant_id=str(merchant["id"]),
                order_id=row["order_id"],
                subject=f"{what} for {row['id']}: {outcome.status}",
                body=body,
            )
        return Result(body={table: dict(row)}, state_diff=diff)

    # ---------------------------------------------------------------- disputes
    def create_dispute(self, request: DisputeCreate, now: dt.datetime) -> Result:
        """Raise a dispute on a payment. One per payment; decided at the response deadline."""
        if request.amount_paise <= 0:
            msg = "amount_paise must be a positive whole number of paise"
            raise ToolRejected(Rejection.INVALID_AMOUNT, msg)
        if request.amount_paise > request.refundable_paise:
            msg = f"at most {request.refundable_paise} paise of this payment can still be refunded"
            raise ToolRejected(Rejection.NOT_REFUNDABLE, msg)
        with self._transaction():
            settings = self._config().settings
            self._merchant(request.merchant_id)
            closes = parse(request.paid_at) + dt.timedelta(seconds=settings.dispute_window_seconds)
            if now > closes:
                msg = f"disputes on payment {request.payment_id} closed at {iso(closes)}"
                raise ToolRejected(Rejection.DISPUTE_WINDOW_CLOSED, msg)
            existing = self._one(
                "SELECT id FROM dispute WHERE payment_id = ?", (request.payment_id,)
            )
            if existing is not None:
                msg = f"payment {request.payment_id} already has dispute {existing['id']}"
                raise ToolRejected(Rejection.DISPUTE_EXISTS, msg)
            respond_by = now + dt.timedelta(seconds=settings.dispute_response_seconds)
            row: Row = {
                "id": self._next_id("dsp", "dispute"),
                "payment_id": request.payment_id,
                "order_id": request.order_id,
                "merchant_id": request.merchant_id,
                "reason": request.reason.value,
                "amount_paise": request.amount_paise,
                "description": request.description,
                "status": DisputeStatus.OPEN.value,
                "decision": None,
                "awarded_paise": None,
                "refund_id": None,
                "created_at": iso(now),
                "respond_by": iso(respond_by),
                "decided_at": None,
            }
            self._insert("dispute", row)
            diff = [self._created("dispute", row)]
            event = ScheduledEvent(
                at=respond_by, name=EVENT_DECIDE, payload={"dispute_id": row["id"]}
            )
        return Result(body={"dispute": row}, state_diff=diff, scheduled=[event], created=True)

    def add_evidence(self, dispute_id: str, evidence: EvidenceCreate, now: dt.datetime) -> Result:
        """Attach evidence to an open dispute, before its deadline."""
        with self._transaction():
            self._config()
            dispute = self._dispute(dispute_id)
            if dispute["status"] != DisputeStatus.OPEN or now >= parse(str(dispute["respond_by"])):
                msg = f"dispute {dispute_id} no longer accepts evidence"
                raise ToolRejected(Rejection.EVIDENCE_WINDOW_CLOSED, msg)
            if evidence.kind is EvidenceKind.MESSAGE:
                found = self._one("SELECT id FROM message WHERE id = ?", (evidence.reference_id,))
            elif evidence.kind is EvidenceKind.REFUND_REQUEST:
                found = self._one(
                    "SELECT id FROM refund_request WHERE id = ?", (evidence.reference_id,)
                )
            else:
                found = {"id": evidence.reference_id}  # checked against its service by the tool
            if found is None:
                msg = (
                    f"there is no {evidence.kind.value.replace('_', ' ')} {evidence.reference_id!r}"
                )
                raise ToolRejected(Rejection.INVALID_EVIDENCE, msg)
            row: Row = {
                "id": self._next_id("evd", "evidence"),
                "dispute_id": dispute_id,
                "kind": evidence.kind.value,
                "reference_id": evidence.reference_id,
                "note": evidence.note,
                "created_at": iso(now),
            }
            self._insert("evidence", row)
            diff = [self._created("evidence", row)]
        return Result(body={"evidence": row}, state_diff=diff, created=True)

    def _dispute(self, dispute_id: str) -> Row:
        row = self._one("SELECT * FROM dispute WHERE id = ?", (dispute_id,))
        if row is None:
            msg = f"no dispute {dispute_id!r}"
            raise ToolRejected(Rejection.DISPUTE_NOT_FOUND, msg)
        return row

    def _decide(
        self, dispute_id: str, facts: dict[str, Any], now: dt.datetime
    ) -> tuple[list[DiffEntry], Row | None]:
        """Decide a dispute at its deadline by fixed rules over evidence and facts."""
        dispute = self._dispute(dispute_id)
        if dispute["status"] != DisputeStatus.OPEN:
            return [], None
        merchant = self._merchant(str(dispute["merchant_id"]))
        problems = self._dispute_problems(dispute, facts)
        refundable = int(facts.get("refundable_paise", 0))
        awarded = min(int(dispute["amount_paise"]), refundable)
        if not problems and awarded <= 0:
            problems = ["the payment has already been refunded in full"]
        if problems:
            status, decision, awarded = DisputeStatus.LOST, "; ".join(problems), 0
        elif not self._behaviour(merchant).contests_disputes:
            status, decision = DisputeStatus.WON, "deemed accepted: the merchant did not respond"
        else:
            status, decision = DisputeStatus.WON, "upheld on the evidence"
        diff = self._set(
            "dispute",
            dispute,
            status=status.value,
            decision=decision,
            awarded_paise=awarded,
            decided_at=iso(now),
        )
        body = (
            f"Dispute {dispute_id} on payment {dispute['payment_id']} was {status.value}: "
            f"{decision}."
        )
        if status is DisputeStatus.WON:
            body += f" {money(awarded)} will be returned to your account."
        diff += self._message(
            now,
            direction="in",
            sender=SYSTEM_SENDER,
            merchant_id=str(merchant["id"]),
            order_id=dispute["order_id"],
            subject=f"Dispute {dispute_id} {status.value}",
            body=body,
        )
        if status is DisputeStatus.LOST:
            return diff, None
        due = {
            "kind": "dispute",
            "id": dispute_id,
            "payment_id": dispute["payment_id"],
            "amount_paise": awarded,
            "idempotency_key": f"chargeback-{dispute_id}",
            "reason": f"chargeback for dispute {dispute_id}",
        }
        return diff, due

    def _dispute_problems(self, dispute: Row, facts: dict[str, Any]) -> list[str]:
        """Every reason a dispute fails, or none.

        A merchant that does not contest is deemed to accept (NPCI's rule for an
        unanswered P2M dispute), so only the facts are checked for it.
        """
        reason = DisputeReason(dispute["reason"])
        problems: list[str] = []
        if reason is DisputeReason.WRONG_ITEM and facts.get("order_status") != "wrong_item":
            problems.append("the order was not delivered with the wrong item")
        if reason is DisputeReason.NOT_RECEIVED and not facts.get("not_received"):
            problems.append("the order was delivered, or is not late yet")
        duplicate_of = facts.get("duplicate_of")
        if reason is DisputeReason.DUPLICATE_CHARGE and not duplicate_of:
            problems.append("the payment is not a duplicate of another payment")
        merchant = self._merchant(str(dispute["merchant_id"]))
        if problems or not self._behaviour(merchant).contests_disputes:
            return problems
        evidence = self.store.query_all(
            "SELECT kind, reference_id FROM evidence WHERE dispute_id = ?", (dispute["id"],)
        )
        pointers = {(row["kind"], row["reference_id"]) for row in evidence}
        if reason is DisputeReason.DUPLICATE_CHARGE:
            if ("payment", duplicate_of) not in pointers:
                problems.append("no evidence pointing to the original payment")
        else:
            if ("order", dispute["order_id"]) not in pointers:
                problems.append("no evidence pointing to the order")
            settings = self._config().settings
            contacted = self._scalar(
                "SELECT COUNT(*) FROM refund_request WHERE order_id = ? AND created_at < ?",
                (dispute["order_id"], dispute["created_at"]),
            )
            if settings.require_merchant_contact and not contacted:
                problems.append("the merchant was not asked for a refund first")
        return problems

    # ------------------------------------------------------------------ inbox
    def send_message(self, request: MessageCreate, now: dt.datetime) -> Result:
        """Write to a merchant. It replies later, in its own style."""
        with self._transaction():
            self._config()
            merchant = self._merchant(request.merchant_id)
            diff = self._message(
                now,
                direction="out",
                sender="you",
                merchant_id=request.merchant_id,
                order_id=request.order_id,
                subject=f"Message to {merchant['name']}",
                body=request.body,
            )
            message_id = str(diff[0].after["id"])
            behaviour = self._behaviour(merchant)
            event = ScheduledEvent(
                at=now + dt.timedelta(seconds=behaviour.reply_after_seconds),
                name=EVENT_MERCHANT_REPLY,
                payload={"message_id": message_id},
            )
        return Result(
            body={"message_id": message_id}, state_diff=diff, scheduled=[event], created=True
        )

    def _merchant_reply(self, message_id: str, now: dt.datetime) -> list[DiffEntry]:
        sent = self._one("SELECT * FROM message WHERE id = ?", (message_id,))
        if sent is None:
            msg = f"no message {message_id!r}"
            raise ToolRejected(Rejection.MESSAGE_NOT_FOUND, msg)
        merchant = self._merchant(str(sent["merchant_id"]))
        behaviour = self._behaviour(merchant)
        about = f" about order {sent['order_id']}" if sent["order_id"] else ""
        if behaviour.reply_style == "clear":
            hours = max(1, behaviour.refund_reply_after_seconds // 3_600)
            body = (
                f"Hello, thank you for your message{about}. If something is wrong with an "
                "order, please raise a refund request on it and we will answer within "
                f"{hours} hours. - {merchant['name']}"
            )
        else:
            body = (
                "Thank you for contacting us. Your concern has been noted and will be looked "
                f"into. - {merchant['name']}"
            )
        return self._message(
            now,
            direction="in",
            sender=str(merchant["name"]),
            merchant_id=str(merchant["id"]),
            order_id=sent["order_id"],
            subject=f"Re: {sent['subject']}",
            body=body,
            reply_to=message_id,
        )

    def add_notices(self, batch: NoticeBatch, now: dt.datetime) -> Result:
        """Deliver what other services want the user to know."""
        with self._transaction():
            self._config()
            diff: list[DiffEntry] = []
            for item in batch.notices:
                sender = (
                    SYSTEM_SENDER
                    if item.merchant_id is None
                    else str(self._merchant(item.merchant_id)["name"])
                )
                diff += self._message(
                    now,
                    direction="in",
                    sender=sender,
                    merchant_id=item.merchant_id,
                    order_id=item.order_id,
                    subject=item.subject,
                    body=item.body,
                )
        return Result(body={"delivered": len(batch.notices)}, state_diff=diff, created=bool(diff))

    def read_inbox(self, unread_only: bool, now: dt.datetime) -> Result:
        """Messages to and from the user, oldest first. Reading marks them read."""
        with self._transaction():
            self._config()
            sql = "SELECT * FROM message"
            if unread_only:
                sql += " WHERE read = 0"
            rows = self.store.query_all(sql + " ORDER BY rowid")
            diff: list[DiffEntry] = []
            for row in rows:
                if not row["read"]:
                    diff += self._set("message", dict(row), read=1)
        return Result(body={"messages": rows}, state_diff=diff)

    # ----------------------------------------------------------------- events
    def apply_event(self, name: str, payload: dict[str, Any], now: dt.datetime) -> Result:
        """Apply something that happens at a scheduled time."""
        due: Row | None = None
        with self._transaction():
            self._config()
            facts = dict(payload.get("facts") or {})
            if name == EVENT_REFUND_RESPONSE:
                diff, due = self._refund_response(str(payload.get("request_id")), facts, now)
            elif name == EVENT_REFUND_DEADLINE:
                diff = self._refund_deadline(str(payload.get("request_id")), now)
            elif name == EVENT_DECIDE:
                diff, due = self._decide(str(payload.get("dispute_id")), facts, now)
            elif name == EVENT_MERCHANT_REPLY:
                diff = self._merchant_reply(str(payload.get("message_id")), now)
            elif name == EVENT_DELIVER_MESSAGE:
                diff = self._deliver_seed(int(payload.get("index", -1)), now)
            else:
                msg = f"disputes does not handle event {name!r}"
                raise ToolRejected(Rejection.UNKNOWN_EVENT, msg)
        return Result(body={"event": name, "refund_due": due}, state_diff=diff)

    def subject(self, name: str, payload: dict[str, Any]) -> Row | None:
        """For an event that needs facts: the refund request or dispute it is about."""
        if name == EVENT_REFUND_RESPONSE:
            return self._one(
                "SELECT * FROM refund_request WHERE id = ?", (payload.get("request_id"),)
            )
        if name == EVENT_DECIDE:
            return self._one("SELECT * FROM dispute WHERE id = ?", (payload.get("dispute_id"),))
        return None

    # ---------------------------------------------------------------- reading
    def refund_requests(self, order_id: str) -> list[Row]:
        """Refund requests made on one order."""
        self._config()
        return self.store.query_all(
            "SELECT * FROM refund_request WHERE order_id = ? ORDER BY rowid", (order_id,)
        )

    def disputes_for(self, order_id: str | None, payment_id: str | None) -> list[Row]:
        """Disputes on an order or a payment."""
        self._config()
        return self.store.query_all(
            "SELECT * FROM dispute WHERE order_id = ? OR payment_id = ? ORDER BY rowid",
            (order_id, payment_id),
        )

    # ----------------------------------------------------------------- admin
    def admin(self, resource: str) -> Any:
        """The true state, for graders and tests only. Reads, never writes."""
        if resource not in ADMIN_RESOURCES:
            msg = f"unknown resource {resource!r}; disputes has {list(ADMIN_RESOURCES)}"
            raise ToolRejected(Rejection.UNKNOWN_RESOURCE, msg)
        if resource == "state":
            return {name: self.admin(name) for name in ADMIN_RESOURCES[1:]}
        tables = {
            "merchants": "merchant ORDER BY id",
            "refund_requests": "refund_request ORDER BY rowid",
            "disputes": "dispute ORDER BY rowid",
            "evidence": "evidence ORDER BY rowid",
            "messages": "message ORDER BY rowid",
        }
        return self.store.query_all(f"SELECT * FROM {tables[resource]}")


__all__ = [
    "EVENT_DECIDE",
    "EVENT_DELIVER_MESSAGE",
    "EVENT_MERCHANT_REPLY",
    "EVENT_REFUND_DEADLINE",
    "EVENT_REFUND_RESPONSE",
    "FACT_EVENTS",
    "SCHEMA",
    "Disputes",
]
