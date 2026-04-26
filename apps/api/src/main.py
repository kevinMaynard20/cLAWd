"""FastAPI entrypoint. Binds 127.0.0.1 only per spec §7.6.

Run with: `uvicorn main:app --host 127.0.0.1 --port 8000 --reload`
(PYTHONPATH must include apps/api/src; the pytest config does this automatically
for tests, and the Makefile target handles it for dev runs.)
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from data.db import init_schema
from routes import artifacts as artifacts_routes
from routes import books as books_routes
from routes import corpora as corpora_routes
from routes import costs as costs_routes
from routes import credentials as credentials_routes
from routes import export as export_routes
from routes import features as features_routes
from routes import flashcards as flashcards_routes
from routes import ingest as ingest_routes
from routes import lineage as lineage_routes
from routes import profiles as profiles_routes
from routes import retrieve as retrieve_routes
from routes import search as search_routes
from routes import system as system_routes
from routes import tasks as tasks_routes
from routes import transcripts as transcripts_routes
from routes import uploads as uploads_routes


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """App lifecycle hook (FastAPI 0.100+). On startup, ensure the SQLite
    schema exists. No teardown work for Phase 1."""
    init_schema()
    yield


app = FastAPI(
    title="Law School Study System API",
    version="0.1.0",
    description="Local-first study system for 1L doctrinal courses. See spec.md.",
    lifespan=lifespan,
)

# CORS: this is a single-user local app (spec §7.6 — backend bound to
# 127.0.0.1). The Next dev server proxies most requests through `/api/*`,
# but multi-MB PDF uploads have to bypass the proxy because Next 15's dev
# rewrite buffers request bodies at 10 MiB. Allow direct POSTs to the
# loopback port from the dev origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(credentials_routes.router)
app.include_router(corpora_routes.router)
app.include_router(books_routes.router)
app.include_router(artifacts_routes.router)
app.include_router(costs_routes.router)
app.include_router(retrieve_routes.router)
app.include_router(ingest_routes.router)
app.include_router(features_routes.router)
app.include_router(flashcards_routes.router)
app.include_router(profiles_routes.router)
app.include_router(transcripts_routes.router)
app.include_router(search_routes.router)
app.include_router(export_routes.router)
app.include_router(lineage_routes.router)
app.include_router(uploads_routes.router)
app.include_router(tasks_routes.router)
app.include_router(system_routes.router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe. No dependencies, no side effects."""
    return {"status": "ok"}
