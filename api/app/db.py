from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import settings

# Hosts whose databases sleep when no connection is open (serverless Postgres).
# A pool would hold connections open and keep them awake, and billed, 24/7.
SERVERLESS_HOST_SUFFIXES = (".neon.tech",)


def use_pool(url: str, setting: str = "auto") -> bool:
    if setting in ("on", "off"):
        return setting == "on"
    host = make_url(url).host or ""
    return not host.endswith(SERVERLESS_HOST_SUFFIXES)


def make_engine(url: str, pool_setting: str = "auto") -> Engine:
    if use_pool(url, pool_setting):
        return create_engine(url, pool_pre_ping=True)
    # One connection per request, closed right after, so the database can
    # scale to zero between uses.
    return create_engine(url, poolclass=NullPool)


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
