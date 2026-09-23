"""
All-in-one entrypoint: HabitFlow as ONE container.

Processes, started in this order and stopped in reverse:
  postgres  only when DATABASE_URL is NOT set: an embedded Postgres 16 whose
            data lives in /data (mount a volume or a disk there!). It listens
            on a unix socket only, never on the network.
  llm       uvicorn on 127.0.0.1:9000
  api       uvicorn on 127.0.0.1:8000 (applies migrations at startup)
  bot       Telegram polling + reminder jobs

Each start waits for the previous service's health check, so nothing starts
before what it depends on. Any process that exits is restarted with
exponential backoff (1s .. 60s). SIGTERM/SIGINT stops everything cleanly
(Postgres gets a fast, checkpointed shutdown) and exits 0.

With DATABASE_URL set (e.g. Render Postgres) the embedded database is skipped.
"""

import logging
import os
import pwd
import signal
import subprocess
import sys
import time
import urllib.request

logging.basicConfig(format="%(asctime)s [supervisor] %(message)s", level=logging.INFO)
log = logging.getLogger("supervisor")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
PGDATA = os.path.join(DATA_DIR, "pgdata")
PG_BIN = os.environ.get("PG_BIN", "/usr/lib/postgresql/16/bin")
PG_SOCKET_DIR = "/run/postgresql"
PG_USER = os.environ.get("POSTGRES_USER") or "habitflow"  # database role, not OS user
PG_DB = os.environ.get("POSTGRES_DB") or "habitflow"

API_HOST = os.environ.get("API_HOST", "127.0.0.1")
HEALTH_TIMEOUT = float(os.environ.get("STARTUP_HEALTH_TIMEOUT", "120"))
MAX_BACKOFF = 60.0
STABLE_AFTER = 60.0  # seconds up before a crash counts as "new" again


# ------------------------------------------------------------------
# Health checks
# ------------------------------------------------------------------
def http_ok(url: str):
    def check() -> bool:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return r.status == 200
        except Exception:
            return False
    return check


def postgres_ready() -> bool:
    r = subprocess.run(
        [f"{PG_BIN}/pg_isready", "-q", "-h", PG_SOCKET_DIR, "-U", PG_USER, "-d", "postgres"],
        capture_output=True,
    )
    return r.returncode == 0


# ------------------------------------------------------------------
# Embedded Postgres
# ------------------------------------------------------------------
def _psql(sql: str) -> str:
    r = subprocess.run(
        [f"{PG_BIN}/psql", "-h", PG_SOCKET_DIR, "-U", PG_USER, "-d", "postgres",
         "-tAc", sql], capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def prepare_embedded_postgres(env: dict) -> list[str]:
    """Create/adopt the data dir; return the postgres command line."""
    pg = pwd.getpwnam("postgres")
    if not os.path.ismount(DATA_DIR):
        log.warning("!" * 70)
        log.warning("%s is not a mounted volume: the database will be LOST when this", DATA_DIR)
        log.warning("container is removed. Run with -v habitflow-data:%s (or a Render disk).",
                    DATA_DIR)
        log.warning("!" * 70)
    for path in (DATA_DIR, PGDATA, PG_SOCKET_DIR):
        os.makedirs(path, exist_ok=True)
    os.chmod(PGDATA, 0o700)
    # Adopt data created by another image (e.g. postgres:16.4's uid 999).
    for root, dirs, files in os.walk(PGDATA):
        for name in [root, *(os.path.join(root, n) for n in dirs + files)]:
            if os.lstat(name).st_uid != pg.pw_uid:
                os.lchown(name, pg.pw_uid, pg.pw_gid)
    os.chown(PG_SOCKET_DIR, pg.pw_uid, pg.pw_gid)

    if not os.path.exists(os.path.join(PGDATA, "PG_VERSION")):
        log.info("initialising a new database in %s", PGDATA)
        subprocess.run(
            [f"{PG_BIN}/initdb", "-D", PGDATA, "-U", PG_USER, "-E", "UTF8", "--no-locale",
             "--auth-local=trust", "--auth-host=reject"],
            user="postgres", group="postgres", check=True, stdout=subprocess.DEVNULL,
        )
    else:
        with open(os.path.join(PGDATA, "PG_VERSION")) as f:
            version = f.read().strip()
        if version != "16":
            sys.exit(f"{PGDATA} holds Postgres {version} data; this image runs Postgres 16.")
        log.info("using existing database in %s", PGDATA)

    env["DATABASE_URL"] = f"postgresql+psycopg://{PG_USER}@/{PG_DB}?host={PG_SOCKET_DIR}"
    return [f"{PG_BIN}/postgres", "-D", PGDATA,
            "-c", "listen_addresses=",                  # no TCP at all
            "-c", f"unix_socket_directories={PG_SOCKET_DIR}"]


def ensure_database():
    if _psql(f"SELECT 1 FROM pg_database WHERE datname = '{PG_DB}'") != "1":
        log.info("creating database %s", PG_DB)
        _psql(f'CREATE DATABASE "{PG_DB}" OWNER "{PG_USER}"')
    refresh_collation_if_needed(PG_DB)


def refresh_collation_if_needed(db: str):
    """Data made under another glibc may sort text differently: rebuild the
    indexes and record the new version, as Postgres itself recommends."""
    row = _psql(
        "SELECT datcollversion || '|' || pg_database_collation_actual_version(oid) "
        f"FROM pg_database WHERE datname = '{db}' AND datcollversion IS NOT NULL"
    )
    if not row:
        return  # C / no-locale database: nothing depends on glibc
    stored, actual = row.split("|")
    if stored == actual:
        return
    log.warning("database %s: collation %s, system has %s: reindexing", db, stored, actual)
    for sql in (f'REINDEX DATABASE "{db}"', f'ALTER DATABASE "{db}" REFRESH COLLATION VERSION'):
        subprocess.run(
            [f"{PG_BIN}/psql", "-h", PG_SOCKET_DIR, "-U", PG_USER, "-d", db, "-tAc", sql],
            check=True, capture_output=True,
        )
    log.info("database %s: indexes rebuilt for collation %s", db, actual)


# ------------------------------------------------------------------
# Process supervision
# ------------------------------------------------------------------
class Proc:
    def __init__(self, name, cwd, cmd, healthy, *, stop_signal=signal.SIGTERM,
                 run_as=None, after_healthy=None):
        self.name, self.cwd, self.cmd, self.healthy = name, cwd, cmd, healthy
        self.stop_signal, self.run_as, self.after_healthy = stop_signal, run_as, after_healthy
        self.popen: subprocess.Popen | None = None
        self.started_at = 0.0
        self.backoff = 1.0
        self.restart_at = 0.0

    def start(self, env):
        extra = {"user": self.run_as, "group": self.run_as} if self.run_as else {}
        self.popen = subprocess.Popen(self.cmd, cwd=self.cwd, env=env, **extra)
        self.started_at = time.monotonic()
        log.info("started %s (pid %s)", self.name, self.popen.pid)

    def running(self) -> bool:
        return self.popen is not None and self.popen.poll() is None


def wait_healthy(proc: Proc, env, stopping) -> None:
    """Wait for proc's health check, restarting it if it dies meanwhile.
    Gives up waiting after HEALTH_TIMEOUT but carries on (it may recover)."""
    deadline = time.monotonic() + HEALTH_TIMEOUT
    while not stopping() and time.monotonic() < deadline:
        if proc.running() and proc.healthy():
            log.info("%s is healthy", proc.name)
            if proc.after_healthy:
                proc.after_healthy()
            return
        if not proc.running():
            log.warning("%s exited with %s while starting; retrying in %.0fs",
                        proc.name, proc.popen.returncode, proc.backoff)
            time.sleep(proc.backoff)
            proc.backoff = min(proc.backoff * 2, MAX_BACKOFF)
            proc.start(env)
        time.sleep(0.5)
    if not stopping():
        log.warning("%s not healthy after %.0fs; continuing anyway", proc.name, HEALTH_TIMEOUT)


def build_processes(env: dict) -> list[Proc]:
    procs = []
    if not env.get("DATABASE_URL"):
        procs.append(Proc("postgres", "/", prepare_embedded_postgres(env), postgres_ready,
                          stop_signal=signal.SIGINT,  # "fast" shutdown: checkpoint + exit
                          run_as="postgres", after_healthy=ensure_database))
    else:
        log.info("DATABASE_URL is set: using the external database")
    procs += [
        Proc("llm", "/app/llm",
             [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
              "--port", "9000", "--log-level", "warning"],
             http_ok("http://127.0.0.1:9000/health")),
        Proc("api", "/app/api",
             [sys.executable, "-m", "uvicorn", "app.main:app", "--host", API_HOST,
              "--port", "8000", "--log-level", "warning"],
             http_ok("http://127.0.0.1:8000/health/db")),
        Proc("bot", "/app/bot", [sys.executable, "bot.py"], lambda: True),
    ]
    return procs


def main() -> int:
    stop = {"flag": False}

    def request_stop(signum, _frame):
        log.info("received signal %s, stopping", signum)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    stopping = lambda: stop["flag"]  # noqa: E731

    env = dict(os.environ)
    procs = build_processes(env)
    for proc in procs:
        if stopping():
            break
        proc.start(env)
        wait_healthy(proc, env, stopping)

    while not stopping():
        now = time.monotonic()
        for proc in procs:
            if proc.popen is None:
                continue
            if proc.running():
                if now - proc.started_at > STABLE_AFTER:
                    proc.backoff = 1.0  # it ran fine for a while: reset
                continue
            if proc.restart_at == 0.0:
                proc.restart_at = now + proc.backoff
                log.warning("%s exited with %s; restarting in %.0fs",
                            proc.name, proc.popen.returncode, proc.backoff)
                proc.backoff = min(proc.backoff * 2, MAX_BACKOFF)
            elif now >= proc.restart_at:
                proc.restart_at = 0.0
                proc.start(env)
        time.sleep(0.5)

    # Stop in reverse: bot, api, llm, then postgres last (once nothing uses it).
    for proc in reversed(procs):
        if proc.running():
            proc.popen.send_signal(proc.stop_signal)
            try:
                proc.popen.wait(20 if proc.name == "postgres" else 10)
            except subprocess.TimeoutExpired:
                log.warning("%s did not stop in time; killing", proc.name)
                proc.popen.kill()
    log.info("all stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
