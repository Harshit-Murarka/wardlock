"""LLM check that a refund reason is supported by the ticket text (SPEC §11).

Built in P5. It can only escalate ALLOW to HOLD; it can never approve (invariant 6).
"""
