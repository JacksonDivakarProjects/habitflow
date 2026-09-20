from fastapi import FastAPI

from app.routes import health, internal

app = FastAPI(title="HabitFlow API", version="0.1.0")

app.include_router(health.router)
app.include_router(internal.router)