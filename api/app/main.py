from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import engine
from app.migrate import run_migrations
from app.routes import health, internal


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations(engine)
    yield


app = FastAPI(title="HabitFlow API", version="0.1.0", lifespan=lifespan)

app.include_router(health.router)
app.include_router(internal.router)
