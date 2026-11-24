# CareThread Cloud Run container image (multi-stage, non-root)
# Build remotely via Cloud Build or `gcloud builds submit` — no local Docker required.

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install .

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    PORT=8080

WORKDIR /app

# Create non-root runtime user
RUN groupadd --gid 10001 carethread \
    && useradd --uid 10001 --gid carethread --shell /usr/sbin/nologin --no-create-home carethread

COPY --from=builder /opt/venv /opt/venv
COPY --chown=carethread:carethread src/ ./src/

USER carethread

EXPOSE 8080

# Override CMD per Cloud Run service (API gateway, ADK runtime, or Cloud Tasks worker)
# once an HTTP/ASGI entrypoint (e.g., uvicorn carethread.api:app) is added.
CMD ["python", "-m", "carethread"]
