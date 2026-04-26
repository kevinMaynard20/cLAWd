"""Unit tests for primitives/template_renderer.py (spec §4.3 prompt rendering)."""

from __future__ import annotations

from pathlib import Path

import pytest

from primitives.prompt_loader import PromptTemplate, load_template
from primitives.template_renderer import TemplateRenderError, render_template


def _template(body: str) -> PromptTemplate:
    """Helper: build a minimal PromptTemplate with a custom body."""
    return PromptTemplate(
        name="test",
        version="0.0.1",
        description=None,
        inputs={},
        output_schema_path="schemas/fake.json",
        model_defaults={},
        body=body,
        source_path=Path("/in-memory"),
    )


def test_renders_simple_variable() -> None:
    t = _template("Hello {{name}}")
    assert render_template(t, {"name": "Shelley"}) == "Hello Shelley"


def test_renders_dotted_access() -> None:
    t = _template("id={{block.id}} page={{block.source_page}}")
    out = render_template(t, {"block": {"id": "blk-1", "source_page": 518}})
    assert out == "id=blk-1 page=518"


def test_renders_each_with_dot_property() -> None:
    t = _template("{{#each items}}- {{this.title}}\n{{/each}}")
    out = render_template(t, {"items": [{"title": "A"}, {"title": "B"}]})
    # pybars3's `{{#each}}` merges iterations back-to-back; both titles must
    # be present in order.
    assert "A" in out and "B" in out
    assert out.index("A") < out.index("B")


def test_renders_if_block() -> None:
    t = _template("{{#if show}}YES{{/if}}")
    assert render_template(t, {"show": True}) == "YES"
    assert render_template(t, {"show": False}) == ""
    assert render_template(t, {}) == ""


def test_renders_case_brief_with_real_template() -> None:
    """Render the real case_brief template against a minimal-but-valid context
    of dict-shaped Block stand-ins. The output must contain our header markers
    and the block's markdown body."""
    template = load_template("case_brief")
    context = {
        "case_opinion_block": {
            "id": "op-shelley",
            "source_page": 518,
            "type": "case_opinion",
            "markdown": "The judicial enforcement of private racially restrictive covenants…",
            "block_metadata": {
                "case_name": "Shelley v. Kraemer",
                "court": "Supreme Court of the United States",
                "year": 1948,
                "citation": "334 U.S. 1",
            },
        },
        "following_notes": [
            {
                "id": "nt-shelley-1",
                "source_page": 519,
                "type": "numbered_note",
                "markdown": "First note body.",
                "block_metadata": {"number": 1},
            },
            {
                "id": "nt-shelley-2",
                "source_page": 520,
                "type": "numbered_note",
                "markdown": "Second note body.",
                "block_metadata": {"number": 2},
            },
        ],
        "professor_profile": None,
        "book_toc_context": None,
    }
    out = render_template(template, context)
    assert "Case opinion" in out
    assert "Shelley v. Kraemer" in out
    assert "The judicial enforcement" in out
    assert "op-shelley" in out
    assert "First note body." in out
    assert "Second note body." in out
    # Professor / TOC sections are gated on their inputs — they must not appear.
    assert "Professor context" not in out
    assert "Where this case appears" not in out


def test_renders_case_brief_with_professor_profile() -> None:
    """When a professor profile is provided the gated section appears."""
    template = load_template("case_brief")
    context = {
        "case_opinion_block": {
            "id": "op-1",
            "source_page": 100,
            "type": "case_opinion",
            "markdown": "body",
            "block_metadata": {"case_name": "Foo v. Bar"},
        },
        "following_notes": [],
        "professor_profile": {
            "professor_name": "Pollack",
            "course": "Property",
            "school": "School",
            "favored_framings": ["rule-first"],
            "pet_peeves": [{"name": "pp1", "pattern": "ptrn"}],
            "stable_traps": [{"name": "tr1", "desc": "desc"}],
        },
        "book_toc_context": None,
    }
    out = render_template(template, context)
    assert "Professor context" in out
    assert "Pollack" in out
    assert "rule-first" in out


def test_malformed_template_raises() -> None:
    """An unclosed `{{#each}}` is flagged pre-compile as a TemplateRenderError."""
    t = _template("before {{#each items}}- {{this}}")
    with pytest.raises(TemplateRenderError, match="Malformed"):
        render_template(t, {"items": [1, 2]})


def test_stray_close_raises() -> None:
    """A stray `{{/each}}` with no open is caught."""
    t = _template("{{/each}}hi")
    with pytest.raises(TemplateRenderError, match="stray"):
        render_template(t, {})


def test_pybars_syntax_error_raises() -> None:
    """A pybars compile error (mismatched close) surfaces as TemplateRenderError."""
    # Opens `each`, closes `if` — pybars itself catches this one.
    t = _template("{{#each x}}hi{{/if}}")
    with pytest.raises(TemplateRenderError):
        render_template(t, {"x": [1]})
