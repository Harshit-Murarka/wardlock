"""Run ALLOWed and merchant-approved refunds (SPEC §6).

Built in P5. The decision row and its idempotency key are saved before PayPal is
called (invariant 4), and any unknown outcome becomes HOLD (invariant 5).
"""
