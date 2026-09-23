# HabitFlow in ONE image: Postgres 16 + llm + api + bot.
#
#   docker run -d --name habitflow --restart unless-stopped \
#     -v habitflow-data:/data --env-file .env jackdiva/habitflow:all-<tag>
#
# Leave DATABASE_URL out of .env to use the built-in database (kept in /data);
# set it to use an external Postgres instead (e.g. Render Postgres).
# Build from the repo root:  docker build -f allinone/Dockerfile -t jackdiva/habitflow:all-<tag> .

# Pinned Debian release, so the Postgres major version never changes under your data.
FROM python:3.12-slim-trixie

ARG PG_MAJOR=16
# Postgres from the official PGDG apt repo; no default cluster (we create ours in /data).
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    install -d /usr/share/postgresql-common/pgdg /etc/postgresql-common; \
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc; \
    . /etc/os-release; \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list; \
    echo "create_main_cluster = false" > /etc/postgresql-common/createcluster.conf; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-${PG_MAJOR}; \
    apt-get purge -y curl; apt-get autoremove -y; rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PATH=/usr/lib/postgresql/${PG_MAJOR}/bin:$PATH \
    LLM_BASE_URL=http://127.0.0.1:9000 \
    API_BASE_URL=http://127.0.0.1:8000

WORKDIR /app

# One environment for all three services; `pip check` fails the build on a conflict.
COPY api/requirements.txt /tmp/req/api.txt
COPY llm/requirements.txt /tmp/req/llm.txt
COPY bot/requirements.txt /tmp/req/bot.txt
RUN pip install --no-cache-dir -r /tmp/req/api.txt -r /tmp/req/llm.txt -r /tmp/req/bot.txt \
    && pip check && rm -rf /tmp/req

# Only runtime code: no tests, evals, docs or .env.
COPY api/app api/app
COPY api/migrations api/migrations
COPY llm/app llm/app
COPY llm/semantics.yaml llm/semantics.yaml
COPY bot/bot.py bot/bot.py
COPY allinone/run_all.py run_all.py
COPY --chmod=755 allinone/habitflow-backup /usr/local/bin/habitflow-backup

# The embedded database lives here; without a volume it's lost with the container.
VOLUME ["/data"]

HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/db')"

STOPSIGNAL SIGTERM
CMD ["python", "/app/run_all.py"]
