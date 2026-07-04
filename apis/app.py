"""Assembles the PoC data APIs into one FastAPI app.

Served in-process by the MCP container on 127.0.0.1 so the MCP tools can call it
over localhost — the MCP layer never reaches the upstream sources directly.
"""
from fastapi import FastAPI, Request

from .entities import router as entities_router
from .rates import router as rates_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="LCH PoC Data APIs",
        version="0.1.0",
        description=(
            "Governed API facade over public, LCH-relevant data: reference risk-free "
            "rates (SOFR/ESTR/SONIA) and legal-entity (GLEIF LEI) data. Consumed by the "
            "LCH MCP server."
        ),
    )

    @app.get("/healthz", tags=["ops"], summary="Liveness probe")
    def healthz():
        return {"status": "ok"}

    @app.middleware("http")
    async def correlation_id(request: Request, call_next):
        """Echo a caller-supplied correlation id so a request can be traced MCP->API."""
        cid = request.headers.get("x-correlation-id")
        response = await call_next(request)
        if cid:
            response.headers["x-correlation-id"] = cid
        return response

    app.include_router(rates_router)
    app.include_router(entities_router)
    return app


app = create_app()
