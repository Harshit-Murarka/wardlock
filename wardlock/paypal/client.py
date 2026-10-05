"""The ONLY PayPal HTTP client (SPEC §6–7, invariant 1).

Built in P1. Refuses any base URL other than the sandbox unless ALLOW_LIVE=1 (invariant 8),
and always sends an explicit refund amount (invariant 4).
"""
