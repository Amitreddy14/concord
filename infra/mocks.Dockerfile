# Single image serving all four mock systems of record; the entrypoint argument
# selects which one. One image keeps the compose stack small and guarantees the
# shared fault-injection middleware is identical across services.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[mocks]" || pip install --no-cache-dir fastapi uvicorn pydantic pyyaml

COPY contracts/ ./contracts/
COPY packages/shared/ ./packages/shared/
COPY apps/mocks/ ./apps/mocks/
COPY evals/golden/fixtures/ ./evals/golden/fixtures/

# Phase 1 implements the service entrypoint. Until then the image builds and
# the compose stack is wiring that can be brought up a service at a time.
ENTRYPOINT ["python", "-m", "apps.mocks.serve"]
CMD ["--help"]
