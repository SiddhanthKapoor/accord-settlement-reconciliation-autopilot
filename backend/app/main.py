from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.ai import router as ai_router
from app.api.investigate import router as investigate_router
from app.api.routes import router
from app.api.runs import router as runs_router
from app.config import CORS_ORIGINS
from app.ledger.db import init_db

app = FastAPI(
    title="Accord",
    description="AI-assisted reconciliation between merchant order records and Razorpay-style "
    "settlement data. Deterministic matching for everything that can be resolved "
    "mathematically; a narrow, confidence-gated model call only for genuinely ambiguous "
    "reference matching. See README.md for the evaluation methodology and results.",
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


app.include_router(router)
app.include_router(runs_router)
app.include_router(ai_router)
app.include_router(investigate_router)
