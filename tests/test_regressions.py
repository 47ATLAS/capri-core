"""Casos de seguridad y temporización detectados durante la integración."""
import asyncio
import json
import os
import subprocess
from datetime import datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from capri_core.memory.mem0_client import LocalMemory, Mem0Memory
from capri_core.mcp_tools.recursive_edit import RecursiveEditor
from capri_core.psyche.prompt_builder import WPP_BLOCK
from capri_core.telemetry.fuzzy_logic import FuzzyEngine, TelemetryPayload


@pytest.mark.asyncio
async def test_sleep_interval_ends_at_six_even_if_next_tick_is_seven():
    engine = FuzzyEngine(clock=lambda: datetime(2026, 1, 1, 0))
    await engine.ingest(TelemetryPayload(charging=True, heart_rate=50))
    result = await engine.evaluate(datetime(2026, 1, 1, 7))
    assert not result["timer_paused"]
    assert result["inactivity_hours"] == pytest.approx(1)


@pytest.mark.asyncio
async def test_midnight_and_telemetry_expiry_integrated_without_ticks():
    engine = FuzzyEngine(clock=lambda: datetime(2026, 1, 1, 23))
    await engine.ingest(TelemetryPayload(charging=True, heart_rate=50))
    result = await engine.evaluate(datetime(2026, 1, 2, 7))
    # Datos de 23:00 válidos hasta 05:00: sueño 00:00..05:00, cinco de ocho horas.
    assert result["inactivity_hours"] == pytest.approx(3)


@pytest.mark.asyncio
async def test_new_sample_does_not_reinterpret_previous_interval():
    now = datetime(2026, 1, 1, 0)
    engine = FuzzyEngine(clock=lambda: now)
    await engine.ingest(TelemetryPayload(charging=False, heart_rate=50))
    now += timedelta(hours=2)
    result = await engine.ingest(TelemetryPayload(charging=True, heart_rate=50))
    assert result["inactivity_hours"] == pytest.approx(2)
    assert result["timer_paused"]


@pytest.mark.asyncio
async def test_heart_rate_boundary_and_fatigue_exact_three_hours():
    engine = FuzzyEngine(clock=lambda: datetime(2026, 1, 1, 1))
    result = await engine.ingest(TelemetryPayload(charging=True, heart_rate=60,
                                                active_app="documento.pdf", active_app_hours=3))
    assert result["sleep_probability"] == 0 and not result["events"]


@pytest.mark.parametrize("payload", [{"battery_percent": True}, {"charging": "true"}, {"heart_rate": 50.5}])
def test_telemetry_rejects_coerced_types(payload):
    with pytest.raises(ValidationError):
        TelemetryPayload.model_validate(payload)


@pytest.mark.asyncio
async def test_concurrent_memory_conflicts_are_serialized_and_history_is_linear(tmp_path):
    memory = LocalMemory(tmp_path / "memory.sqlite3")
    await memory.initialize()
    await asyncio.gather(*(memory.add(f"hecho {i}", conflict_key="same") for i in range(8)))
    records = await memory.list_all()
    assert len(records) == 1
    versions = records[0]["metadata"]["versions"]
    assert len(versions) == 7
    assert all("versions" not in item["metadata"] for item in versions)
    await memory.close()


@pytest.mark.asyncio
async def test_mem0_next_page_cannot_exfiltrate_authentication():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"results": [], "next": "https://other.invalid/steal"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        memory = Mem0Memory("test-key", "https://mem0.invalid", "owner", client=client)
        with pytest.raises(ValueError, match="fuera"):
            await memory.list_all()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_mem0_repeated_cursor_fails_instead_of_silent_partial_backup():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
        200, json={"results": [], "next": "https://mem0.invalid/v1/memories/?page=2"}))) as client:
        memory = Mem0Memory("test-key", "https://mem0.invalid", "owner", client=client)
        with pytest.raises(RuntimeError, match="incompleto"):
            await memory.list_all()


def make_editor(tmp_path):
    repository = tmp_path / "repo"
    psyche = repository / "capri_core" / "psyche"
    psyche.mkdir(parents=True)
    core = psyche / "core_directives.json"
    state = psyche / "psyche_state.json"
    core.write_text(json.dumps({"wpp": WPP_BLOCK}), encoding="utf-8")
    state.write_text(json.dumps({"mood": "neutral", "trust": 0.5, "sarcasm_level": 0.8,
                                "vad": {"valence": 0.0, "arousal": 0.0, "dominance": 0.0}}), encoding="utf-8")
    def git(*args):
        return subprocess.run(["git", "-C", str(repository), *args], capture_output=True,
                              text=True, check=True)
    git("init", "-q")
    git("config", "user.name", "Capri Offline Test")
    git("config", "user.email", "offline@example.invalid")
    git("add", ".")
    git("commit", "-q", "-m", "baseline")
    return RecursiveEditor(psyche, repository, tmp_path / "audit.log"), state, core, git


@pytest.mark.asyncio
async def test_snapshot_commit_exists_even_when_state_was_unchanged(tmp_path):
    editor, state, core, git = make_editor(tmp_path)
    before = git("rev-parse", "HEAD").stdout.strip()
    result = await editor.overwrite_psyche_trait("mood", "alerta", "evento")
    assert result["previous_commit"] != before
    assert git("rev-list", "--count", "HEAD").stdout.strip() == "2"
    assert json.loads(git("show", "HEAD:capri_core/psyche/psyche_state.json").stdout)["mood"] == "neutral"
    assert json.loads(state.read_text(encoding="utf-8"))["mood"] == "alerta"


@pytest.mark.asyncio
async def test_audit_failure_aborts_before_mutation(tmp_path, monkeypatch):
    editor, state, core, git = make_editor(tmp_path)
    original = state.read_bytes()
    def fail(*args):
        raise PermissionError("simulated")
    monkeypatch.setattr(editor, "_append_audit", fail)
    with pytest.raises(PermissionError):
        await editor.overwrite_psyche_trait("mood", "alerta", "evento")
    assert state.read_bytes() == original


@pytest.mark.asyncio
async def test_precommit_hook_failure_aborts_instead_of_bypassing_hook(tmp_path):
    editor, state, core, git = make_editor(tmp_path)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    git("config", "core.hooksPath", str(hooks))
    original = state.read_bytes()
    with pytest.raises(RuntimeError):
        await editor.overwrite_psyche_trait("mood", "alerta", "evento")
    assert state.read_bytes() == original


@pytest.mark.asyncio
async def test_core_hardlink_alias_is_never_modified(tmp_path):
    editor, state, core, git = make_editor(tmp_path)
    original = core.read_bytes()
    state.unlink()
    os.link(core, state)
    with pytest.raises(ValueError):
        await editor.overwrite_psyche_trait("mood", "alerta", "evento")
    assert core.read_bytes() == original


@pytest.mark.parametrize("amount", [1.23, "1.000000000000000000000000000001", "1e-1000"])
def test_finance_rejects_float_or_hidden_precision(amount):
    from capri_core.mcp_tools.finance_manager import ExpenseInput
    with pytest.raises(ValidationError):
        ExpenseInput(amount=amount, concept="prueba")
