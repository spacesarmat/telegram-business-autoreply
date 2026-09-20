FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

LABEL org.opencontainers.image.source="https://github.com/spacesarmat/TGAUTOREPLY" \
      org.opencontainers.image.title="Telegram Business AutoReply" \
      org.opencontainers.image.description="Telegram Business auto-reply bot for ZimaOS/CasaOS"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data

CMD ["python", "-m", "app.main"]
