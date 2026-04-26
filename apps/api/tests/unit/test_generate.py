"""Unit tests for primitives/generate.py (spec §4.3 Generate primitive).

Covers:

- Happy path: valid JSON response → persisted Artifact + CostEvent.
- Caching: second call with identical inputs returns the cached Artifact and
  emits a `cached=True, total_cost_usd=0` CostEvent (§4.3).
- Force-regenerate bypass.
- Retry on malformed JSON and on schema violation.
- Retry exhaustion → GenerateError.
- Missing API key → GenerateError.
- CostEvent token-count / pricing wiring.
- Model override + config/models.toml lookup precedence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlmodel import Session, select

from data import db
from data.models import (
    Artifact,
    ArtifactType,
    Corpus,
    CostEvent,
    Credentials,
    Provider,
)
from primitives import generate as generate_module
from primitives.generate import (
    GenerateError,
    GenerateRequest,
    generate,
    set_anthropic_client_factory,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fresh SQLite file per test (same pattern as test_models.py)."""
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "test.db"))
    db.reset_engine()
    db.init_schema()
    # Seed the Corpus FK target so Artifact inserts don't trip FOREIGN KEY.
    with Session(db.get_engine()) as s:
        s.add(Corpus(id="corpus-1", name="Property – Pollack", course="Property"))
        s.commit()
    yield
    db.reset_engine()


@pytest.fixture
def fake_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch `load_credentials` inside generate.py to return a fixed key."""
    monkeypatch.setattr(
        generate_module,
        "load_credentials",
        lambda: Credentials(anthropic_api_key=SecretStr("sk-ant-test-key-abcdXXXX")),
    )


@pytest.fixture
def no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch `load_credentials` to return an empty envelope — forces missing-key error."""
    monkeypatch.setattr(
        generate_module,
        "load_credentials",
        lambda: Credentials(anthropic_api_key=None),
    )


@pytest.fixture(autouse=True)
def _reset_client_factory():
    """Restore the SDK factory after every test so leaks don't bleed across."""
    yield
    set_anthropic_client_factory(None)


# ---------------------------------------------------------------------------
# Mock client scaffolding
# ---------------------------------------------------------------------------


@dataclass
class FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeResponse:
    content: list[FakeTextBlock]
    usage: FakeUsage
    id: str = "msg_fake_1"
    model: str = "claude-opus-4-7"


class FakeMessages:
    def __init__(self, responses: list[FakeResponse] | list[Exception]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeMessages.create called but no responses queued")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, responses: list[FakeResponse] | list[Exception]):
        self.messages = FakeMessages(responses)
        self.api_keys_seen: list[str] = []


def _install_client(
    responses: list[FakeResponse] | list[Exception],
) -> FakeClient:
    client = FakeClient(responses)

    def _factory(api_key: str) -> FakeClient:
        client.api_keys_seen.append(api_key)
        return client

    set_anthropic_client_factory(_factory)
    return client


# ---------------------------------------------------------------------------
# Sample payloads
# ---------------------------------------------------------------------------


def _valid_case_brief_json() -> dict:
    """A minimal valid payload matching packages/schemas/case_brief.json."""
    block_id = "op-shelley"
    claim = {"text": "sample", "source_block_ids": [block_id]}
    return {
        "case_name": "Shelley v. Kraemer",
        "citation": "334 U.S. 1",
        "court": "Supreme Court of the United States",
        "year": 1948,
        "facts": [claim],
        "procedural_posture": claim,
        "issue": {"text": "Does state enforcement count as state action?", "source_block_ids": [block_id]},
        "holding": claim,
        "rule": claim,
        "reasoning": [claim],
        "significance": claim,
        "where_this_fits": None,
        "limitations": ["procedural posture unclear"],
        "sources": [block_id, "nt-1"],
    }


def _build_brief_request() -> GenerateRequest:
    return GenerateRequest(
        template_name="case_brief",
        inputs={
            "case_opinion_block": {
                "id": "op-shelley",
                "source_page": 518,
                "type": "case_opinion",
                "markdown": "Opinion body.",
                "block_metadata": {"case_name": "Shelley v. Kraemer"},
            },
            "following_notes": [
                {
                    "id": "nt-1",
                    "source_page": 519,
                    "type": "numbered_note",
                    "markdown": "note",
                    "block_metadata": {"number": 1},
                }
            ],
        },
        artifact_type=ArtifactType.CASE_BRIEF,
        corpus_id="corpus-1",
    )


def _response_with_json(payload: dict, *, input_tokens: int = 1200, output_tokens: int = 450) -> FakeResponse:
    import json as _json

    return FakeResponse(
        content=[FakeTextBlock(text=_json.dumps(payload))],
        usage=FakeUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _text_response(text: str, *, input_tokens: int = 100, output_tokens: int = 50) -> FakeResponse:
    return FakeResponse(
        content=[FakeTextBlock(text=text)],
        usage=FakeUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_generate_happy_path(temp_db: None, fake_credentials: None) -> None:
    """Fake client returns valid JSON → Artifact persisted + CostEvent emitted."""
    client = _install_client([_response_with_json(_valid_case_brief_json())])

    result = generate(_build_brief_request())

    assert not result.cache_hit
    art = result.artifact
    assert art.type is ArtifactType.CASE_BRIEF
    assert art.corpus_id == "corpus-1"
    assert art.content["case_name"] == "Shelley v. Kraemer"
    assert art.prompt_template == "case_brief@1.2.0"
    assert art.llm_model == "claude-opus-4-7"
    assert art.cache_key != ""
    assert art.cost_usd > Decimal("0")
    # Source-envelope canonicalization: list of {"kind":"block","id":...}.
    ids = [s["id"] for s in art.sources]
    assert "op-shelley" in ids and "nt-1" in ids

    # Persisted Artifact row and CostEvent row both exist.
    with Session(db.get_engine()) as s:
        rows = s.exec(select(Artifact)).all()
        assert len(rows) == 1
        events = s.exec(select(CostEvent)).all()
        assert len(events) == 1
        ev = events[0]
        assert ev.feature == "case_brief"
        assert ev.artifact_id == art.id
        assert ev.cached is False
        assert ev.input_tokens == 1200
        assert ev.output_tokens == 450
        assert ev.provider is Provider.ANTHROPIC

    # Exactly one live call, keyed through our injected key.
    assert len(client.messages.calls) == 1
    assert client.api_keys_seen == ["sk-ant-test-key-abcdXXXX"]


def test_generate_cache_hit(temp_db: None, fake_credentials: None) -> None:
    """Identical inputs → the second call returns the cached Artifact and
    emits a `cached=True` CostEvent."""
    client = _install_client(
        [_response_with_json(_valid_case_brief_json())]
        # only one queued — if generate tries a second live call, the queue
        # is empty and the fake raises.
    )

    req = _build_brief_request()
    first = generate(req)
    assert first.cache_hit is False

    second = generate(req)
    assert second.cache_hit is True
    assert second.artifact.id == first.artifact.id

    # Still only one Anthropic call.
    assert len(client.messages.calls) == 1

    # Two CostEvents total: one live, one cached.
    with Session(db.get_engine()) as s:
        events = s.exec(select(CostEvent).order_by(CostEvent.timestamp)).all()
        assert len(events) == 2
        live, cached = events
        assert live.cached is False
        assert cached.cached is True
        assert cached.total_cost_usd == Decimal("0")
        assert cached.feature == "case_brief"
        assert cached.artifact_id == first.artifact.id


def test_generate_force_regenerate(temp_db: None, fake_credentials: None) -> None:
    """With `force_regenerate=True`, even a cache hit triggers a new API call
    and persists a new Artifact row."""
    _install_client(
        [
            _response_with_json(_valid_case_brief_json()),
            _response_with_json(_valid_case_brief_json()),
        ]
    )

    req = _build_brief_request()
    first = generate(req)

    forced_req = GenerateRequest(
        template_name=req.template_name,
        inputs=req.inputs,
        artifact_type=req.artifact_type,
        corpus_id=req.corpus_id,
        force_regenerate=True,
    )
    second = generate(forced_req)

    assert second.cache_hit is False
    assert second.artifact.id != first.artifact.id

    with Session(db.get_engine()) as s:
        rows = s.exec(select(Artifact)).all()
        assert len(rows) == 2


def test_generate_retry_on_malformed_json(temp_db: None, fake_credentials: None) -> None:
    """First call returns non-JSON prose, second returns valid → success with
    exactly 2 API calls."""
    client = _install_client(
        [
            _text_response("I am thinking about this case. No JSON here, sorry."),
            _response_with_json(_valid_case_brief_json()),
        ]
    )

    result = generate(_build_brief_request())

    assert not result.cache_hit
    assert result.artifact.content["case_name"] == "Shelley v. Kraemer"
    assert len(client.messages.calls) == 2

    # The retry prompt must include the correction directive.
    retry_msgs = client.messages.calls[1]["messages"]
    retry_user = retry_msgs[0]["content"]
    assert "REQUIRED FIXES" in retry_user or "corrected JSON" in retry_user


def test_generate_retry_on_schema_violation(temp_db: None, fake_credentials: None) -> None:
    """The repair pass coerces COMMON drift (string-shaped Claim fields,
    missing top-level `sources` array) without a round-trip retry. This
    makes the wizard feel reliable when the model returns a 95%-valid brief
    that just shipped one field as a bare string.

    Truly missing required content (e.g. no `facts` array at all) still
    triggers a retry — see `test_generate_retry_when_repair_cannot_rescue`.
    """
    payload = _valid_case_brief_json()
    # Drift: model returned `significance` as a bare string instead of a
    # Claim object, and forgot the top-level `sources` field. Both are
    # commonly observed and both should be silently repaired.
    payload["significance"] = "Established the state-action doctrine."
    payload.pop("sources", None)
    client = _install_client([_response_with_json(payload)])

    result = generate(_build_brief_request())
    assert not result.cache_hit
    # Single API call — repair satisfied the schema without a retry.
    assert len(client.messages.calls) == 1
    content = result.artifact.content
    assert isinstance(content["significance"], dict)
    assert content["significance"]["text"] == "Established the state-action doctrine."
    assert content["sources"] == []


def test_generate_exhausts_retries_raises(temp_db: None, fake_credentials: None) -> None:
    """After two failures the call raises GenerateError with a helpful message."""
    _install_client(
        [
            _text_response("still no json"),
            _text_response("second attempt also garbled"),
        ]
    )

    with pytest.raises(GenerateError) as exc:
        generate(_build_brief_request())

    msg = str(exc.value)
    assert "2 attempts" in msg or "valid JSON" in msg

    # No Artifact persisted.
    with Session(db.get_engine()) as s:
        rows = s.exec(select(Artifact)).all()
        assert len(rows) == 0


def test_generate_exhausts_schema_retries_raises(temp_db: None, fake_credentials: None) -> None:
    """Schema violations the repair pass can't rescue still exhaust retries.

    The repair pass fills missing Claim fields with placeholders, but it
    cannot rescue a candidate where a Claim has an explicit non-string
    ``text`` value — that's a type error the model's correction pass needs
    to address.
    """
    bad = {
        "case_name": "Shelley v. Kraemer",
        # Repair leaves dict-shaped claims alone if their text+source_block_ids
        # pass surface checks; here `text` is an int → schema rejects → retry.
        "issue": {"text": 123, "source_block_ids": ["b1"]},
    }
    _install_client(
        [
            _response_with_json(bad),
            _response_with_json(bad),
        ]
    )
    with pytest.raises(GenerateError, match="schema"):
        generate(_build_brief_request())


def test_generate_missing_api_key_raises(temp_db: None, no_credentials: None) -> None:
    """No key stored → immediate GenerateError with the spec'd message."""
    _install_client([_response_with_json(_valid_case_brief_json())])

    with pytest.raises(GenerateError, match="No Anthropic API key stored"):
        generate(_build_brief_request())


def test_generate_emits_cost_event_with_real_tokens(
    temp_db: None, fake_credentials: None
) -> None:
    """Tokens 1200/450 → cost = (1200*15 + 450*75) / 1e6 = 0.05175 (Opus 4.7)."""
    _install_client(
        [_response_with_json(_valid_case_brief_json(), input_tokens=1200, output_tokens=450)]
    )

    result = generate(_build_brief_request())
    expected = Decimal("0.05175")
    # Round to 6 decimals to sidestep any trailing-zero quantization noise.
    assert result.artifact.cost_usd.quantize(Decimal("0.000001")) == expected

    with Session(db.get_engine()) as s:
        events = s.exec(select(CostEvent)).all()
        assert len(events) == 1
        ev = events[0]
        assert ev.total_cost_usd.quantize(Decimal("0.000001")) == expected
        assert ev.input_tokens == 1200
        assert ev.output_tokens == 450


def test_generate_uses_model_override_from_request(
    temp_db: None, fake_credentials: None
) -> None:
    """`model_override` is passed to the SDK and governs pricing."""
    client = _install_client(
        [_response_with_json(_valid_case_brief_json(), input_tokens=1000, output_tokens=200)]
    )

    base = _build_brief_request()
    req = GenerateRequest(
        template_name=base.template_name,
        inputs=base.inputs,
        artifact_type=base.artifact_type,
        corpus_id=base.corpus_id,
        model_override="claude-haiku-4-5",
    )
    result = generate(req)

    assert result.artifact.llm_model == "claude-haiku-4-5"
    # Client saw the override.
    assert client.messages.calls[0]["model"] == "claude-haiku-4-5"

    # Haiku pricing: input 1.00 / output 5.00 per mtok
    # 1000 * 1 / 1e6 + 200 * 5 / 1e6 = 0.001 + 0.001 = 0.002
    assert result.artifact.cost_usd.quantize(Decimal("0.000001")) == Decimal("0.002000")


def test_generate_uses_model_from_config_when_no_override(
    temp_db: None, fake_credentials: None
) -> None:
    """When an override is absent and the template has no defaults, fall back
    to config/models.toml. The real `case_brief` template *does* carry a
    default, but it matches the config entry (Opus 4.7), so we verify both
    paths converge on the correct model.

    To exercise the config-fallback path on its own, we drop the template's
    `model_defaults.model` in-memory and confirm resolution still lands on the
    config value.
    """
    from primitives import prompt_loader

    original_load = prompt_loader.load_template

    def patched_load(name, *, prompts_dir=None):
        t = original_load(name, prompts_dir=prompts_dir)
        # Build a copy with model stripped from defaults.
        new_defaults = dict(t.model_defaults)
        new_defaults.pop("model", None)
        return prompt_loader.PromptTemplate(
            name=t.name,
            version=t.version,
            description=t.description,
            inputs=t.inputs,
            output_schema_path=t.output_schema_path,
            model_defaults=new_defaults,
            body=t.body,
            source_path=t.source_path,
        )

    # Patch both the module-scoped symbol generate.py imported and the original.
    import primitives.generate as _gm

    _gm.load_template = patched_load  # type: ignore[assignment]
    try:
        client = _install_client([_response_with_json(_valid_case_brief_json())])
        result = generate(_build_brief_request())
        assert result.artifact.llm_model == "claude-opus-4-7"  # via config
        assert client.messages.calls[0]["model"] == "claude-opus-4-7"
    finally:
        _gm.load_template = prompt_loader.load_template  # type: ignore[assignment]


def test_generate_anthropic_error_raises_generateerror(
    temp_db: None, fake_credentials: None
) -> None:
    """Network / API exceptions are wrapped and DO NOT leak the key."""
    import anthropic

    # APIError's base constructor wants (message, request, body) — simplest is
    # to subclass with a message.
    class FakeAPIError(anthropic.APIError):
        def __init__(self, message: str):
            self.message = message
            super().__init__(message, request=None, body=None)  # type: ignore[arg-type]

    _install_client([FakeAPIError("rate limited")])

    with pytest.raises(GenerateError) as exc:
        generate(_build_brief_request())

    msg = str(exc.value)
    assert "Anthropic API call failed" in msg
    # Must not contain the key.
    assert "sk-ant-test-key" not in msg
