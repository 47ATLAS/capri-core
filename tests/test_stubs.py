"""Pruebas de stubs Fase 1 sin acceso a red ni al sistema operativo."""
from __future__ import annotations

import pytest

from capri_core.mcp_tools.system_actions import (
    SystemActionsStub,
    control_spotify,
    control_window,
    show_critical_notification,
)
from capri_core.voice.voice_engine import VoiceEngine


@pytest.mark.asyncio
async def test_acciones_de_sistema_son_simuladas() -> None:
    """Las tres interfaces devuelven simulación y no requieren Windows ni Spotify."""
    notification = await show_critical_notification("  alerta  ")
    window = await control_window("focus", "Code::Blocks")
    spotify = await control_spotify("pause")

    assert notification["simulated"] is True
    assert notification["text"] == "alerta"
    assert window["simulated"] is True
    assert window["target"] == "Code::Blocks"
    assert spotify["simulated"] is True
    assert spotify["action"] == "pause"


@pytest.mark.asyncio
async def test_system_actions_stub_despacha_intencion_sin_os() -> None:
    """El stub despacha una intención conocida sin invocar procesos externos."""
    stub = SystemActionsStub()

    result = await stub.execute_intent(
        {"type": "control_window", "window_action": "focus", "target": "editor"}
    )
    unknown = await stub.execute_intent({"type": "not_implemented"})

    assert result["simulated"] is True
    assert result["operation"] == "control_window"
    assert unknown["simulated"] is True
    assert unknown["ok"] is False


@pytest.mark.asyncio
async def test_voice_engine_intercepta_voice_note_y_humming_sin_red() -> None:
    """Las etiquetas se convierten en segmentos simulados aunque enabled sea true."""
    engine = VoiceEngine(enabled=True, api_key="clave-no-debe-salir")

    result = await engine.process(
        "antes <voice_note>  habla ahora </voice_note> "
        "y <humming> melodía </humming> después"
    )

    assert result["simulated"] is True
    assert result["segments"] == [
        {"type": "voice_note", "text": "habla ahora", "simulated": True},
        {"type": "humming", "text": "melodía", "simulated": True},
    ]
    assert "clave-no-debe-salir" not in repr(result)


@pytest.mark.asyncio
async def test_voice_engine_force_crea_voice_note_simulada() -> None:
    """force permite producir un segmento local aun sin etiqueta explícita."""
    engine = VoiceEngine()

    result = await engine.process("  mensaje forzado  ", force=True)

    assert result["segments"] == [
        {"type": "voice_note", "text": "mensaje forzado", "simulated": True}
    ]
