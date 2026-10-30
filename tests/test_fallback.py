import httpx
import pytest

from cognitive import (CloudFailure, CognitiveResponse, FakeProvider, FallbackEngine,
                       GeminiProvider, OllamaProvider, ProviderUnavailable, ToolCall, build_engine)
from config import Settings


class RecordingLocal(FakeProvider):
    def __init__(self):
        self.calls = 0
    async def generate(self, system_prompt, messages, tools=None):
        self.calls += 1
        return CognitiveResponse(text="local", provider="ollama")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429])
async def test_cloud_status_fallback(status):
    local = RecordingLocal()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json={"error": "dummy"}))) as client:
        engine = FallbackEngine(GeminiProvider("test-key", "https://mock.invalid", "test-model", client=client), local)
        result = await engine.generate("system", [{"role": "user", "content": "hola"}])
        assert result.provider == "ollama" and result.fallback
        assert local.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout])
async def test_cloud_transport_fallback(error):
    def failing(request):
        raise error("simulated", request=request)
    local = RecordingLocal()
    async with httpx.AsyncClient(transport=httpx.MockTransport(failing)) as client:
        result = await FallbackEngine(GeminiProvider("test", "https://mock.invalid", "model", client=client), local).generate(
            "sys", [{"role": "user", "content": "hola"}])
        assert result.fallback and local.calls == 1


@pytest.mark.asyncio
async def test_other_http_error_does_not_hide_failure():
    local = RecordingLocal()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
        with pytest.raises(CloudFailure):
            await FallbackEngine(GeminiProvider("test", "https://mock.invalid", "model", client=client), local).generate(
                "sys", [{"role": "user", "content": "hola"}])
    assert local.calls == 0


@pytest.mark.asyncio
async def test_success_cloud_and_request_schema():
    def success(request):
        import json
        payload = json.loads(request.content)
        assert payload["systemInstruction"]["parts"][0]["text"] == "sys"
        assert request.headers["x-goog-api-key"] == "test"
        assert "test" not in str(request.url)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "nube"}]}}]})
    local = RecordingLocal()
    async with httpx.AsyncClient(transport=httpx.MockTransport(success)) as client:
        result = await FallbackEngine(GeminiProvider("test", "https://mock.invalid", "model", client=client), local).generate(
            "sys", [{"role": "user", "content": "hola"}])
    assert result.text == "nube" and not result.fallback and local.calls == 0


@pytest.mark.asyncio
async def test_ollama_model_configurable_and_json_tools():
    def success(request):
        import json
        body = json.loads(request.content)
        assert body["model"] == "gemma4:e4b"
        assert body["stream"] is False
        assert "Herramientas disponibles" in body["messages"][0]["content"]
        return httpx.Response(200, json={"message": {"content": '{"tool_calls":[{"name":"query_recent_expenses","arguments":{}}]}'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(success)) as client:
        result = await OllamaProvider("http://mock.invalid", "gemma4:e4b", client=client).generate(
            "sys", [{"role": "user", "content": "hola"}], [{"name": "query_recent_expenses"}])
    assert result.tool_calls[0].name == "query_recent_expenses" and result.text == ""


@pytest.mark.asyncio
async def test_force_local_skips_cloud():
    class ForbiddenCloud(FakeProvider):
        async def generate(self, *args, **kwargs):
            raise AssertionError("Datos privados no deben llegar a nube")
    result = await FallbackEngine(ForbiddenCloud(), RecordingLocal()).generate(
        "sys", [{"role": "user", "content": "privado"}], force_local=True)
    assert result.provider == "ollama" and not result.fallback


@pytest.mark.asyncio
async def test_missing_third_party_keys_dev_is_offline():
    engine = build_engine(Settings(api_key="test"))
    result = await engine.generate("sys", [{"role": "user", "content": "hola"}])
    assert result.provider == "fake"
    await engine.close()


@pytest.mark.asyncio
async def test_local_failure_is_controlled():
    engine = build_engine(Settings(api_key="test", cognitive_provider="cloud"))
    with pytest.raises(ProviderUnavailable):
        await engine.generate("sys", [{"role": "user", "content": "hola"}])
    await engine.close()


def test_gemini_thought_signature_and_call_id_preserved():
    parts = [{"functionCall": {"name": "edit", "args": {}, "id": "call-1"}, "thoughtSignature": "signature"}]
    response = CognitiveResponse(tool_calls=[ToolCall("edit", {}, "call-1")], raw_parts=parts)
    contents = GeminiProvider._contents([response.as_message(), {"role": "tool", "name": "edit", "id": "call-1", "result": {"ok": True}}])
    assert contents[0]["parts"] == parts
    assert contents[1]["parts"][0]["functionResponse"]["id"] == "call-1"
