import threading
import time

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import settings

# Hosts whose databases sleep when no connection is open (serverless Postgres).
# A normal pool would hold connections open and keep them awake, and billed, 24/7.
SERVERLESS_HOST_SUFFIXES = (".neon.tech",)
IDLE_CLOSE_SECONDS = 60  # well under Neon's 5-minute scale-to-zero delay


def pool_mode(url: str, setting: str = "auto") -> str:
    """'pool': normal pool; 'idle': pool that closes its connections after a quiet
    minute (serverless); 'none': a new connection for every checkout."""
    if setting == "on":
        return "pool"
    if setting == "off":
        return "none"
    if setting == "idle":
        return "idle"
    host = make_url(url).host or ""
    return "idle" if host.endswith(SERVERLESS_HOST_SUFFIXES) else "pool"


def use_pool(url: str, setting: str = "auto") -> bool:
    return pool_mode(url, setting) != "none"


def _close_when_idle(engine: Engine, idle_seconds: float) -> None:
    """Close pooled connections once none has been used for idle_seconds, so a
    serverless database can scale to zero; a burst of requests still reuses them."""
    last_used = {"t": time.monotonic()}

    @event.listens_for(engine, "checkout")
    def _checkout(*_):
        last_used["t"] = time.monotonic()

    @event.listens_for(engine, "checkin")
    def _checkin(*_):
        last_used["t"] = time.monotonic()

    def reaper():
        while True:
            time.sleep(min(10.0, idle_seconds / 2))
            pool = engine.pool
            if (pool.checkedout() == 0 and pool.checkedin() > 0
                    and time.monotonic() - last_used["t"] > idle_seconds):
                pool.dispose()  # closes the idle (checked-in) connections

    threading.Thread(target=reaper, name="db-idle-closer", daemon=True).start()


def make_engine(url: str, pool_setting: str = "auto",
                idle_seconds: float = IDLE_CLOSE_SECONDS) -> Engine:
    mode = pool_mode(url, pool_setting)
    if mode == "none":
        return create_engine(url, poolclass=NullPool)
    engine = create_engine(url, pool_pre_ping=True, pool_size=2 if mode == "idle" else 5)
    if mode == "idle":
        _close_when_idle(engine, idle_seconds)
    return engine


engine = make_engine(settings.database_url, settings.database_pool)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
