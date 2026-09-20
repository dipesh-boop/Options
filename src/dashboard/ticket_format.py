"""The exact "COPY FIDELITY ORDER" text (Step 18) — what the dashboard's
COPY FIDELITY ORDER button puts on the clipboard. Deliberately a
separate formatter from `src.brokers.fidelity.render_ticket_text` (the
CLI-report template `/morning-scan` already uses): Step 18 specifies its
own exact wording and layout, and the two are independent presentations
of the same, single, already-validated `FidelityTradeTicket` — this
module computes nothing and adds no field that isn't already on the
ticket. Returns a plain string; clipboard access is the browser's own
`navigator.clipboard.writeText`, called by the frontend with the string
this function returns — never a side effect of this function itself.
"""
from __future__ import annotations

from src.brokers.fidelity import FidelityTradeTicket
from src.data.option_chain import OptionRight

_ACTION_LABELS = {
    "buy_to_open": "BUY TO OPEN",
    "sell_to_open": "SELL TO OPEN",
    "buy_to_close": "BUY TO CLOSE",
    "sell_to_close": "SELL TO CLOSE",
}


def render_dashboard_order_text(ticket: FidelityTradeTicket) -> str:
    lines = [
        "FIDELITY TRADER+ ORDER",
        "",
        ticket.ticker,
        "",
        ticket.strategy,
        "",
        "EXPIRATION:",
        ticket.expiration.strftime("%m/%d/%Y"),
        "",
    ]

    for leg in ticket.legs:
        put_call = "PUT" if leg.put_call == OptionRight.PUT else "CALL"
        lines += [
            f"{_ACTION_LABELS[leg.action.value]}:",
            f"{leg.contracts} × {ticket.ticker} {leg.strike:g} {put_call}",
            "",
        ]

    is_credit = ticket.estimated_credit_debit >= 0
    order_type = "NET CREDIT LIMIT" if is_credit else "NET DEBIT LIMIT"
    guard_label = "DO NOT ENTER BELOW:" if is_credit else "DO NOT ENTER ABOVE:"

    lines += [
        "ORDER TYPE:",
        order_type,
        "",
        "TARGET:",
        f"${abs(ticket.limit_price):.2f}",
        "",
        guard_label,
        f"${abs(ticket.minimum_acceptable_price):.2f}",
        "",
        "TIME IN FORCE:",
        ticket.time_in_force,
        "",
        "Quote:",
        f"Bid ${ticket.net_bid:.2f} / Ask ${ticket.net_ask:.2f} / Mid ${ticket.net_mid:.2f}",
        "",
        "Quote timestamp:",
        ticket.market_data_timestamp.strftime("%Y-%m-%d %H:%M:%S %Z"),
    ]
    return "\n".join(lines)
