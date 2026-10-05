"""Open and validate agent sessions (SPEC §10).

Built in P3. Only a SHA-256 hash of each session token is stored, so a database
leak does not hand out working tokens.
"""
