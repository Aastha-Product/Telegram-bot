import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google.genai import errors

import config
import gemini_client

SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "number"}, "reason": {"type": "string"}},
    "required": ["score", "reason"],
}
GOOD = json.dumps({"score": 8, "reason": "concrete incident plus a claim"})


def _api_error(cls: type, code: int, status: str, headers: dict | None = None) -> errors.APIError:
    response = httpx.Response(code, headers=headers or {})
    # Real SDK errors echo the server's message; make sure ours never forwards it.
    body = {"error": {"code": code, "status": status, "message": f"detail {config.settings.gemini_api_key}"}}
    return cls(code, body, response)


class FakeModels:
    """Stands in for client.aio.models; replays scripted replies (text) or raises (exceptions)."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, *, model: str, contents: Any, config: Any) -> SimpleNamespace:
        self.calls.append({"model": model, "contents": contents, "config": config})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=item)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(gemini_client.asyncio, "sleep", fake_sleep)
    return recorded


def _install(monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> FakeModels:
    models = FakeModels(script)
    monkeypatch.setattr(gemini_client, "_get_client", lambda: SimpleNamespace(aio=SimpleNamespace(models=models)))
    return models


def _json(prompt: str = "score this note") -> dict:
    return asyncio.run(gemini_client.generate_json(prompt, SCHEMA, "gemini-3.5-flash-lite"))


# --- generate_json -------------------------------------------------------------


def test_success_returns_parsed_dict_and_requests_json(monkeypatch, sleeps) -> None:
    models = _install(monkeypatch, [GOOD])
    assert _json() == {"score": 8, "reason": "concrete incident plus a claim"}
    [call] = models.calls
    assert call["model"] == "gemini-3.5-flash-lite"
    assert call["config"].response_mime_type == "application/json"
    assert call["config"].response_json_schema == SCHEMA
    assert sleeps == []


def test_server_error_retried_then_succeeds(monkeypatch, sleeps) -> None:
    models = _install(monkeypatch, [_api_error(errors.ServerError, 503, "UNAVAILABLE"), GOOD])
    assert _json()["score"] == 8
    assert len(models.calls) == 2
    assert sleeps == [2.0]


def test_timeout_retried_then_succeeds(monkeypatch, sleeps) -> None:
    _install(monkeypatch, [httpx.ReadTimeout("slow"), httpx.ConnectError("down"), GOOD])
    assert _json()["score"] == 8
    assert sleeps == [2.0, 4.0]


def test_rate_limit_honours_retry_after(monkeypatch, sleeps) -> None:
    _install(monkeypatch, [_api_error(errors.ClientError, 429, "RESOURCE_EXHAUSTED", {"retry-after": "7"}), GOOD])
    assert _json()["score"] == 8
    assert sleeps == [7.0]


def test_retry_after_is_capped(monkeypatch, sleeps) -> None:
    _install(monkeypatch, [_api_error(errors.ClientError, 429, "RESOURCE_EXHAUSTED", {"retry-after": "3600"}), GOOD])
    _json()
    assert sleeps == [gemini_client.MAX_BACKOFF_SECONDS]


def test_hard_fail_after_max_retries(monkeypatch, sleeps) -> None:
    models = _install(monkeypatch, [_api_error(errors.ServerError, 500, "INTERNAL")] * 10)
    with pytest.raises(gemini_client.GeminiError, match="after 3 retries"):
        _json()
    assert len(models.calls) == gemini_client.MAX_RETRIES + 1
    assert sleeps == [2.0, 4.0, 8.0]


@pytest.mark.parametrize(("code", "status"), [(400, "INVALID_ARGUMENT"), (401, "UNAUTHENTICATED"),
                                              (403, "PERMISSION_DENIED"), (404, "NOT_FOUND")])
def test_client_errors_are_not_retried(monkeypatch, sleeps, code: int, status: str) -> None:
    models = _install(monkeypatch, [_api_error(errors.ClientError, code, status), GOOD])
    with pytest.raises(gemini_client.GeminiError, match=str(code)):
        _json()
    assert len(models.calls) == 1 and sleeps == []


def test_error_messages_and_logs_never_contain_api_key(monkeypatch, sleeps, caplog) -> None:
    _install(monkeypatch, [_api_error(errors.ServerError, 500, "INTERNAL")] * 10)
    with pytest.raises(gemini_client.GeminiError) as exc:
        _json()
    key = config.settings.gemini_api_key
    assert key not in str(exc.value)
    assert exc.value.__cause__ is None  # the raw SDK error (which echoes server text) is not chained
    assert key not in caplog.text


def test_invalid_json_repaired_once(monkeypatch, sleeps) -> None:
    models = _install(monkeypatch, ["Sure! Here's the score: 8", GOOD])
    assert _json()["score"] == 8
    assert len(models.calls) == 2
    repair_prompt = models.calls[1]["contents"]
    assert "Previous reply:" in repair_prompt
    assert "Sure! Here's the score: 8" in repair_prompt
    assert '"required"' in repair_prompt  # the schema is included
    assert models.calls[1]["config"].response_mime_type == "application/json"


@pytest.mark.parametrize("bad", [json.dumps({"score": 8}), json.dumps([1, 2]), "", None])
def test_missing_keys_non_object_or_empty_trigger_repair(monkeypatch, sleeps, bad) -> None:
    models = _install(monkeypatch, [bad, GOOD])
    assert _json()["reason"]
    assert len(models.calls) == 2


def test_invalid_json_after_repair_is_a_hard_fail(monkeypatch, sleeps) -> None:
    models = _install(monkeypatch, ["not json", "still not json", GOOD])
    with pytest.raises(gemini_client.GeminiError, match="invalid JSON"):
        _json()
    assert len(models.calls) == 2  # exactly one repair, never a loop


def test_repair_call_is_also_retried(monkeypatch, sleeps) -> None:
    _install(monkeypatch, ["not json", _api_error(errors.ServerError, 503, "UNAVAILABLE"), GOOD])
    assert _json()["score"] == 8
    assert sleeps == [2.0]


# --- transcribe_audio ----------------------------------------------------------


def _transcribe(audio: bytes = b"OggS...") -> str:
    return asyncio.run(gemini_client.transcribe_audio(audio, "audio/ogg", "gemini-3.5-flash"))


def test_transcribe_sends_prompt_and_audio(monkeypatch, sleeps) -> None:
    models = _install(monkeypatch, ["  batch fourteen came back with a pH drift  "])
    assert _transcribe(b"OggS-audio") == "batch fourteen came back with a pH drift"
    [call] = models.calls
    prompt, part = call["contents"]
    assert "verbatim" in prompt.lower()
    assert part.inline_data.data == b"OggS-audio"
    assert part.inline_data.mime_type == "audio/ogg"


@pytest.mark.parametrize("reply", ["", "   ", None])
def test_empty_transcript_is_an_error(monkeypatch, sleeps, reply) -> None:
    _install(monkeypatch, [reply])
    with pytest.raises(gemini_client.GeminiError, match="empty"):
        _transcribe()


def test_no_audio_fails_without_calling_model(monkeypatch, sleeps) -> None:
    models = _install(monkeypatch, ["unused"])
    with pytest.raises(gemini_client.GeminiError):
        _transcribe(b"")
    assert models.calls == []


def test_transcribe_retries_server_errors(monkeypatch, sleeps) -> None:
    _install(monkeypatch, [_api_error(errors.ServerError, 503, "UNAVAILABLE"), "a transcript"])
    assert _transcribe() == "a transcript"


# --- prompts -------------------------------------------------------------------


def test_render_only_touches_named_placeholders() -> None:
    assert gemini_client.render('{"a": 1} {x} {y}', x="X") == '{"a": 1} X {y}'


def test_prompt_files_exist() -> None:
    for name in ("repair", "transcribe"):
        assert gemini_client.load_prompt(name).strip()
    assert "{schema}" in gemini_client.load_prompt("repair")
    assert "{previous}" in gemini_client.load_prompt("repair")
