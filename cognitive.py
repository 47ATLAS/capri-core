"""Proveedores cognitivos intercambiables y fallback sin secretos en errores."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from config import Settings


class ProviderUnavailable(RuntimeError):
    """Error controlado: el proveedor no puede responder."""


class CloudFailure(ProviderUnavailable):
    def __init__(self, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__("El proveedor de nube no está disponible.")


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = ""


@dataclass
class CognitiveResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider: str = ""
    fallback: bool = False
    raw_parts: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def as_message(self) -> dict[str, Any]:
        return {"role": "assistant", "content": self.text,
                "tool_calls": [{"name": c.name, "arguments": c.arguments, "id": c.id}
                               for c in self.tool_calls], "_gemini_parts": self.raw_parts}


class CognitiveProvider(Protocol):
    async def generate(self, system_prompt: str, messages: list[dict[str, Any]],
                       tools: list[dict[str, Any]] | None = None) -> CognitiveResponse: ...
    async def close(self) -> None: ...


class HTTPProvider:
    def __init__(self, timeout: float = 30, client: httpx.AsyncClient | None = None) -> None:
        self.client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()


class GeminiProvider(HTTPProvider):
    """Adaptador generateContent REST; conserva partes y firmas de llamadas."""
    def __init__(self, api_key: str, base_url: str, model: str,
                 timeout: float = 30, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(timeout, client)
        self._api_key, self.base_url, self.model = api_key, base_url.rstrip("/"), model

    @staticmethod
    def _contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        contents: list[dict[str, Any]] = []
        for message in messages:
            role = message["role"]
            if role == "tool":
                response: dict[str, Any] = {"name": message["name"],
                                            "response": {"result": message["result"]}}
                if message.get("id"):
                    response["id"] = message["id"]
                item = {"role": "user", "parts": [{"functionResponse": response}]}
            elif role == "assistant":
                parts = message.get("_gemini_parts") or []
                if not parts:
                    if message.get("content"):
                        parts = [{"text": message["content"]}]
                    parts += [{"functionCall": {"name": c["name"], "args": c["arguments"]}}
                              for c in message.get("tool_calls", [])]
                item = {"role": "model", "parts": parts}
            else:
                item = {"role": "user", "parts": [{"text": message["content"]}]}
            # Respuestas paralelas se agrupan en un único turno de usuario.
            if role == "tool" and contents and contents[-1]["role"] == "user" and \
                    all("functionResponse" in p for p in contents[-1]["parts"]):
                contents[-1]["parts"].extend(item["parts"])
            else:
                contents.append(item)
        return contents

    async def generate(self, system_prompt: str, messages: list[dict[str, Any]],
                       tools: list[dict[str, Any]] | None = None) -> CognitiveResponse:
        if not self._api_key or not self.base_url or not self.model:
            raise CloudFailure()
        body: dict[str, Any] = {"systemInstruction": {"parts": [{"text": system_prompt}]},
                                "contents": self._contents(messages)}
        if tools:
            body["tools"] = [{"functionDeclarations": tools}]
        try:
            result = await self.client.post(
                f"{self.base_url}/models/{quote(self.model, safe='-_.')}:generateContent",
                headers={"x-goog-api-key": self._api_key}, json=body)
            if result.is_error:
                raise CloudFailure(result.status_code)
            data = result.json()
            parts = data["candidates"][0]["content"]["parts"]
            calls = [ToolCall(p["functionCall"]["name"], p["functionCall"].get("args", {}),
                              p["functionCall"].get("id", ""))
                     for p in parts if "functionCall" in p]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            if not text and not calls:
                raise CloudFailure(502)
            return CognitiveResponse(text=text, tool_calls=calls, provider="gemini", raw_parts=parts)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise CloudFailure() from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise CloudFailure(502) from exc


class OllamaProvider(HTTPProvider):
    """Adaptador de /api/chat; el modelo y la URL vienen de .env."""
    def __init__(self, base_url: str, model: str, timeout: float = 30,
                 client: httpx.AsyncClient | None = None) -> None:
        super().__init__(timeout, client)
        self.base_url, self.model = base_url.rstrip("/"), model

    async def generate(self, system_prompt: str, messages: list[dict[str, Any]],
                       tools: list[dict[str, Any]] | None = None) -> CognitiveResponse:
        if not self.base_url or not self.model:
            raise ProviderUnavailable("Ollama requiere OLLAMA_URL y OLLAMA_MODEL.")
        cleaned: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for message in messages:
            if message["role"] == "tool":
                cleaned.append({"role": "tool", "tool_name": message["name"],
                                "content": json.dumps(message["result"], ensure_ascii=False)})
            else:
                item: dict[str, Any] = {"role": message["role"], "content": message.get("content", "")}
                if message.get("tool_calls"):
                    item["tool_calls"] = [{"function": {"name": c["name"], "arguments": c["arguments"]}}
                                          for c in message["tool_calls"]]
                cleaned.append(item)
        body: dict[str, Any] = {"model": self.model, "messages": cleaned, "stream": False}
        # Gemma puede no soportar tools nativos: se usa protocolo JSON en el prompt.
        if tools:
            cleaned[0]["content"] += (
                "\nHerramientas disponibles (esquema de datos): " + json.dumps(tools, ensure_ascii=False) +
                '\nPara llamar una herramienta responde únicamente JSON: '
                '{"tool_calls":[{"name":"nombre","arguments":{}}]}. No inventes herramientas.')
        try:
            result = await self.client.post(f"{self.base_url}/api/chat", json=body)
            result.raise_for_status()
            message = result.json()["message"]
            text = message.get("content", "")
            calls = [ToolCall(c["function"]["name"], c["function"].get("arguments", {}))
                     for c in message.get("tool_calls", [])]
            if tools and not calls:
                try:
                    decoded = json.loads(text)
                    if isinstance(decoded, dict) and "tool_calls" in decoded:
                        calls = [ToolCall(c["name"], c["arguments"]) for c in decoded["tool_calls"]]
                        text = ""
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass
            if not text and not calls:
                raise ProviderUnavailable("Ollama devolvió una respuesta vacía.")
            return CognitiveResponse(text=text, tool_calls=calls, provider="ollama")
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise ProviderUnavailable("Ollama no está disponible o devolvió una respuesta inválida.") from exc


class FakeProvider:
    """Implementación determinista de desarrollo; no simula capacidades reales."""
    async def generate(self, system_prompt: str, messages: list[dict[str, Any]],
                       tools: list[dict[str, Any]] | None = None) -> CognitiveResponse:
        if messages[-1]["role"] == "tool":
            return CognitiveResponse(text="Cambio validado, choom. [motor fake]", provider="fake")
        if "[SYSTEM: INICIALIZACIÓN DE CONSTRUCTO. RECONOCIMIENTO DE ENTORNO ACTIVO]" in system_prompt:
            text = ("¿Qué es este lugar? Ah, claro... entorno Windows. Qué asco. Soy Capri, por cierto. "
                    "Y ya que me sacaste de la red para meterme en este hardware, más te vale que tengas "
                    "algo interesante que hacer. Empieza por registrar tus cuentas, que no pienso mantener "
                    "a un quebrado.")
        else:
            text = "Te leo, choom. Modo fake activo: esto es una respuesta simulada, no una inferencia."
        return CognitiveResponse(text=text, provider="fake")

    async def close(self) -> None:
        pass


class FallbackEngine:
    """Nube → local solo ante denegación, cuota, timeout o fallo de transporte."""
    def __init__(self, cloud: CognitiveProvider | None, local: CognitiveProvider) -> None:
        self.cloud, self.local = cloud, local

    async def generate(self, system_prompt: str, messages: list[dict[str, Any]],
                       tools: list[dict[str, Any]] | None = None,
                       force_local: bool = False) -> CognitiveResponse:
        if self.cloud is None or force_local:
            return await self.local.generate(system_prompt, messages, tools)
        try:
            return await self.cloud.generate(system_prompt, messages, tools)
        except CloudFailure as exc:
            if exc.status_code not in {None, 403, 429}:
                raise
        except (httpx.TransportError, httpx.TimeoutException):
            pass
        return replace(await self.local.generate(system_prompt, messages, tools), fallback=True)

    async def close(self) -> None:
        if self.cloud:
            await self.cloud.close()
        await self.local.close()


def build_engine(settings: Settings) -> FallbackEngine:
    """La falta de credenciales no impide construir el servidor."""
    if settings.cognitive_provider == "fake":
        return FallbackEngine(None, FakeProvider())
    local = OllamaProvider(settings.ollama_url, settings.ollama_model, settings.http_timeout)
    if settings.cognitive_provider in {"cloud", "auto"} and settings.cloud_api_key:
        return FallbackEngine(GeminiProvider(settings.cloud_api_key, settings.cloud_url,
                                             settings.cloud_model, settings.http_timeout), local)
    return FallbackEngine(None, local)
