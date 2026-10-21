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
COPY packages/ ./packages/
COPY apps/ ./apps/
COPY evals/ ./evals/

# The service is chosen by argument: one image for all four, so the shared
# middleware is identical rather than four copies that drift.
ENTRYPOINT ["python", "-m", "apps.mocks.serve"]
CMD ["--help"]
