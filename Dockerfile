# ========== Stage 1: Build frontend ==========
FROM node:20-alpine AS frontend-builder

WORKDIR /app/frontend
COPY frontend/package.json ./
RUN npm install

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

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 --retries 5 -i "$PIP_INDEX_URL" -r requirements.txt

# Copy Python source code
COPY . .

# Copy frontend build artifacts from stage 1
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

RUN mkdir -p /app/novels /app/results

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('WEB_PORT', '8765'), timeout=3).read()"

CMD ["python", "web_manager.py"]
