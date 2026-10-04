FROM node:22-alpine AS frontend
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci --registry=https://registry.npmmirror.com
COPY frontend/ ./
ARG VITE_BASE_PATH=/
RUN npm run build

FROM python:3.11-slim AS python-base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client ripgrep curl \
    && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.lock.txt /app/backend/requirements.lock.txt
RUN --mount=type=secret,id=build_proxy \
    if [ -f /run/secrets/build_proxy ]; then export HTTPS_PROXY="$(cat /run/secrets/build_proxy)"; fi; \
    pip install --no-cache-dir -r backend/requirements.lock.txt
COPY backend/ /app/backend/
COPY pyproject.toml pytest.ini /app/
ENV PYTHONPATH=/app/backend

FROM python-base AS verification
COPY scripts/ /app/scripts/
COPY examples/ /app/examples/
RUN ruff check backend scripts examples && pytest -q --junitxml=/app/test-results.xml \
    && touch /app/verified

FROM python-base AS runtime
COPY --from=verification /app/verified /app/verified
COPY --from=frontend /build/frontend/dist /app/frontend/dist
COPY workspace/README.txt /app/workspace/README.txt
COPY examples/ /app/examples/
COPY deploy/entrypoint.py /app/entrypoint.py
RUN useradd --uid 1000 --create-home agent && mkdir -p /data /workspace \
    && chown -R 1000:1000 /data /workspace
ENV MYCLAUDE_DATA_DIR=/data HOME=/home/agent
USER 1000:1000
EXPOSE 8000
CMD ["python", "/app/entrypoint.py"]
