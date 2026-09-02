from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.config import CORS_ORIGINS
from app.ledger.db import init_db

app = FastAPI(
    title="Interlock",
    description="Settlement-time integrity verification for agentic payments. "
    "See /docs for the API and the repository README for the design rationale.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/health")
def health():
    return {"status": "ok", "service": "interlock"}


app.include_router(router)
