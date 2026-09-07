from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api.routes import router
from .infrastructure.database import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="LedgerFlow Payments API", version="0.1.0", lifespan=lifespan)
app.include_router(router, prefix="/api/v1")