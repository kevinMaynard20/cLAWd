"""Prompt template loader (spec §2.4, §4.3).

Prompts are *data*, not code. Every prompt lives at
`packages/prompts/{name}.prompt.md` with a YAML frontmatter header declaring
its name, version, inputs, the JSON schema its output must match, and the
default model configuration. This module parses those files into a typed
:class:`PromptTemplate`, surfaces the corresponding output schema, and can
enumerate the catalog for UI pickers.

We deliberately keep the loader narrowly-scoped (no rendering, no generation).
Rendering lives in :mod:`primitives.template_renderer`; generation orchestration
lives in :mod:`primitives.generate`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptTemplate:
    """Parsed representation of a prompt template file.

    Fields mirror the frontmatter keys named in spec §4.3. Unknown frontmatter
    keys are preserved via `model_defaults` / `inputs` dicts so new template
    authors can experiment without loader changes.
    """

    name: str
    version: str
    description: str | None
    inputs: dict[str, str]
    output_schema_path: str
    model_defaults: dict[str, Any]
    body: str
    source_path: Path

    @property
    def identifier(self) -> str:
        """Canonical identifier used for Artifact.prompt_template and cache keys."""
        return f"{self.name}@{self.version}"


# ---------------------------------------------------------------------------
# Path discovery
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Walk up from this module until a directory containing `spec.md` is found.

    Mirrors the convention used in `data.db` and `costs.pricing` so all modules
    agree on what "the repo" is. Callers can still pass an explicit
    `prompts_dir` to override for tests.
    """
    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "spec.md").exists():
            return candidate
    return Path.cwd()


def _default_prompts_dir() -> Path:
    return _repo_root() / "packages" / "prompts"


def _default_schemas_dir() -> Path:
    return _repo_root() / "packages"


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


_FRONTMATTER_DELIM = "---"


def _split_frontmatter(text: str, *, source: Path) -> tuple[str, str]:
    """Split a Markdown-with-YAML-frontmatter document.

    Returns ``(yaml_text, body_text)``. Raises `ValueError` if the frontmatter
    block is missing or not properly closed.

    The expected shape is::

        ---
        key: value
        ...
        ---

        body...
    """
    # Accept a leading BOM / whitespace but the first non-whitespace line must
    # be the opening delimiter.
    stripped_leading = text.lstrip("﻿")
    if not stripped_leading.startswith(_FRONTMATTER_DELIM):
        raise ValueError(
            f"Missing YAML frontmatter in prompt template {source}: "
            f"expected a line starting with '---' at the top of the file"
        )

    # Work on a normalized view: strip BOM but keep interior content intact.
    rest = stripped_leading[len(_FRONTMATTER_DELIM) :]
    # The opening delimiter must be terminated by a newline.
    if not rest.startswith(("\n", "\r\n", "\r")):
        raise ValueError(
            f"Malformed frontmatter in prompt template {source}: "
            f"opening '---' must be on its own line"
        )

    # Find the closing delimiter on its own line.
    lines = rest.splitlines(keepends=False)
    # `splitlines` drops the leading empty segment introduced by the leading
    # newline, so lines[0] is the first YAML line.
    close_idx: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == _FRONTMATTER_DELIM:
            close_idx = i
            break
    if close_idx is None:
        raise ValueError(
            f"Malformed frontmatter in prompt template {source}: "
            f"closing '---' line not found"
        )

    yaml_text = "\n".join(lines[:close_idx])
    body_text = "\n".join(lines[close_idx + 1 :])
    # Preserve the trailing newline convention of the source file so rendered
    # prompts end cleanly when the file did.
    if text.endswith("\n") and not body_text.endswith("\n"):
        body_text += "\n"
    # Drop a single leading blank line that conventionally follows the
    # frontmatter close; everything else is body.
    if body_text.startswith("\n"):
        body_text = body_text[1:]
    return yaml_text, body_text


def _coerce_inputs(raw: Any, *, source: Path) -> dict[str, str]:
    """Normalize the `inputs:` block.

    Two accepted shapes per spec §4.3:

    - mapping: ``{case_opinion_block: Block, following_notes: list[Block]}``
    - list of single-key dicts: ``[{case_opinion_block: Block}, ...]``

    Returns a plain ``{name: type_or_description}`` dict. Missing / empty is
    tolerated and yields ``{}`` — some templates may not declare inputs.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        out: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, dict) or len(item) != 1:
                raise ValueError(
                    f"Malformed frontmatter 'inputs' entry in {source}: "
                    f"expected single-key dict, got {item!r}"
                )
            (k, v), = item.items()
            out[str(k)] = str(v)
        return out
    raise ValueError(
        f"Malformed frontmatter 'inputs' field in {source}: "
        f"expected mapping or list, got {type(raw).__name__}"
    )


def _coerce_model_defaults(raw: Any, *, source: Path) -> dict[str, Any]:
    """Normalize model_defaults into a dict. Missing ⇒ empty dict (callers fall
    back to `config/models.toml` per spec §7.7.6)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"Malformed frontmatter 'model_defaults' in {source}: "
            f"expected mapping, got {type(raw).__name__}"
        )
    return dict(raw)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_template(name: str, *, prompts_dir: Path | None = None) -> PromptTemplate:
    """Load a prompt template by its canonical short name.

    Resolves to ``<prompts_dir>/<name>.prompt.md`` (default `prompts_dir` is
    `<repo_root>/packages/prompts/`). Raises:

    - :class:`FileNotFoundError` — the file does not exist (message includes
      the full resolved path for debugging).
    - :class:`ValueError` — the file exists but its frontmatter is missing or
      malformed (error message always contains the word ``"frontmatter"``).
    """
    directory = prompts_dir if prompts_dir is not None else _default_prompts_dir()
    source = (directory / f"{name}.prompt.md").resolve()
    if not source.exists():
        raise FileNotFoundError(
            f"Prompt template {name!r} not found at {source}. "
            f"Check that the file exists in {directory}."
        )

    raw_text = source.read_text(encoding="utf-8")
    yaml_text, body = _split_frontmatter(raw_text, source=source)

    try:
        parsed = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(
            f"Unparseable frontmatter in prompt template {source}: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise ValueError(
            f"Malformed frontmatter in prompt template {source}: "
            f"expected mapping, got {type(parsed).__name__}"
        )

    required = ("name", "version", "output_schema")
    missing = [k for k in required if k not in parsed]
    if missing:
        raise ValueError(
            f"Malformed frontmatter in {source}: missing required keys {missing}"
        )

    description = parsed.get("description")
    if description is not None and not isinstance(description, str):
        description = str(description)

    return PromptTemplate(
        name=str(parsed["name"]),
        version=str(parsed["version"]),
        description=description.strip() if description else None,
        inputs=_coerce_inputs(parsed.get("inputs"), source=source),
        output_schema_path=str(parsed["output_schema"]),
        model_defaults=_coerce_model_defaults(parsed.get("model_defaults"), source=source),
        body=body,
        source_path=source,
    )


def load_output_schema(
    template: PromptTemplate,
    *,
    schemas_dir: Path | None = None,
) -> dict[str, Any]:
    """Load and return the JSON schema a template's output must match.

    ``template.output_schema_path`` is a path relative to `packages/` (the
    convention used by the real templates; see `case_brief.prompt.md`). If
    a caller needs a different root they can pass `schemas_dir`.
    """
    # Permit absolute paths for tests that want to point elsewhere.
    candidate = Path(template.output_schema_path)
    if candidate.is_absolute():
        schema_path = candidate
    else:
        base = schemas_dir if schemas_dir is not None else _default_schemas_dir()
        schema_path = (base / template.output_schema_path).resolve()

    if not schema_path.exists():
        raise FileNotFoundError(
            f"Output schema for template {template.name!r} not found at "
            f"{schema_path}. Expected path is `{template.output_schema_path}` "
            f"(relative to {schemas_dir or _default_schemas_dir()})."
        )

    import json

    return json.loads(schema_path.read_text(encoding="utf-8"))


def list_templates(prompts_dir: Path | None = None) -> list[str]:
    """Return the sorted list of available template names (without the
    ``.prompt.md`` suffix). Used by the UI "select template" pickers."""
    directory = prompts_dir if prompts_dir is not None else _default_prompts_dir()
    if not directory.exists():
        return []
    names = [p.stem.removesuffix(".prompt") for p in directory.glob("*.prompt.md")]
    return sorted(names)


__all__ = [
    "PromptTemplate",
    "list_templates",
    "load_output_schema",
    "load_template",
]
