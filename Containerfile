FROM python:3.12-slim

LABEL org.opencontainers.image.title="Lucid" \
      org.opencontainers.image.description="Web UI for streamrip, built to hand off downloads to Navidrome"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LUCID_MUSIC_DIR=/music \
    LUCID_CONFIG_DIR=/config \
    LUCID_FRONTEND_DIR=/app/frontend

WORKDIR /app/backend

# ffmpeg is required by streamrip for format conversion / some codecs.
# build-essential covers any dependency that ships only as a source distribution;
# it's removed again once pip install is done to keep the final image small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y build-essential \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY backend/app ./app
COPY frontend /app/frontend

RUN mkdir -p /music /config

VOLUME ["/music", "/config"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/api/health', timeout=3)" || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
