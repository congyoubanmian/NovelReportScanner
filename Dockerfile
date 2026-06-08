# ========== Stage 1: Build frontend ==========
FROM node:20-alpine AS frontend-builder

WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# ========== Stage 2: Python runtime ==========
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=5 \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8765

WORKDIR /app

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG APP_UID=1000
ARG APP_GID=1000
ARG APP_VERSION=dev
ARG APP_COMMIT=unknown
ARG APP_BUILD_DATE=

ENV APP_VERSION=${APP_VERSION} \
    APP_COMMIT=${APP_COMMIT} \
    APP_BUILD_DATE=${APP_BUILD_DATE}

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 --retries 5 -i "$PIP_INDEX_URL" -r requirements.txt

# Copy only runtime application files into the final image.
COPY *.py ./
COPY rules2.json ./
COPY profiles ./profiles
COPY docker-entrypoint.sh ./
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

RUN groupadd --gid "$APP_GID" appuser \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/novels /app/results \
    && chmod +x /app/docker-entrypoint.sh \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('WEB_PORT', '8765'), timeout=3).read()"

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["python", "web_manager.py"]
