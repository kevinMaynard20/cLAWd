"""Pytest fixtures shared across unit/integration/e2e.

pyproject.toml already sets pythonpath = ["apps/api/src"], so imports like
`from primitives.ingest import ...` work without installing the package.
"""

from __future__ import annotations
