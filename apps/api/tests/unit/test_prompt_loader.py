"""Unit tests for primitives/prompt_loader.py (spec §2.4, §4.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from primitives.prompt_loader import (
    list_templates,
    load_output_schema,
    load_template,
)


def test_loads_case_brief_template() -> None:
    """The real case_brief template parses into a fully populated PromptTemplate."""
    t = load_template("case_brief")
    assert t.name == "case_brief"
    assert t.version == "1.2.0"
    assert t.output_schema_path == "schemas/case_brief.json"
    assert t.model_defaults["model"] == "claude-opus-4-7"
    # Project policy: every prompt's max_tokens is set to the model's full
    # output budget (Opus 32K / Sonnet 64K / Haiku 16K). Don't reintroduce
    # smaller defaults — see feedback_no_token_caps memory.
    assert t.model_defaults["max_tokens"] == 32000
    assert "FIRAC" in (t.description or "")
    # The body must contain the case-brief specific section headers but NOT
    # the frontmatter delimiters.
    assert "Case opinion" in t.body
    assert "---" not in t.body.splitlines()[0:5]  # no frontmatter leaked
    assert t.identifier == "case_brief@1.2.0"


def test_load_missing_template_raises_filenotfound(tmp_path: Path) -> None:
    """Asking for a template that doesn't exist surfaces a FileNotFoundError
    whose message names the attempted path (helps debugging in tests)."""
    with pytest.raises(FileNotFoundError, match="not found"):
        load_template("does_not_exist", prompts_dir=tmp_path)


def test_load_malformed_frontmatter_raises(tmp_path: Path) -> None:
    """A template whose frontmatter is unparseable YAML raises ValueError
    mentioning 'frontmatter'."""
    bad = tmp_path / "bad.prompt.md"
    bad.write_text(
        "---\n"
        "name: bad\n"
        ": this : is : not : yaml\n"  # malformed
        "version: 1.0.0\n"
        "---\n"
        "body\n"
    )
    with pytest.raises(ValueError, match="frontmatter"):
        load_template("bad", prompts_dir=tmp_path)


def test_load_no_frontmatter_raises(tmp_path: Path) -> None:
    """A file without YAML frontmatter at all is rejected."""
    bad = tmp_path / "noheader.prompt.md"
    bad.write_text("# just a markdown file\n\nno frontmatter here.\n")
    with pytest.raises(ValueError, match="frontmatter"):
        load_template("noheader", prompts_dir=tmp_path)


def test_load_unclosed_frontmatter_raises(tmp_path: Path) -> None:
    """An opening `---` without a closing one is malformed."""
    bad = tmp_path / "unclosed.prompt.md"
    bad.write_text("---\nname: x\nversion: 1\noutput_schema: s.json\nbody...\n")
    with pytest.raises(ValueError, match="frontmatter"):
        load_template("unclosed", prompts_dir=tmp_path)


def test_load_output_schema() -> None:
    """The real case_brief schema is loadable as a JSON schema dict."""
    t = load_template("case_brief")
    schema = load_output_schema(t)
    assert isinstance(schema, dict)
    assert "$schema" in schema
    assert schema.get("title") == "CaseBrief"


def test_load_output_schema_missing(tmp_path: Path) -> None:
    """Pointing at a non-existent schema path surfaces FileNotFoundError."""
    bad = tmp_path / "missing.prompt.md"
    bad.write_text(
        "---\n"
        "name: missing\n"
        "version: 0.1.0\n"
        "output_schema: schemas/nope.json\n"
        "---\n"
        "body\n"
    )
    t = load_template("missing", prompts_dir=tmp_path)
    with pytest.raises(FileNotFoundError):
        load_output_schema(t, schemas_dir=tmp_path)


def test_list_templates_returns_case_brief() -> None:
    """The real prompts_dir lists case_brief."""
    names = list_templates()
    assert "case_brief" in names
    # Sorted output
    assert names == sorted(names)


def test_list_templates_on_empty_dir(tmp_path: Path) -> None:
    """An empty prompts dir returns an empty list (not an error)."""
    names = list_templates(prompts_dir=tmp_path)
    assert names == []


def test_load_template_inputs_as_mapping(tmp_path: Path) -> None:
    """Inputs declared as a YAML mapping parse into a {key: type} dict."""
    f = tmp_path / "t.prompt.md"
    f.write_text(
        "---\n"
        "name: t\n"
        "version: 0.0.1\n"
        "output_schema: schemas/x.json\n"
        "inputs:\n"
        "  case_opinion_block: Block\n"
        "  following_notes: list[Block]\n"
        "---\n"
        "body\n"
    )
    t = load_template("t", prompts_dir=tmp_path)
    assert t.inputs == {
        "case_opinion_block": "Block",
        "following_notes": "list[Block]",
    }


def test_load_template_inputs_as_list(tmp_path: Path) -> None:
    """Inputs declared as a YAML sequence (spec §4.3 worked example format)
    normalize to the same dict shape."""
    f = tmp_path / "t.prompt.md"
    f.write_text(
        "---\n"
        "name: t\n"
        "version: 0.0.1\n"
        "output_schema: schemas/x.json\n"
        "inputs:\n"
        "  - case_opinion_block: Block\n"
        "  - following_notes: list[Block]\n"
        "---\n"
        "body\n"
    )
    t = load_template("t", prompts_dir=tmp_path)
    assert t.inputs == {
        "case_opinion_block": "Block",
        "following_notes": "list[Block]",
    }
