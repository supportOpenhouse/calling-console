"""Default tag taxonomy (ported from the old lib/taxonomy.ts)."""

DEFAULT_TAXONOMY = {
    "good": ["qualified", "want_to_sell", "hot_buyer", "booked", "interested"],
    "soft": ["qualified_no_price", "want_to_sell_later", "sell_later", "yes_unverified", "callback_requested"],
    "terminal": ["not_selling", "already_sold", "not_eligible", "buyer_lead", "no_buyer", "not_interested"],
    "dnc": ["dnc", "do_not_call", "angry", "wrong_person"],
    "retry": ["not_picked", "unclear"],
}
