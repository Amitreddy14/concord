"""Entrypoint for all four mock systems of record.

One image, one module, the service selected by argument. Keeps the compose
stack small and guarantees the cross-cutting middleware is byte-identical
across services rather than four copies that drift.
"""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn
from fastapi import FastAPI

from apps.mocks.app import create_app
from apps.mocks.faults import FaultConfig
from apps.mocks.services import crm, knowledge, payments, ticketing
from apps.mocks.state import Store

SERVICES = {
    "crm": (crm.router, "Merchant CRM", 8101),
    "payments": (payments.router, "Payments ledger", 8102),
    "ticketing": (ticketing.router, "Ticketing", 8103),
    "knowledge": (knowledge.router, "Knowledge base", 8104),
}


def build(service: str, fault_config: FaultConfig | None = None) -> FastAPI:
    if service not in SERVICES:
        raise SystemExit(f"unknown service {service!r}; choose from {', '.join(SERVICES)}")
    router, title, _ = SERVICES[service]
    return create_app(
        title=title,
        router=router,
        store=Store(),
        fault_config=fault_config or FaultConfig.from_env(dict(os.environ)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one mock system of record")
    parser.add_argument("service", choices=sorted(SERVICES))
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args(argv)

    port = args.port or SERVICES[args.service][2]
    uvicorn.run(build(args.service), host=args.host, port=port, log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
