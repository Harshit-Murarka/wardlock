"""Text that is safe to store in any database.

Postgres text columns cannot hold a NUL character (0x00); SQLite can. Any string that comes
from outside (an agent, a ticket, a URL) passes through `no_nul()` before it reaches a query,
so a stray NUL gives a normal answer instead of a database error.
"""

REPLACEMENT = "�"  # the standard "unknown character" sign, so the change stays visible


def no_nul(text: str) -> str:
    return text.replace("\x00", REPLACEMENT)
