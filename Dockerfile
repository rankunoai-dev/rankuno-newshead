# The RankUno Brief: one always-on container that runs the schedule and the admin page.
# Mount a persistent volume at /data (Railway: service → Settings → Volumes). The database there
# records what was sent, which is what prevents anyone receiving an issue twice after a redeploy.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY rankuno_brief ./rankuno_brief
COPY config ./config
COPY templates ./templates
COPY assets ./assets

# Railway mounts volumes as root, so the process runs as root to be able to write to /data.
CMD ["python", "-m", "rankuno_brief", "serve"]
