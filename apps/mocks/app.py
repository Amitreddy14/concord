"""Shared application factory for the four mock systems of record.

One factory rather than four means the cross-cutting behaviour — problem
responses, correlation ids, fault injection, idempotency — is provably
identical across services, which matters because the tool layer is written
once against all four.

The middleware order is deliberate. Faults are injected *before* the handler
runs, so an injected 500 costs nothing and, more importantly, an injected
timeout happens after the effect has been applied rather than instead of it.
That asymmetry is the whole of S09: the refund exists upstream, and the client
never learns it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator
from functools import cached_property
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Match, Route

from apps.mocks.faults import FaultConfig, FaultInjector, ScriptedFault
from apps.mocks.idempotency import IdempotencyStore
from apps.mocks.state import Store
from packages.shared.problem import (
    PROBLEM_CONTENT_TYPE,
    RATE_LIMITED,
    UPSTREAM_FAILURE,
    UPSTREAM_TIMEOUT,
    VALIDATION_FAILED,
    Problem,
    ProblemError,
)

CORRELATION_HEADER = "X-Correlation-Id"


def problem_response(problem: Problem, extra_headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=problem.status,
        content=problem.to_payload(),
        media_type=PROBLEM_CONTENT_TYPE,
        headers=extra_headers or {},
    )


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Propagate the caller's correlation id, or mint one.

    Every run's steps carry the same id, which is what makes a trace
    reconstructable across four services after the fact.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        correlation_id = request.headers.get(CORRELATION_HEADER) or str(uuid.uuid4())
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers[CORRELATION_HEADER] = correlation_id
        return response


class FaultMiddleware(BaseHTTPMiddleware):
    """Apply the configured fault profile before the handler runs.

    ``accept_then_client_timeout`` is the one that is not like the others: the
    handler *is* executed and its effect *is* applied, and only then does the
    client receive a timeout. A naive retry therefore performs the effect
    twice, which is precisely the defect idempotency keys exist to prevent and
    precisely what S09 asserts against.
    """

    def __init__(self, app: FastAPI, injector: FaultInjector, owner: FastAPI) -> None:
        super().__init__(app)
        self.injector = injector
        # The FastAPI instance whose routes to match against. `app` here is
        # the next ASGI layer, not necessarily the application object.
        self._owner = owner

    def _route_template(self, request: Request) -> str:
        """The route's path template, e.g. ``/captures/{capture_id}``.

        Resolved by matching the request against the app's routes rather than
        read from ``request.scope["route"]``. BaseHTTPMiddleware runs before
        routing, so that key is absent here, and falling back to the literal
        path silently broke two things: a scripted fault on a parameterised
        endpoint never fired, and profile-driven faults counted attempts per
        id rather than per endpoint — so the sequence depended on which
        records a run happened to touch.
        """
        scope = request.scope
        for route in self._leaf_routes:
            match, _ = route.matches(scope)
            if match is not Match.NONE:
                path: str = route.path
                return path
        return request.url.path

    @cached_property
    def _leaf_routes(self) -> list[Route]:
        """Every concrete route, flattened out of the nested routers."""

        def walk(router: Any) -> Iterator[Route]:
            for route in getattr(router, "routes", []):
                if isinstance(route, Route):
                    yield route
                    continue
                nested = getattr(route, "original_router", None) or route
                if nested is not route or hasattr(nested, "routes"):
                    yield from walk(nested)

        return list(walk(self._owner.router))

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path == "/health" or request.url.path.startswith("/_test"):
            return await call_next(request)

        # Resolve the route template first so the fault key is stable across
        # different ids hitting the same endpoint.
        decision = self.injector.decide(request.method, self._route_template(request))

        match decision.behaviour:
            case "server_error":
                return problem_response(
                    Problem(UPSTREAM_FAILURE, "injected upstream failure", extra={"injected": True})
                )
            case "rate_limited":
                return problem_response(
                    Problem(RATE_LIMITED, "injected rate limit", extra={"injected": True}),
                    {"Retry-After": str(decision.retry_after_s or 1)},
                )
            case "connection_reset":
                # A reset has no response in reality; 504 is the closest thing
                # expressible over a test client, and the tool layer treats
                # both as retryable transport failures.
                return problem_response(
                    Problem(UPSTREAM_TIMEOUT, "injected connection reset", extra={"injected": True})
                )
            case "latency":
                await asyncio.sleep(decision.delay_ms / 1000)
                return await call_next(request)
            case "accept_then_client_timeout":
                # Apply the effect, discard the response, report a timeout.
                await call_next(request)
                return problem_response(
                    Problem(
                        UPSTREAM_TIMEOUT,
                        "injected client timeout after the request was accepted upstream",
                        extra={"injected": True, "effect_applied": True},
                    )
                )
            case "replay_original":
                # No injection: the handler runs and the idempotency store
                # returns the original response because the key matches. Named
                # explicitly so a fixture that asks for it is not silently
                # relying on the default branch.
                return await call_next(request)
            case _:
                return await call_next(request)


def create_app(
    *,
    title: str,
    router: APIRouter,
    store: Store,
    fault_config: FaultConfig | None = None,
) -> FastAPI:
    config = fault_config or FaultConfig()
    injector = FaultInjector(config.profile, config.seed, config.script)

    app = FastAPI(title=title, version="0.1.0")
    app.state.store = store
    app.state.injector = injector
    app.state.idempotency = IdempotencyStore()
    # Per-tenant retrieval indexes, built lazily and kept for the process.
    app.state.indexes = {}

    @app.exception_handler(ProblemError)
    async def _problem_handler(_request: Request, exc: ProblemError) -> JSONResponse:
        return problem_response(exc.problem)

    @app.exception_handler(ValueError)
    async def _value_handler(_request: Request, exc: ValueError) -> JSONResponse:
        # A ValueError escaping a handler is a malformed argument, not a bug in
        # the service; Money raises these for a float amount or a bad currency.
        return problem_response(Problem(VALIDATION_FAILED, str(exc)))

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # Test-only control plane. Mounted because the eval harness has to reset
    # state and pin a fault script between scenario runs, and doing that over
    # HTTP keeps the harness free of any in-process coupling to the services.
    control = APIRouter(prefix="/_test", tags=["test-control"])

    @control.post("/reset")
    async def reset(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        app.state.store.reset()
        app.state.idempotency.clear()
        app.state.injector.reset()
        if payload:
            app.state.store.load(payload.get("seed", {}))
            if "fault_profile" in payload:
                app.state.injector.profile = type(app.state.injector.profile)(
                    payload["fault_profile"]
                )
            if "fault_seed" in payload:
                app.state.injector.seed = int(payload["fault_seed"])
            if "fault_script" in payload:
                app.state.injector.load_script(
                    [ScriptedFault(**entry) for entry in payload["fault_script"]]
                )
        return {"status": "reset", "profile": str(app.state.injector.profile)}

    app.include_router(control)
    app.include_router(router)

    # Starlette's add_middleware signature does not accommodate a
    # BaseHTTPMiddleware subclass with extra constructor arguments; the
    # call is correct at runtime.
    app.add_middleware(FaultMiddleware, injector=injector, owner=app)  # type: ignore[arg-type]
    app.add_middleware(CorrelationMiddleware)
    return app
