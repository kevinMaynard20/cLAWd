"""Corpus export + restore routes (spec §6.3 / §9 Phase 6 backup/export).

Export streams a tar.gz of the entire corpus state. Restore reads such an
archive and re-creates every row inside a new corpus.
"""

from __future__ import annotations

import io

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from data.db import get_session
from data.models import Corpus
from features.corpus_export import (
    CorpusExportError,
    export_corpus,
    export_filename,
)
from features.corpus_restore import (
    CorpusRestoreError,
    restore_corpus,
)

router = APIRouter(tags=["export"])


@router.get("/corpora/{corpus_id}/export")
def export_corpus_route(
    corpus_id: str,
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """Build and stream a tar.gz archive of the corpus's full state.

    The archive contains JSONL files per table plus a manifest. Schema
    version is in `manifest.json` so future restore code can branch on it.
    """
    corpus = session.exec(select(Corpus).where(Corpus.id == corpus_id)).first()
    if corpus is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Corpus {corpus_id!r} not found.",
        )

    try:
        archive_bytes = export_corpus(session, corpus_id)
    except CorpusExportError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    fname = export_filename(corpus)
    return StreamingResponse(
        io.BytesIO(archive_bytes),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Content-Length": str(len(archive_bytes)),
        },
    )


class RestoreResponse(BaseModel):
    new_corpus_id: str
    table_counts: dict[str, int]
    skipped: dict[str, int]


@router.post("/corpora/restore", response_model=RestoreResponse)
async def restore_corpus_route(
    archive: UploadFile = File(..., description="A tar.gz produced by the export endpoint."),
    new_corpus_name: str | None = Form(None),
    preserve_corpus_id: bool = Form(False),
) -> RestoreResponse:
    """Restore a corpus from an export archive (Q51).

    The archive's `manifest.json` `schema_version` must match the current
    server's. The new corpus gets a fresh id by default; passing
    `preserve_corpus_id=true` is allowed when restoring into a clean DB.
    """
    archive_bytes = await archive.read()
    if not archive_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty archive uploaded.",
        )
    try:
        result = restore_corpus(
            archive_bytes,
            new_corpus_name=new_corpus_name,
            preserve_corpus_id=preserve_corpus_id,
        )
    except CorpusRestoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return RestoreResponse(
        new_corpus_id=result.new_corpus_id,
        table_counts=result.table_counts,
        skipped=result.skipped,
    )
