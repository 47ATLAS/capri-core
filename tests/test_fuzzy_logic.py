"""Pruebas deterministas del motor de telemetría de Capri."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from capri_core.telemetry.fuzzy_logic import FuzzyEngine, Location, TelemetryPayload


class FrozenClock:
    """Reloj mutable para probar el motor sin esperar tiempo real."""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


def test_payload_defaults_and_validation() -> None:
    """El payload aplica defaults, rangos y prohibición de campos extra."""
    payload = TelemetryPayload()
    assert payload.location is Location.OTHER
    assert payload.charging is False
    assert payload.active_app_hours == 0

    with pytest.raises(ValidationError):
        TelemetryPayload(latitude=91)
    with pytest.raises(ValidationError):
        TelemetryPayload(extra_field="no permitido")
    with pytest.raises(ValidationError):
        TelemetryPayload(heart_rate=19)


@pytest.mark.asyncio
async def test_escalada_normal_y_dedupe_en_limites() -> None:
    """Las fases normal aparecen en 2, 5 y 12 horas, una sola vez."""
    clock = FrozenClock(datetime(2026, 1, 1, 8, 0))
    engine = FuzzyEngine(clock=clock)

    first = await engine.evaluate(clock.value + timedelta(hours=2))
    assert first["phase"] == 1
    assert [event["phase"] for event in first["events"]] == [1]

    duplicate = await engine.evaluate(clock.value + timedelta(hours=2))
    assert duplicate["events"] == []

    second = await engine.evaluate(clock.value + timedelta(hours=5))
    assert second["phase"] == 2
    assert [event["phase"] for event in second["events"]] == [2]
    assert second["events"][0]["requires_voice"] is True

    third = await engine.evaluate(clock.value + timedelta(hours=12))
    assert third["phase"] == 3
    assert [event["phase"] for event in third["events"]] == [3]
    assert third["events"][0]["critical"] is True
    assert third["events"][0]["requires_voice"] is False

    after = await engine.evaluate(clock.value + timedelta(hours=13))
    assert after["phase"] == 3
    assert after["events"] == []


@pytest.mark.asyncio
async def test_ubicacion_ipn_y_deportes_retrasan_primera_alerta() -> None:
    """IPN y deportes llevan la primera alerta al límite de cuatro horas."""
    clock = FrozenClock(datetime(2026, 1, 1, 8, 0))
    engine = FuzzyEngine(clock=clock)
    await engine.ingest(TelemetryPayload(location=Location.IPN))

    before = await engine.evaluate(clock.value + timedelta(hours=4) - timedelta(seconds=1))
    assert before["phase"] == 0
    assert before["events"] == []

    at_boundary = await engine.evaluate(clock.value + timedelta(hours=4))
    assert at_boundary["phase"] == 1
    assert at_boundary["events"][0]["phase"] == 1


@pytest.mark.asyncio
async def test_sueno_pausa_reanuda_y_no_se_infiere_sin_ritmo() -> None:
    """El sueño pausa sin perder tiempo y exige frecuencia cardíaca reciente."""
    clock = FrozenClock(datetime(2026, 1, 1, 0, 0))
    engine = FuzzyEngine(clock=clock, sleep_telemetry_max_age_minutes=360)

    sleeping = await engine.ingest(
        TelemetryPayload(charging=True, heart_rate=50)
    )
    assert sleeping["sleep_probability"] == 0.95
    assert sleeping["timer_paused"] is True

    during_sleep = await engine.evaluate(clock.value + timedelta(hours=5))
    assert during_sleep["timer_paused"] is True
    assert during_sleep["inactivity_hours"] == pytest.approx(0)

    awake = await engine.evaluate(clock.value + timedelta(hours=6))
    assert awake["timer_paused"] is False
    assert awake["sleep_probability"] == 0
    assert awake["inactivity_hours"] == pytest.approx(0)

    no_heart_rate = await engine.ingest(TelemetryPayload(charging=True))
    assert no_heart_rate["sleep_probability"] == 0
    assert no_heart_rate["timer_paused"] is False


@pytest.mark.asyncio
async def test_actividad_reinicia_sesion_y_conserva_alertas_previas_solo_hasta_actividad() -> None:
    """La actividad explícita reinicia inactividad y deduplicación de fase."""
    clock = FrozenClock(datetime(2026, 1, 1, 8, 0))
    engine = FuzzyEngine(clock=clock)
    clock.advance(hours=2)
    await engine.evaluate()

    reset = await engine.ingest(TelemetryPayload(activity=True))
    assert reset["inactivity_hours"] == pytest.approx(0)
    assert reset["phase"] == 0

    clock.advance(hours=2)
    next_alert = await engine.evaluate()
    assert next_alert["phase"] == 1
    assert [event["phase"] for event in next_alert["events"]] == [1]


@pytest.mark.asyncio
async def test_fatiga_de_codeblocks_es_literal_y_deduplicada() -> None:
    """La nota de voz de cansancio se emite una vez y se rearma en cero."""
    clock = FrozenClock(datetime(2026, 1, 1, 10, 0))
    engine = FuzzyEngine(clock=clock)
    literal = "<voice_note>Deja de compilar, tu cerebro ya está frito. Cierra eso.</voice_note>"

    first = await engine.ingest(
        TelemetryPayload(active_app="Code::Blocks", active_app_hours=3.1)
    )
    fatigue_events = [event for event in first["events"] if event["kind"] == "mental_fatigue"]
    assert [event["text"] for event in fatigue_events] == [literal]

    duplicate = await engine.ingest(
        TelemetryPayload(active_app="Code::Blocks", active_app_hours=4)
    )
    assert not any(event["kind"] == "mental_fatigue" for event in duplicate["events"])

    await engine.ingest(TelemetryPayload(active_app="Code::Blocks", active_app_hours=0))
    armed_again = await engine.ingest(
        TelemetryPayload(active_app="Code::Blocks", active_app_hours=3.5)
    )
    assert sum(event["kind"] == "mental_fatigue" for event in armed_again["events"]) == 1
