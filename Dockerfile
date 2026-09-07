FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SPEC_MANAGER_DATA_DIR=/var/data/spec-manager \
    KCS_DATA_DIR=/var/data/kcs

RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-writer fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY server/requirements.txt /app/server/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/server/requirements.txt

COPY server /app/server

EXPOSE 8000

CMD ["sh", "-c", "python -m uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
