"""FastAPI application entry point.

P0 only serves /health. Later phases mount /sessions (P3), the MCP server (P6),
/webhooks (P8) and /admin/api (P9) here.
"""

from fastapi import FastAPI

app = FastAPI(title="Wardlock", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check for local runs and Render.

    It deliberately touches neither the database nor PayPal: it answers "is the
    process up?", not "is every dependency healthy?".
    """
    return {"status": "ok"}
