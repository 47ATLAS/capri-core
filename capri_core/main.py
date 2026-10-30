"""Servidor y orquestador de la Fase 1. Arranque con uvicorn --factory."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.responses import JSONResponse

from config import Settings
from cognitive import CognitiveResponse, FallbackEngine, ProviderUnavailable, build_engine
from capri_core.memory.mem0_client import LocalMemory, Mem0Memory, MemoryProvider
from capri_core.memory.backup_cron import BackupService
from capri_core.mcp_tools.finance_manager import FinanceManager, build_finance_router, configure_finance_manager
from capri_core.mcp_tools.recursive_edit import RecursiveEditor, configure_editor
from capri_core.mcp_tools.system_actions import SystemActionsStub
from capri_core.psyche.prompt_builder import build_system_prompt
from capri_core.telemetry.fuzzy_logic import FuzzyEngine
from capri_core.telemetry.webhook_receiver import build_telemetry_router
from capri_core.voice.voice_engine import VoiceEngine

logger = logging.getLogger(__name__)


class APIKeyMiddleware:
    """Autenticar antes del routing, incluso rutas desconocidas y redirecciones."""
    def __init__(self, app: Any, api_key: str) -> None:
        self.app = app
        self._key = api_key.encode("utf-8")

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            supplied = [value for key, value in scope["headers"] if key.lower() == b"x-api-key"]
            if len(supplied) != 1 or not secrets.compare_digest(supplied[0], self._key):
                await JSONResponse({"detail": "API Key ausente o inválida."}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class ChatInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    message: str = Field(min_length=1, max_length=8000)


class TraitArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target_key: str = Field(min_length=1, max_length=80)
    new_value: str = Field(max_length=512)
    reason: str = Field(min_length=1, max_length=1000)


class QueryArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CategoryArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    expense_id: int = Field(gt=0)
    category: str = Field(min_length=1, max_length=80)


TOOLS: list[dict[str, Any]] = [
    {"name": "overwrite_psyche_trait", "description": "Cambiar un rasgo mutable validado; nunca directrices.",
     "parameters": {"type": "object", "properties": {
         "target_key": {"type": "string"}, "new_value": {"type": "string"},
         "reason": {"type": "string"}}, "required": ["target_key", "new_value", "reason"]}},
    {"name": "query_recent_expenses", "description": "Consultar gastos locales e identificar categorías NULL.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "categorize_expense", "description": "Categorizar un gasto según la respuesta explícita del usuario.",
     "parameters": {"type": "object", "properties": {
         "expense_id": {"type": "integer"}, "category": {"type": "string"}},
         "required": ["expense_id", "category"]}},
]

# La ruta financiera y sus resultados nunca entran al prompt de un tercero.
FINANCE_LANGUAGE = re.compile(
    r"gasto|finanz|categor[ií]a|cargo|compr[aeé]|dinero|presupuesto|cuenta|saldo|\bmxn\b|\bpesos\b|\$|\b\d+[.,]?\d*\b", re.I)


class Orchestrator:
    def __init__(self, settings: Settings, engine: FallbackEngine, memory: MemoryProvider,
                 finance: FinanceManager, editor: RecursiveEditor, voice: VoiceEngine) -> None:
        self.settings, self.engine, self.memory = settings, engine, memory
        self.finance, self.editor, self.voice = finance, editor, voice
        self._lock = asyncio.Lock()
        self.cold_boot = True
        self.pending_expense_id: int | None = None

    async def _finance_reply(self, category: str | None = None) -> str:
        if category is not None and self.pending_expense_id is not None:
            await self.finance.categorize_expense(self.pending_expense_id, category)
            self.pending_expense_id = None
            return "Categoría registrada, choom. No dejes cargos en el limbo."
        recent = await self.finance.query_recent_expenses()
        ambiguous = next((expense for expense in recent if expense["needs_category"]), None)
        if ambiguous:
            self.pending_expense_id = ambiguous["id"]
            return (f"Veo un cargo sin identificar de {ambiguous['amount']} pesos. ¿En qué lo gastaste esta vez? "
                    "Responde «categoría: nombre» para identificarlo.")
        return "No hay gastos recientes sin categoría, choom." if recent else "No hay gastos registrados todavía, choom."

    async def chat(self, message: str) -> dict[str, Any]:
        async with self._lock:
            category_match = re.fullmatch(r"categor[ií]a\s*:\s*(.{1,80})", message, re.I)
            if category_match and self.pending_expense_id is not None:
                text = await self._finance_reply(category_match[1].strip())
                return await self._result(CognitiveResponse(text=text, provider="local-finance"))
            if FINANCE_LANGUAGE.search(message):
                return await self._result(CognitiveResponse(text=await self._finance_reply(), provider="local-finance"))
            context = await self.memory.search(message, limit=10)
            context = [item for item in context if item.get("metadata", {}).get("domain") != "finance"]
            system = await asyncio.to_thread(build_system_prompt, self.settings.psyche_dir, context, self.cold_boot)
            messages: list[dict[str, Any]] = [{"role": "user", "content": message}]
            response = await self.engine.generate(system, messages, TOOLS)
            fallback = response.fallback
            tool_results: list[dict[str, Any]] = []
            for _ in range(4):
                if not response.tool_calls:
                    break
                if len(response.tool_calls) > 8:
                    raise ProviderUnavailable("Demasiadas llamadas de herramientas.")
                messages.append(response.as_message())
                financial_response = False
                for call in response.tool_calls:
                    try:
                        if not isinstance(call.arguments, dict):
                            raise ValueError("Argumentos inválidos")
                        if call.name == "overwrite_psyche_trait":
                            args = TraitArguments.model_validate(call.arguments)
                            result = await self.editor.overwrite_psyche_trait(**args.model_dump())
                        elif call.name == "query_recent_expenses":
                            QueryArguments.model_validate(call.arguments)
                            # No se reenvía un resultado financiero a Gemini ni Mem0.
                            result = {"handled_locally": True}
                            response = CognitiveResponse(text=await self._finance_reply(), provider="local-finance", fallback=fallback)
                            financial_response = True
                        elif call.name == "categorize_expense":
                            args = CategoryArguments.model_validate(call.arguments)
                            explicit = category_match is not None
                            if not explicit or args.expense_id != self.pending_expense_id or \
                                    args.category != category_match[1].strip():
                                raise ValueError("Categoría sin confirmación explícita del usuario")
                            result = {"handled_locally": True}
                            response = CognitiveResponse(text=await self._finance_reply(args.category), provider="local-finance", fallback=fallback)
                            financial_response = True
                        else:
                            raise ValueError("Herramienta no permitida")
                    except (ValueError, ValidationError) as exc:
                        result = {"ok": False, "error": "Argumentos o herramienta rechazados."}
                    except Exception:
                        # No exponer errores de Git, paths internos ni datos del usuario.
                        result = {"ok": False, "error": "La operación no pudo completarse."}
                    tool_results.append({"name": call.name, "result": result})
                    messages.append({"role": "tool", "name": call.name, "id": call.id, "result": result})
                    if financial_response:
                        break
                if financial_response:
                    return await self._result(response, tool_results)
                response = await self.engine.generate(system, messages, TOOLS, force_local=fallback)
                fallback = fallback or response.fallback
            if response.tool_calls:
                raise ProviderUnavailable("Límite de iteraciones de herramientas alcanzado.")
            response.fallback = fallback
            # Memoria dinámica, excluyendo todo el flujo financiero.
            await self.memory.add(message, metadata={"domain": "conversation", "role": "user"})
            await self.memory.add(response.text, metadata={"domain": "conversation", "role": "assistant"})
            self.cold_boot = False
            # Pregunta proactiva local: se añade DESPUÉS de persistir la parte no financiera.
            recent = await self.finance.query_recent_expenses()
            if any(expense["needs_category"] for expense in recent):
                response.text += "\n" + await self._finance_reply()
            return await self._result(response, tool_results)

    async def _result(self, response: CognitiveResponse,
                      tool_results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {"reply": response.text, "provider": response.provider, "fallback": response.fallback,
                "voice": await self.voice.process(response.text), "tools": tool_results or []}


def create_app(settings: Settings | None = None, *, engine: FallbackEngine | None = None,
               memory: MemoryProvider | None = None, finance: FinanceManager | None = None,
               fuzzy: FuzzyEngine | None = None, editor: RecursiveEditor | None = None,
               enable_background: bool = True) -> FastAPI:
    """Construir la aplicación sin depender de llaves ni servicios de terceros."""
    settings = settings or Settings.from_env()
    engine = engine or build_engine(settings)
    if memory is None:
        if settings.memory_provider in {"mem0", "auto"} and settings.mem0_api_key and settings.mem0_url:
            memory = Mem0Memory(settings.mem0_api_key, settings.mem0_url, settings.mem0_user_id, settings.http_timeout)
        else:
            memory = LocalMemory(settings.data_dir / "memory.sqlite3")
    finance = finance or FinanceManager(settings.data_dir / "finance.sqlite3")
    fuzzy = fuzzy or FuzzyEngine(settings.timezone, settings.low_heart_rate)
    editor = editor or RecursiveEditor(settings.psyche_dir, settings.repository_dir, settings.data_dir / "psyche-audit.log")
    voice = VoiceEngine(enabled=settings.elevenlabs_enabled, api_key=settings.elevenlabs_api_key)
    actions = SystemActionsStub()
    stop = asyncio.Event()
    tasks: list[asyncio.Task] = []
    orchestrator = Orchestrator(settings, engine, memory, finance, editor, voice)
    backup = BackupService(memory, settings.data_dir / "backups", settings.backup_key, settings.timezone, settings.backup_hour)

    async def dispatch(result: dict[str, Any]) -> dict[str, Any]:
        deliveries = []
        for event in result.get("events", []):
            intent = {**event, "type": "show_critical_notification"} if event.get("critical") else event
            deliveries.append({"system": await actions.execute_intent(intent),
                               "voice": await voice.process(event.get("text", ""), force=event.get("requires_voice", False))})
        result["deliveries"] = deliveries
        return result

    class TelemetryOrchestrator:
        async def ingest(self, payload: Any) -> dict[str, Any]:
            return await dispatch(await fuzzy.ingest(payload))

    async def timer() -> None:
        while not stop.is_set():
            try:
                app.state.last_telemetry = await dispatch(await fuzzy.evaluate())
            except Exception:
                logger.warning("No se pudo evaluar el temporizador de telemetría.")
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.timer_interval)
            except TimeoutError:
                pass

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(settings.data_dir.mkdir, parents=True, exist_ok=True)
        try:
            await memory.initialize()
            await finance.initialize()
            configure_finance_manager(finance)
            if isinstance(editor, RecursiveEditor):
                configure_editor(editor)
            if await editor.validate_and_recover() is False:
                raise RuntimeError("Psique inválida sin recuperación segura")
            # Validar W++ y JSON antes de servir peticiones.
            await asyncio.to_thread(build_system_prompt, settings.psyche_dir)
            orchestrator.cold_boot = not bool(await memory.list_all())
            if enable_background:
                await asyncio.to_thread(editor.start_watchdog)
                tasks.extend([asyncio.create_task(backup.run(stop)), asyncio.create_task(timer())])
            yield
        finally:
            stop.set()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if enable_background:
                await asyncio.to_thread(editor.stop_watchdog)
            await memory.close()
            await engine.close()

    app = FastAPI(title="Capri — Fase 1", docs_url=None, redoc_url=None, openapi_url=None,
                  redirect_slashes=False, lifespan=lifespan)
    app.add_middleware(APIKeyMiddleware, api_key=settings.api_key)
    app.state.settings, app.state.orchestrator = settings, orchestrator
    app.state.memory, app.state.finance, app.state.editor = memory, finance, editor
    app.state.backup, app.state.fuzzy = backup, fuzzy
    app.state.last_telemetry = None
    app.include_router(build_finance_router(finance))
    app.include_router(build_telemetry_router(TelemetryOrchestrator()))

    @app.post("/api/chat")
    async def chat(payload: ChatInput) -> dict[str, Any]:
        try:
            return await orchestrator.chat(payload.message)
        except ProviderUnavailable:
            raise HTTPException(status_code=503, detail="Motor cognitivo no disponible; revisa el proveedor local.") from None
        except Exception:
            logger.warning("No se pudo completar la petición cognitiva.")
            raise HTTPException(status_code=503, detail="No se pudo completar la petición; revisa los servicios locales.") from None

    return app
