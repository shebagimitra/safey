# ============================================================
# COMPONENT: C11a - Dockerfile Packaging
# PURPOSE: Package application container in replay-only mode with non-root user security.
# INPUT: Local source code, requirements.txt, and processed data assets.
# OUTPUT: Container image exposing REST API on port 8000.
# CALLED BY: Container runtime (Docker, Podman)      CALLS: python:3.11-slim, api.main
# IF IT BREAKS, CHECK: Non-root user permissions, requirements installation, port mapping.
# ============================================================

FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -m -u 1001 appuser && chown -R appuser:appuser /app

USER appuser

ENV ALLOW_LIVE=false HOST=0.0.0.0 PORT=8000 DATA_DIR=data

EXPOSE 8000

CMD ["python", "-m", "api.main"]
