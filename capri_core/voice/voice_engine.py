"""Interceptor local de etiquetas de voz para Capri, simulado durante Fase 1."""
from __future__ import annotations

import re
from typing import Any, TypedDict


_TAG_PATTERN = re.compile(
    r"<(?P<tag>voice_note|humming)>(?P<content>.*?)</(?P=tag)>",
    flags=re.DOTALL,
)


class VoiceSegment(TypedDict):
    """Segmento de voz que habría sido enviado a un motor externo."""

    type: str
    text: str
    simulated: bool


class VoiceProcessResult(TypedDict):
    """Respuesta del procesamiento local de voz."""

    simulated: bool
    segments: list[VoiceSegment]


class VoiceEngine:
    """Interceptar marcas de voz sin realizar llamadas de red en Fase 1."""

    def __init__(self, enabled: bool = False, api_key: str = "") -> None:
        """Configurar el stub; `enabled` y la clave no activan integraciones todavía."""
        self.enabled = bool(enabled)
        # TODO(fase-2): usar la clave únicamente al integrar ElevenLabs de forma segura.
        # Se conserva de forma privada y nunca se incluye en respuestas ni mensajes.
        self._api_key = api_key

    async def process(self, text: str, force: bool = False) -> dict[str, Any]:
        """Extraer etiquetas de voz y devolver segmentos marcados como simulados."""
        # TODO(fase-2): enviar los segmentos al proveedor de voz cuando corresponda.
        if not isinstance(text, str):
            raise TypeError("text debe ser texto")

        segments: list[VoiceSegment] = []
        for match in _TAG_PATTERN.finditer(text):
            content = match.group("content").strip()
            if content:
                segments.append(
                    {
                        "type": match.group("tag"),
                        "text": content,
                        "simulated": True,
                    }
                )

        if force and not segments and text.strip():
            segments.append(
                {
                    "type": "voice_note",
                    "text": text.strip(),
                    "simulated": True,
                }
            )

        return {"simulated": True, "segments": segments}


__all__ = ["VoiceEngine", "VoiceProcessResult", "VoiceSegment"]
