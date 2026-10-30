"""Interfaces simuladas para acciones del sistema en Fase 1 de Capri."""
from __future__ import annotations

from typing import Any, Protocol, TypedDict


class ActionResult(TypedDict, total=False):
    """Resultado común de una acción sin efectos sobre el sistema operativo."""

    ok: bool
    simulated: bool
    operation: str
    action: str
    target: str
    text: str
    message: str
    intent_type: str


class SystemActionsProtocol(Protocol):
    """Contrato que implementará la integración real durante Fase 2."""

    async def show_critical_notification(self, text: str) -> ActionResult:
        """Mostrar una notificación crítica al usuario."""

    async def control_window(self, action: str, target: str) -> ActionResult:
        """Controlar una ventana del sistema."""

    async def control_spotify(self, action: str) -> ActionResult:
        """Controlar Spotify."""

    async def execute_intent(self, intent: dict[str, Any]) -> dict[str, Any]:
        """Ejecutar una intención ya interpretada."""


# Alias descriptivo para consumidores que prefieren el nombre corto del contrato.
SystemActions = SystemActionsProtocol


def _text_value(value: Any, field: str) -> str:
    """Validar y recortar un argumento textual de acción."""
    if not isinstance(value, str):
        raise ValueError(f"{field} debe ser texto")
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field} no puede estar vacío")
    return clean


async def show_critical_notification(text: str) -> ActionResult:
    """Devolver una notificación simulada sin invocar APIs del sistema operativo."""
    # TODO(fase-2): conectar con el sistema de notificaciones de la laptop.
    clean_text = _text_value(text, "text")
    return {
        "ok": True,
        "simulated": True,
        "operation": "show_critical_notification",
        "text": clean_text,
        "message": "Acción simulada; no se modificó el sistema operativo.",
    }


async def control_window(action: str, target: str) -> ActionResult:
    """Devolver el control de ventana simulado sin usar DWM, X11 ni Win32."""
    # TODO(fase-2): integrar el controlador multiplataforma de ventanas.
    clean_action = _text_value(action, "action")
    clean_target = _text_value(target, "target")
    return {
        "ok": True,
        "simulated": True,
        "operation": "control_window",
        "action": clean_action,
        "target": clean_target,
        "message": "Acción simulada; no se modificó el sistema operativo.",
    }


async def control_spotify(action: str) -> ActionResult:
    """Devolver el control de Spotify simulado sin conectarse a Spotify."""
    # TODO(fase-2): integrar la API o automatización local de Spotify.
    clean_action = _text_value(action, "action")
    return {
        "ok": True,
        "simulated": True,
        "operation": "control_spotify",
        "action": clean_action,
        "message": "Acción simulada; no se modificó el sistema operativo.",
    }


class SystemActionsStub:
    """Implementación asíncrona simulada de todas las acciones de Capri."""

    async def show_critical_notification(self, text: str) -> ActionResult:
        """Delegar a la interfaz de notificación simulada."""
        return await show_critical_notification(text)

    async def control_window(self, action: str, target: str) -> ActionResult:
        """Delegar al controlador de ventanas simulado."""
        return await control_window(action, target)

    async def control_spotify(self, action: str) -> ActionResult:
        """Delegar al controlador de Spotify simulado."""
        return await control_spotify(action)

    async def execute_intent(self, intent: dict[str, Any]) -> dict[str, Any]:
        """Resolver una intención conocida sin ejecutar acciones externas."""
        # TODO(fase-2): sustituir el despacho simulado por adaptadores del sistema.
        if not isinstance(intent, dict):
            raise TypeError("intent debe ser un diccionario")

        intent_type = (
            intent.get("type")
            or intent.get("operation")
            or intent.get("name")
            or intent.get("action")
        )
        if intent_type in {"show_critical_notification", "critical_notification", "notification"}:
            text = intent.get("text", intent.get("message"))
            return await self.show_critical_notification(text)

        if intent_type in {"control_window", "window"}:
            action = intent.get("window_action", intent.get("command", "inspect"))
            target = intent.get("target", intent.get("window"))
            return await self.control_window(action, target)

        if intent_type in {"control_spotify", "spotify"}:
            action = intent.get("spotify_action", intent.get("command", "inspect"))
            return await self.control_spotify(action)

        # Las intenciones desconocidas no se ejecutan y no se devuelve su contenido,
        # lo que evita reflejar accidentalmente valores sensibles en una respuesta.
        return {
            "ok": False,
            "simulated": True,
            "operation": "unknown_intent",
            "intent_type": str(intent_type or "unknown"),
            "message": "Intención no reconocida; no se ejecutó ninguna acción.",
        }


__all__ = [
    "ActionResult",
    "SystemActions",
    "SystemActionsProtocol",
    "SystemActionsStub",
    "control_spotify",
    "control_window",
    "show_critical_notification",
]
