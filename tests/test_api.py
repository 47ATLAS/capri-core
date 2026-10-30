from pathlib import Path
import shutil

import pytest
from fastapi.testclient import TestClient

from config import ROOT, Settings
from cognitive import CognitiveResponse, FakeProvider, FallbackEngine, ToolCall
from capri_core.main import create_app


class NoopEditor:
    def __init__(self):
        self.calls = []
    async def validate_and_recover(self):
        return True
    async def overwrite_psyche_trait(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True}


@pytest.fixture
def app_settings(tmp_path):
    psyche = tmp_path / "psyche"
    psyche.mkdir()
    for name in ("core_directives.json", "psyche_state.json"):
        shutil.copy2(ROOT / "capri_core" / "psyche" / name, psyche / name)
    return Settings(api_key="offline-test-key", data_dir=tmp_path / "data", psyche_dir=psyche,
                    repository_dir=tmp_path)


@pytest.fixture
def app(app_settings):
    return create_app(app_settings, editor=NoopEditor(), enable_background=False)


@pytest.mark.parametrize("route,payload", [
    ("/api/chat", {"message": "hola"}),
    ("/api/finance/add_expense", {"amount": "800.00", "concept": "componentes"}),
    ("/api/telemetry/tasker", {"location": "ipn", "battery_percent": 85, "charging": False}),
])
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
def test_every_endpoint_requires_key(app, route, payload, headers):
    with TestClient(app) as client:
        assert client.post(route, json=payload, headers=headers).status_code == 401


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/unknown", "/api/chat/"])
def test_no_unprotected_routes(app, path):
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"X-API-Key": "offline-test-key"}).status_code == 404


def test_duplicate_key_rejected(app):
    with TestClient(app) as client:
        assert client.post("/api/chat", json={"message": "hola"},
                           headers=[("X-API-Key", "offline-test-key"), ("X-API-Key", "wrong")]).status_code == 401


def test_exactly_three_endpoints(app):
    assert {(path, method) for path, operations in app.openapi()["paths"].items()
            for method in operations} == {
        ("/api/chat", "post"),
        ("/api/finance/add_expense", "post"),
        ("/api/telemetry/tasker", "post"),
    }


def test_cold_boot_then_fake_and_local_memory(app):
    with TestClient(app) as client:
        headers = {"X-API-Key": "offline-test-key"}
        first = client.post("/api/chat", json={"message": "hola"}, headers=headers)
        assert first.status_code == 200
        assert "Soy Capri" in first.json()["reply"] and first.json()["provider"] == "fake"
        second = client.post("/api/chat", json={"message": "qué tal"}, headers=headers)
        assert second.status_code == 200 and "Modo fake" in second.json()["reply"]
        assert not second.json()["fallback"]


def test_finance_query_and_explicit_category_stay_local(app):
    with TestClient(app) as client:
        headers = {"X-API-Key": "offline-test-key"}
        saved = client.post("/api/finance/add_expense", json={"amount": "800.01", "concept": "hardware"}, headers=headers)
        assert saved.status_code == 201
        assert saved.json()["amount"] == "800.01" and saved.json()["needs_category"]
        response = client.post("/api/chat", json={"message": "revisa mis gastos"}, headers=headers)
        assert response.status_code == 200 and "800.01" in response.json()["reply"]
        assert response.json()["provider"] == "local-finance"
        categorized = client.post("/api/chat", json={"message": "categoría: equipo"}, headers=headers)
        assert categorized.status_code == 200 and "registrada" in categorized.json()["reply"]
        followup = client.post("/api/chat", json={"message": "gastos"}, headers=headers)
        assert "sin categoría" in followup.json()["reply"]


@pytest.mark.parametrize("path,payload", [
    ("/api/finance/add_expense", {"amount": "1.001", "concept": "x"}),
    ("/api/finance/add_expense", {"amount": "1", "concept": " "}),
    ("/api/telemetry/tasker", {"battery_percent": 101}),
    ("/api/telemetry/tasker", {"latitude": 100}),
    ("/api/chat", {"message": ""}),
    ("/api/chat", {"message": "hola", "unexpected": True}),
])
def test_payload_validation(app, path, payload):
    with TestClient(app) as client:
        assert client.post(path, json=payload, headers={"X-API-Key": "offline-test-key"}).status_code == 422


def test_telemetry_success_returns_stub_intents(app):
    with TestClient(app) as client:
        result = client.post("/api/telemetry/tasker", json={"active_app": "Code::Blocks", "active_app_hours": 3.1},
                             headers={"X-API-Key": "offline-test-key"})
        assert result.status_code == 200
        assert result.json()["events"]
        assert result.json()["deliveries"]


def test_unconfigured_ollama_does_not_block_server(app_settings):
    from dataclasses import replace
    settings = replace(app_settings, cognitive_provider="cloud", cloud_api_key="", ollama_url="", ollama_model="")
    app = create_app(settings, editor=NoopEditor(), enable_background=False)
    with TestClient(app) as client:
        headers = {"X-API-Key": "offline-test-key"}
        assert client.post("/api/finance/add_expense", json={"amount": "1", "concept": "x"}, headers=headers).status_code == 201
        assert client.post("/api/chat", json={"message": "hola"}, headers=headers).status_code == 503


class ScriptedProvider(FakeProvider):
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
    async def generate(self, system_prompt, messages, tools=None):
        self.calls.append((system_prompt, messages.copy(), tools))
        return self.responses.pop(0)


def test_financial_tool_results_never_return_to_cloud(app_settings):
    cloud = ScriptedProvider([CognitiveResponse(tool_calls=[ToolCall("query_recent_expenses", {})], provider="gemini")])
    app = create_app(app_settings, engine=FallbackEngine(cloud, FakeProvider()), editor=NoopEditor(), enable_background=False)
    with TestClient(app) as client:
        headers = {"X-API-Key": "offline-test-key"}
        client.post("/api/finance/add_expense", json={"amount": "800", "concept": "dato-privado"}, headers=headers)
        response = client.post("/api/chat", json={"message": "cómo vamos"}, headers=headers)
        assert response.status_code == 200 and response.json()["provider"] == "local-finance"
        assert "800" in response.json()["reply"] and len(cloud.calls) == 1
        assert "dato-privado" not in repr(cloud.calls)


def test_tool_registration_and_validation(app_settings):
    provider = ScriptedProvider([
        CognitiveResponse(tool_calls=[ToolCall("overwrite_psyche_trait", {"target_key": "mood", "new_value": "alerta", "reason": "contexto"})]),
        CognitiveResponse(text="listo", provider="fake"),
    ])
    editor = NoopEditor()
    app = create_app(app_settings, engine=FallbackEngine(None, provider), editor=editor, enable_background=False)
    with TestClient(app) as client:
        response = client.post("/api/chat", json={"message": "cambia tu ánimo"}, headers={"X-API-Key": "offline-test-key"})
        assert response.status_code == 200 and response.json()["reply"] == "listo"
        assert editor.calls[0]["target_key"] == "mood"
        assert {tool["name"] for tool in provider.calls[0][2]} >= {"overwrite_psyche_trait", "query_recent_expenses"}


def test_unknown_tool_not_executed(app_settings):
    provider = ScriptedProvider([CognitiveResponse(tool_calls=[ToolCall("run_shell", {"command": "forbidden"})]),
                                 CognitiveResponse(text="rechazado", provider="fake")])
    app = create_app(app_settings, engine=FallbackEngine(None, provider), editor=NoopEditor(), enable_background=False)
    with TestClient(app) as client:
        response = client.post("/api/chat", json={"message": "hola"}, headers={"X-API-Key": "offline-test-key"})
        assert response.status_code == 200
        assert response.json()["tools"][0]["result"]["ok"] is False


def test_financial_nudge_is_proactive_and_not_persisted_as_memory(app):
    with TestClient(app) as client:
        headers = {"X-API-Key": "offline-test-key"}
        client.post("/api/finance/add_expense", json={"amount": "735.12", "concept": "privado"}, headers=headers)
        response = client.post("/api/chat", json={"message": "hola"}, headers=headers)
        assert response.status_code == 200
        assert "735.12" in response.json()["reply"]
        assert "735.12" not in repr(client.portal.call(app.state.memory.list_all))


def test_critical_phase_dispatches_notification_stub(app_settings):
    from datetime import datetime, timedelta
    from capri_core.telemetry.fuzzy_logic import FuzzyEngine
    now = datetime(2026, 1, 1, 8)
    fuzzy = FuzzyEngine(clock=lambda: now)
    app = create_app(app_settings, fuzzy=fuzzy, editor=NoopEditor(), enable_background=False)
    now += timedelta(hours=12)
    with TestClient(app) as client:
        result = client.post("/api/telemetry/tasker", json={}, headers={"X-API-Key": "offline-test-key"})
        assert result.status_code == 200 and result.json()["phase"] == 3
        critical = result.json()["deliveries"][-1]["system"]
        assert critical["operation"] == "show_critical_notification" and critical["simulated"]
