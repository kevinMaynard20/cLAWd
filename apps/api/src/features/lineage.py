"""Artifact lineage (spec §7.4 — per-artifact lineage viewable in a debug UI).

Walks an Artifact's `parent_artifact_id` chain upward and gathers the
CostEvents that contributed to it, plus the source Block / TranscriptSegment
ids it cited. Used by the lineage page in the UI ("show me everything
about this artifact").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlmodel import Session, select

from data.models import Artifact, Block, CostEvent, TranscriptSegment


@dataclass
class LineageNode:
    """One artifact in the lineage chain."""

    id: str
    type: str
    created_at: datetime
    created_by: str
    prompt_template: str
    llm_model: str
    cost_usd: Decimal
    cache_key: str
    parent_artifact_id: str | None
    sources_summary: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LineageEvent:
    """One CostEvent attributed to the artifact."""

    id: str
    timestamp: datetime
    feature: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    total_cost_usd: Decimal
    cached: bool


@dataclass
class LineageReport:
    """Result of `build_lineage(artifact_id)`. The caller renders the UI;
    this is just structured data."""

    target_artifact_id: str
    chain: list[LineageNode]  # root-first ordering (oldest ancestor → target)
    events: list[LineageEvent]
    total_cost_usd: Decimal
    cited_block_count: int
    cited_segment_count: int
    missing_sources: list[str]   # source ids declared but not found in the DB


class LineageError(RuntimeError):
    """Raised when the target artifact doesn't exist."""


def _walk_chain(session: Session, artifact_id: str, max_depth: int = 64) -> list[Artifact]:
    """Return the parent chain, target-last, root-first. Hard cap on depth
    to avoid infinite loops if data is corrupt."""
    chain: list[Artifact] = []
    seen: set[str] = set()
    current_id: str | None = artifact_id
    while current_id is not None:
        if current_id in seen or len(chain) >= max_depth:
            break
        seen.add(current_id)
        row = session.exec(select(Artifact).where(Artifact.id == current_id)).first()
        if row is None:
            break
        chain.append(row)
        current_id = row.parent_artifact_id
    chain.reverse()  # root-first
    return chain


def _summarize_sources(
    session: Session, artifact: Artifact
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build the per-source summary block (kind + display label) plus a list
    of declared source ids that don't resolve in the DB. The DB lookups are
    the same anti-hallucination check verify(citation_grounding) does — so
    if the artifact passed verify on creation, missing_sources here should
    be empty."""
    summary: list[dict[str, Any]] = []
    missing: list[str] = []
    for src in artifact.sources or []:
        if not isinstance(src, dict):
            continue
        kind = str(src.get("kind", "unknown"))
        sid = str(src.get("id", ""))
        if not sid:
            continue
        if kind == "block":
            row = session.exec(select(Block).where(Block.id == sid)).first()
            if row is None:
                missing.append(sid)
                summary.append({"kind": "block", "id": sid, "found": False})
            else:
                summary.append(
                    {
                        "kind": "block",
                        "id": sid,
                        "found": True,
                        "book_id": row.book_id,
                        "source_page": row.source_page,
                        "block_type": row.type.value,
                        "case_name": row.block_metadata.get("case_name"),
                    }
                )
        elif kind == "transcript_segment":
            row = session.exec(
                select(TranscriptSegment).where(TranscriptSegment.id == sid)
            ).first()
            if row is None:
                missing.append(sid)
                summary.append(
                    {"kind": "transcript_segment", "id": sid, "found": False}
                )
            else:
                summary.append(
                    {
                        "kind": "transcript_segment",
                        "id": sid,
                        "found": True,
                        "transcript_id": row.transcript_id,
                        "order_index": row.order_index,
                        "speaker": row.speaker.value,
                    }
                )
        else:
            summary.append({"kind": kind, "id": sid, "found": None})
    return summary, missing


def _to_node(session: Session, artifact: Artifact) -> tuple[LineageNode, list[str]]:
    sources_summary, missing = _summarize_sources(session, artifact)
    node = LineageNode(
        id=artifact.id,
        type=artifact.type.value,
        created_at=artifact.created_at,
        created_by=artifact.created_by.value,
        prompt_template=artifact.prompt_template,
        llm_model=artifact.llm_model,
        cost_usd=artifact.cost_usd,
        cache_key=artifact.cache_key,
        parent_artifact_id=artifact.parent_artifact_id,
        sources_summary=sources_summary,
    )
    return node, missing


def build_lineage(session: Session, artifact_id: str) -> LineageReport:
    """Spec §7.4 entrypoint. Returns a structured report covering the full
    parent chain, every CostEvent ever attributed to those artifacts, and a
    rolled-up per-target source summary.
    """
    target = session.exec(select(Artifact).where(Artifact.id == artifact_id)).first()
    if target is None:
        raise LineageError(f"Artifact {artifact_id!r} not found.")

    chain_artifacts = _walk_chain(session, artifact_id)
    if not chain_artifacts:
        # Pathological — target exists but chain walk returned empty. Use just
        # the target as a one-node chain.
        chain_artifacts = [target]

    chain_nodes: list[LineageNode] = []
    all_missing: list[str] = []
    for art in chain_artifacts:
        node, missing = _to_node(session, art)
        chain_nodes.append(node)
        all_missing.extend(missing)

    # CostEvents for any artifact in the chain (parent calls + target call).
    chain_ids = [a.id for a in chain_artifacts]
    events_rows = list(
        session.exec(
            select(CostEvent)
            .where(CostEvent.artifact_id.in_(chain_ids))
            .order_by(CostEvent.timestamp)
        ).all()
    )
    events = [
        LineageEvent(
            id=e.id,
            timestamp=e.timestamp,
            feature=e.feature,
            model=e.model,
            provider=e.provider.value,
            input_tokens=e.input_tokens,
            output_tokens=e.output_tokens,
            total_cost_usd=e.total_cost_usd,
            cached=e.cached,
        )
        for e in events_rows
    ]

    total_cost = sum(
        (e.total_cost_usd for e in events_rows),
        start=Decimal("0"),
    )

    # Source counts on the *target* (not the whole chain — usually what users
    # care about; the chain ancestors have their own sources).
    target_node = chain_nodes[-1]
    cited_block = sum(1 for s in target_node.sources_summary if s.get("kind") == "block")
    cited_seg = sum(
        1 for s in target_node.sources_summary if s.get("kind") == "transcript_segment"
    )

    return LineageReport(
        target_artifact_id=artifact_id,
        chain=chain_nodes,
        events=events,
        total_cost_usd=total_cost,
        cited_block_count=cited_block,
        cited_segment_count=cited_seg,
        missing_sources=sorted(set(all_missing)),
    )


__all__ = [
    "LineageError",
    "LineageEvent",
    "LineageNode",
    "LineageReport",
    "build_lineage",
]
