FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WORKGUARD_DATA_DIR=/app/data/artifacts

WORKDIR /app

RUN addgroup --system workguard && adduser --system --ingroup workguard workguard

COPY pyproject.toml README.md requirements.lock alembic.ini ./
COPY backend ./backend
COPY migrations ./migrations
COPY frontend ./frontend
COPY demo ./demo

RUN pip install --no-cache-dir -r requirements.lock \
    && pip install --no-cache-dir --no-deps . \
    && mkdir -p /app/data/artifacts \
    && chown -R workguard:workguard /app

USER workguard

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" || exit 1

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
