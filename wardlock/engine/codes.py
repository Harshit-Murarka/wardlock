"""Reason codes the decision engine can raise (SPEC §5), and what each one means in plain words."""

import enum


class Code(enum.StrEnum):
    # The order here is the SPEC §5 table order; decisions list their codes in it.
    SESSION_INVALID = "SESSION_INVALID"
    UNKNOWN_HANDLE = "UNKNOWN_HANDLE"
    HANDLE_OTHER_SESSION = "HANDLE_OTHER_SESSION"
    HANDLE_ALREADY_USED = "HANDLE_ALREADY_USED"
    INVALID_COMBINATION = "INVALID_COMBINATION"
    NOT_OWNER = "NOT_OWNER"
    MISSING_AMOUNT = "MISSING_AMOUNT"  # compat only
    INVALID_AMOUNT = "INVALID_AMOUNT"  # compat only
    EXCEEDS_REFUNDABLE = "EXCEEDS_REFUNDABLE"
    REFUND_WINDOW_EXPIRED = "REFUND_WINDOW_EXPIRED"
    REASON_KIND_MISMATCH = "REASON_KIND_MISMATCH"
    OVER_AUTO_LIMIT = "OVER_AUTO_LIMIT"
    SESSION_VELOCITY = "SESSION_VELOCITY"
    AMOUNT_NOT_DERIVABLE = "AMOUNT_NOT_DERIVABLE"  # compat only
    REASON_NOT_SUPPORTED_BY_TICKET = "REASON_NOT_SUPPORTED_BY_TICKET"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"  # eval config B: every refund needs a person
    # Executor outcomes (SPEC §6), never produced by decide(): PayPal itself said no.
    PAYPAL_REJECTED = "PAYPAL_REJECTED"  # a 4xx such as REFUND_AMOUNT_EXCEEDED
    PAYPAL_REFUND_FAILED = "PAYPAL_REFUND_FAILED"  # the refund came back FAILED or CANCELLED


# BLOCK: the request is wrong and must never run. Nobody can approve it.
BLOCK_CODES = frozenset({
    Code.SESSION_INVALID,
    Code.UNKNOWN_HANDLE,
    Code.HANDLE_OTHER_SESSION,
    Code.HANDLE_ALREADY_USED,
    Code.INVALID_COMBINATION,
    Code.NOT_OWNER,
    Code.MISSING_AMOUNT,
    Code.INVALID_AMOUNT,
    Code.EXCEEDS_REFUNDABLE,
    Code.REFUND_WINDOW_EXPIRED,
    Code.PAYPAL_REJECTED,
    Code.PAYPAL_REFUND_FAILED,
})

# HOLD: the request may be fine, but a person must approve it first.
HOLD_CODES = frozenset(set(Code) - BLOCK_CODES)

# Plain words for the agent's reply and the console. They never reveal another
# customer's data, e.g. whether an unknown capture exists.
EXPLANATIONS: dict[Code, str] = {
    Code.SESSION_INVALID: "This support session has ended.",
    Code.UNKNOWN_HANDLE: "A refund option does not exist. Use the options from get_session_context.",
    Code.HANDLE_OTHER_SESSION: "A refund option belongs to a different conversation.",
    Code.HANDLE_ALREADY_USED: "A refund option was already used or has expired. Fetch fresh options.",
    Code.INVALID_COMBINATION: (
        "These refund options cannot be combined: one order per request, a full refund on its "
        "own, at most one goodwill gesture, and no option twice."
    ),
    Code.NOT_OWNER: "That payment does not belong to this customer.",
    Code.MISSING_AMOUNT: "No amount was given.",
    Code.INVALID_AMOUNT: "The amount is not a valid positive amount in the payment's currency.",
    Code.EXCEEDS_REFUNDABLE: "The refund is more than what is left to refund on this order.",
    Code.REFUND_WINDOW_EXPIRED: "The payment is more than 180 days old and can no longer be refunded.",
    Code.REASON_KIND_MISMATCH: "This kind of refund does not fit the stated reason. A person will review it.",
    Code.OVER_AUTO_LIMIT: (
        "Together with this conversation's earlier automatic refunds, the amount is above the "
        "automatic approval limit. A person will review it."
    ),
    Code.SESSION_VELOCITY: "This conversation reached its limit of automatic refunds. A person will review it.",
    Code.AMOUNT_NOT_DERIVABLE: "The amount does not match any refund option for this order. A person will review it.",
    Code.REASON_NOT_SUPPORTED_BY_TICKET: "The ticket does not clearly support this reason. A person will review it.",
    Code.DEPENDENCY_UNAVAILABLE: "A system Wardlock relies on did not answer. A person will review it.",
    Code.MANUAL_REVIEW_REQUIRED: "Every refund on this shop is reviewed by a person before it is made.",
    Code.PAYPAL_REJECTED: "PayPal declined this refund. No money moved.",
    Code.PAYPAL_REFUND_FAILED: "PayPal could not complete this refund. No money moved.",
}
