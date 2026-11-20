# Market Signal Desk - web service image (pure Python standard library, no pip packages needed).
FROM python:3.12-slim

# TZ matters: the app reasons in IST (market hours, "today"), so the container clock must be IST.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Kolkata \
    PORT=10000 \
    REQUIRE_LOGIN=1 \
    LITE_MODE=1 \
    CACHE_DIR=/data/cache \
    DESK_ENV_FILE=/data/desk.env

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 app \
 && mkdir -p /data/cache \
 && chown -R app:app /data

WORKDIR /app
COPY *.py ./
COPY public ./public
COPY data_audit_report.txt walkforward_result.json scorecard_snapshot.json ./
# the strategy models: WITHOUT these the engine has nothing to score signals with and issues no calls at all
COPY strict_model.json conf_model.json ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 10000

# /healthz is exempt from the site login and reveals nothing.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','10000'), timeout=4)" || exit 1

# The entrypoint fixes ownership of the (possibly freshly mounted) /data disk, then drops to the unprivileged user.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "server.py"]
