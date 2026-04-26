"""FastAPI router modules (spec §7.2, §1.6 of the checklist).

Each submodule defines a `router` (APIRouter) that `main.py` mounts.
Routes return pydantic models; error conditions raise `HTTPException` with
actionable messages per spec §7.5.
"""
