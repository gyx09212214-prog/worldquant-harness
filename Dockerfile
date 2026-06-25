# ── Stage 1: Build frontend ──────────────────────────────────────
FROM node:20-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts
COPY frontend/ ./
RUN npm run build

# ── Stage 2: Python runtime ─────────────────────────────────────
FROM python:3.12-slim AS runtime
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[postgresql]" && \
    rm -rf /root/.cache/pip

COPY worldquant_harness/ ./worldquant_harness/
COPY scripts/ ./scripts/
COPY --from=frontend /app/frontend/dist ./frontend/dist

RUN mkdir -p data reports logs

EXPOSE 8003

ENV AUTH_DISABLED=true
ENV WORLDQUANT_HARNESS_TASK_BACKEND=process
ENV WORLDQUANT_HARNESS_WORKER_PROCESSES=2

CMD ["python", "-m", "worldquant_harness", "--transport", "http"]
