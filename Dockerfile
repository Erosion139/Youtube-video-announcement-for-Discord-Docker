FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=25599 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Runs as UID/GID 568, the "apps" user TrueNAS SCALE uses for app datasets.
RUN groupadd --gid 568 apps \
 && useradd --uid 568 --gid 568 --no-create-home --shell /usr/sbin/nologin apps \
 && mkdir -p /data \
 && chown 568:568 /data

USER 568:568
VOLUME ["/data"]
EXPOSE 25599

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "/app/app/healthcheck.py"]

CMD ["python", "-m", "app.main"]
