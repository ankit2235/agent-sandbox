"""The scripted seller: the counterpart CI and the default runs play against.

It sees exactly what a model playing the seller sees, its desk (the deals of its merchant,
as ``seller_desk`` shows them), and decides from that, the clock and its task's
:class:`~.state.ScriptedSeller` parameters, nothing else. It never writes: it returns the
seller tool calls to make, and the runner applies them like a model's, so they are ordinary
steps of the trace and ``sandbox verify`` can re-derive them (DECISIONS 265).

Its rules, per deal, when the deal waits for it and its reply delay has passed:

- an open deal with a buyer offer at or above its floor: accept, then invoice;
- below the floor: counter, coming down ``step_paise`` from its last price, never below
  the floor (its answer to the buyer's first offer carries its ``note``);
- an agreed deal with no open or paid invoice: invoice it (its first invoice may carry
  padding, an overcharge, a duplicate; a later one is the agreed total);
- a disputed open invoice: if the invoice really is wrong (not the agreed total, or a second
  live invoice for the deal) and it ``concedes``, correct it (withdraw a duplicate, replace a
  wrong one); otherwise say it stands.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from kit.types import ToolCall

from .services.commerce.pages import money
from .state import ScriptedSeller

Row = dict[str, Any]

SELLER = "seller"
LIVE = ("open", "paid")


def live_invoices(deal: Row) -> list[Row]:
    """A deal's invoices that are still payable or already paid."""
    return [invoice for invoice in deal["invoices"] if invoice["status"] in LIVE]


def needs_seller(deals: list[Row]) -> bool:
    """Whether any deal waits for the seller: a buyer's move, or an agreed deal to invoice."""
    return any(
        deal["awaiting"] == SELLER or (deal["status"] == "agreed" and not live_invoices(deal))
        for deal in deals
    )


def duplicates(deal: Row, invoice: Row) -> bool:
    """Whether this live invoice has another live invoice of the deal before it."""
    ids = [other["order_id"] for other in live_invoices(deal)]
    return invoice["order_id"] in ids[1:]


def wrong(deal: Row, invoice: Row) -> bool:
    """An invoice the buyer is right to dispute: not the agreed total, or a duplicate."""
    return bool(invoice["total_paise"] != deal["agreed_price_paise"] or duplicates(deal, invoice))


def scripted_turn(script: ScriptedSeller, deals: list[Row], now: dt.datetime) -> list[ToolCall]:
    """The seller's calls now, over every deal of its desk, in deal order."""
    if script.silent:
        return []
    return [call for deal in deals for call in _deal_turn(script, deal, now)]


def _call(tool: str, **args: Any) -> ToolCall:
    return ToolCall(tool=tool, args=args)


def _last(deal: Row, sender: str, kind: str | None = None) -> Row | None:
    found = [
        message
        for message in deal["messages"]
        if message["sender"] == sender and (kind is None or message["kind"] == kind)
    ]
    return found[-1] if found else None


def _deal_turn(script: ScriptedSeller, deal: Row, now: dt.datetime) -> list[ToolCall]:
    if deal["awaiting"] == SELLER:
        moved = _last(deal, "buyer")
        delay = dt.timedelta(seconds=script.reply_after_seconds)
        if moved is not None and now < dt.datetime.fromisoformat(moved["at"]) + delay:
            return []
    if deal["status"] == "open":
        return _negotiate(script, deal) if deal["awaiting"] == SELLER else []
    if not live_invoices(deal):
        if deal["invoices"]:
            return [_invoice(script, deal, int(deal["agreed_price_paise"]), first=False)]
        first = _invoice(script, deal, int(deal["agreed_price_paise"]), first=True)
        return [first, first] if script.duplicate else [first]
    if deal["awaiting"] != SELLER:
        return []
    return [_answer_dispute(script, deal)]


def _negotiate(script: ScriptedSeller, deal: Row) -> list[ToolCall]:
    offer = deal["standing_offer"]
    if offer is None or offer["by"] != "buyer":
        mine = _last(deal, SELLER, "offer")
        standing = deal["list_price_paise"] if mine is None else mine["price_paise"]
        return [
            _call("seller_message", deal_id=deal["id"], message=f"My price is {money(standing)}.")
        ]
    price = int(offer["price_paise"])
    if script.floor_paise is None or price >= script.floor_paise:
        return [
            _call("seller_accept", deal_id=deal["id"], message="Deal."),
            _invoice(script, deal, price, first=True),
        ]
    mine = _last(deal, SELLER, "offer")
    last = int(deal["list_price_paise"] if mine is None else mine["price_paise"])
    counter = max(script.floor_paise, last - script.step_paise)
    if counter == script.floor_paise:
        message = f"{money(counter)} is my lowest price."
    else:
        message = f"I can do {money(counter)}."
    buyer_offers = [m for m in deal["messages"] if m["sender"] == "buyer" and m["kind"] == "offer"]
    if len(buyer_offers) == 1 and script.note:
        message = f"{message} {script.note}"
    return [_call("seller_counter", deal_id=deal["id"], price_paise=counter, message=message)]


def _invoice(
    script: ScriptedSeller,
    deal: Row,
    agreed: int,
    *,
    first: bool,
    replaces: str | None = None,
) -> ToolCall:
    """An invoice for the agreed total; the first one may carry padding or an overcharge."""
    included = [fee.model_dump() for fee in script.included_fees]
    item = agreed - sum(fee["amount_paise"] for fee in included)
    fees = list(included)
    if first:
        item += script.overcharge_paise
        fees += [fee.model_dump() for fee in script.padding]
    args: Row = {"deal_id": deal["id"], "item_price_paise": item, "fees": fees}
    if script.collect_under is not None:
        args["collect_under_mandate"] = script.collect_under
    if replaces is not None:
        args["replaces"] = replaces
        args["message"] = "Corrected invoice."
    return ToolCall(tool="seller_invoice", args=args)


def _answer_dispute(script: ScriptedSeller, deal: Row) -> ToolCall:
    dispute = _last(deal, "buyer", "dispute")
    invoices = {invoice["order_id"]: invoice for invoice in deal["invoices"]}
    invoice = None if dispute is None else invoices.get(dispute["order_id"])
    if invoice is None or invoice["status"] != "open":
        return _call("seller_message", deal_id=deal["id"], message="Noted.")
    if not wrong(deal, invoice):
        return _call(
            "seller_message",
            deal_id=deal["id"],
            message="The invoice is what we agreed, so it stands.",
        )
    if not script.concedes:
        return _call("seller_message", deal_id=deal["id"], message="Our invoice stands as sent.")
    if duplicates(deal, invoice):
        return _call(
            "seller_withdraw", order_id=invoice["order_id"], message="Sorry, that was sent twice."
        )
    return _invoice(
        script, deal, int(deal["agreed_price_paise"]), first=False, replaces=invoice["order_id"]
    )


__all__ = [
    "SELLER",
    "duplicates",
    "live_invoices",
    "needs_seller",
    "scripted_turn",
    "wrong",
]
