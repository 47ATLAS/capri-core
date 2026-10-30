"""Inferencia temporal de Capri: acumula inactividad, nunca ejecuta voz ni SO."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, time, timedelta
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt


class Location(str, Enum):
    """Etiquetas suministradas por Tasker, sin geocercas inventadas."""
    OTHER = "other"
    IPN = "ipn"
    SPORTS = "sports"


class TelemetryPayload(BaseModel):
    """Snapshot completo; los campos omitidos recuperan su valor predeterminado."""
    model_config = ConfigDict(extra="forbid")
    location: Location = Location.OTHER
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    battery_percent: StrictInt | None = Field(default=None, ge=0, le=100)
    charging: StrictBool = False
    heart_rate: StrictInt | None = Field(default=None, ge=20, le=250)
    active_app: str | None = Field(default=None, max_length=120)
    active_app_hours: float = Field(default=0.0, ge=0, le=168)
    activity: StrictBool = False


class FuzzyEngine:
    """Reloj inyectable y sueño con telemetría válida como máximo seis horas.

    Se integran intervalos contra la ventana [00:00,06:00), incluso si no hubo
    una evaluación exactamente en sus límites. Un snapshot nuevo no cambia
    retroactivamente la interpretación del intervalo anterior.
    """
    _SLEEP_PROBABILITY = 0.95
    _FATIGUE_TEXT = "<voice_note>Deja de compilar, tu cerebro ya está frito. Cierra eso.</voice_note>"

    def __init__(self, timezone_name: str = "America/Mexico_City", low_heart_rate: int = 60,
                 clock: Callable[[], datetime] | None = None, *,
                 sleep_telemetry_max_age_minutes: float = 360.0) -> None:
        self._timezone = ZoneInfo(timezone_name)
        if not 20 <= low_heart_rate <= 250 or sleep_telemetry_max_age_minutes < 0:
            raise ValueError("Parámetros temporales fuera de rango")
        self.timezone_name, self.low_heart_rate = timezone_name, low_heart_rate
        self.sleep_telemetry_max_age = timedelta(minutes=sleep_telemetry_max_age_minutes)
        self._clock = clock or (lambda: datetime.now(self._timezone))
        self._lock = asyncio.Lock()
        started = self._coerce_datetime(self._clock())
        self._last_observed_at = started
        self._last_evaluated_at = started
        self._effective_inactivity_seconds = 0.0
        self._last_payload: TelemetryPayload | None = None
        self._last_telemetry_at: datetime | None = None
        self._emitted_phases: set[int] = set()
        self._mental_app = ""
        self._mental_fatigue_emitted = False
        self._pending_fatigue_event = False

    async def ingest(self, payload: TelemetryPayload) -> dict[str, Any]:
        """Registrar un snapshot; solamente activity=True reinicia la inactividad."""
        if not isinstance(payload, TelemetryPayload):
            payload = TelemetryPayload.model_validate(payload)
        async with self._lock:
            now = self._monotonic_now(self._clock())
            self._advance_timer_locked(now)
            self._last_payload, self._last_telemetry_at = payload, now
            if payload.activity:
                self._effective_inactivity_seconds = 0.0
                self._emitted_phases.clear()
            self._update_mental_fatigue(payload)
            return self._evaluate_locked(now)

    async def evaluate(self, now: datetime | None = None) -> dict[str, Any]:
        """Evaluar sin esperar un nuevo webhook ni usar red."""
        async with self._lock:
            return self._evaluate_locked(self._monotonic_now(self._clock() if now is None else now))

    def _sleep_conditions(self) -> bool:
        payload = self._last_payload
        return bool(payload and not payload.activity and payload.charging and
                    payload.heart_rate is not None and payload.heart_rate < self.low_heart_rate)

    def _sleep_probability_at(self, now: datetime) -> float:
        if self._last_telemetry_at is None or not self._sleep_conditions():
            return 0.0
        if now - self._last_telemetry_at > self.sleep_telemetry_max_age or not 0 <= now.hour < 6:
            return 0.0
        return self._SLEEP_PROBABILITY

    def _sleep_seconds_between(self, start: datetime, end: datetime) -> float:
        """Sumar solo el solapamiento de sueño con una muestra no caducada."""
        if self._last_telemetry_at is None or not self._sleep_conditions():
            return 0.0
        start = max(start, self._last_telemetry_at)
        end = min(end, self._last_telemetry_at + self.sleep_telemetry_max_age)
        if end <= start:
            return 0.0
        day, result = start.date(), 0.0
        while day <= end.date():
            midnight = datetime.combine(day, time(0), tzinfo=self._timezone)
            six = datetime.combine(day, time(6), tzinfo=self._timezone)
            lower, upper = max(start, midnight), min(end, six)
            if upper > lower:
                result += (upper - lower).total_seconds()
            day += timedelta(days=1)
        return result

    def _advance_timer_locked(self, now: datetime) -> None:
        elapsed = max(0.0, (now - self._last_evaluated_at).total_seconds())
        self._effective_inactivity_seconds += max(
            0.0, elapsed - self._sleep_seconds_between(self._last_evaluated_at, now))
        self._last_evaluated_at = now

    def _evaluate_locked(self, now: datetime) -> dict[str, Any]:
        self._advance_timer_locked(now)
        sleep_probability = self._sleep_probability_at(now)
        hours = self._effective_inactivity_seconds / 3600.0
        first_threshold = 4.0 if self._last_payload and self._last_payload.location in {
            Location.IPN, Location.SPORTS} else 2.0
        phase = 3 if hours >= 12.0 else 2 if hours >= 5.0 else 1 if hours >= first_threshold else 0
        events: list[dict[str, Any]] = []
        if self._pending_fatigue_event:
            events.append({"phase": 0, "kind": "mental_fatigue", "text": self._FATIGUE_TEXT,
                           "requires_voice": True, "critical": False})
            self._pending_fatigue_event = False
        for next_phase in range(1, phase + 1):
            if next_phase not in self._emitted_phases:
                events.append(self._phase_event(next_phase))
                self._emitted_phases.add(next_phase)
        return {"phase": phase, "inactivity_hours": hours, "sleep_probability": sleep_probability,
                "timer_paused": sleep_probability > 0, "events": events}

    @staticmethod
    def _phase_event(phase: int) -> dict[str, Any]:
        texts = {1: "¿Te compilaron en modo estatua, choom? Muévete un poco.",
                 2: "Cinco horas de inactividad. Muévete ahora; esto ya requiere atención.",
                 3: "Doce horas o más de inactividad. Esto es crítico: revisa tu estado."}
        return {"phase": phase, "kind": "inactivity", "text": texts[phase],
                "requires_voice": phase == 2, "critical": phase == 3}

    def _update_mental_fatigue(self, payload: TelemetryPayload) -> None:
        app = payload.active_app.strip().casefold() if payload.active_app else ""
        if app != self._mental_app or payload.active_app_hours == 0:
            self._mental_fatigue_emitted = False
        self._mental_app = app
        if ("code::blocks" in app or "pdf" in app) and payload.active_app_hours > 3 and not self._mental_fatigue_emitted:
            self._mental_fatigue_emitted = True
            self._pending_fatigue_event = True

    def _monotonic_now(self, value: datetime) -> datetime:
        normalized = self._coerce_datetime(value)
        self._last_observed_at = max(normalized, self._last_observed_at)
        return self._last_observed_at

    def _coerce_datetime(self, value: datetime) -> datetime:
        return value.replace(tzinfo=self._timezone) if value.tzinfo is None else value.astimezone(self._timezone)


__all__ = ["FuzzyEngine", "Location", "TelemetryPayload"]
