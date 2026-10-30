"""Edición segura del estado mutable de la psique de Capri."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from capri_core.psyche.prompt_builder import PsycheState


_ALLOWED_TRAITS: Final[frozenset[str]] = frozenset(
    {
        "mood",
        "trust",
        "sarcasm_level",
        "vad.valence",
        "vad.arousal",
        "vad.dominance",
    }
)
_NUMERIC_RANGES: Final[dict[str, tuple[float, float]]] = {
    "trust": (0.0, 1.0),
    "sarcasm_level": (0.0, 1.0),
    "vad.valence": (-1.0, 1.0),
    "vad.arousal": (-1.0, 1.0),
    "vad.dominance": (-1.0, 1.0),
}
_NUMERIC_TEXT: Final[re.Pattern[str]] = re.compile(
    r"^[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][+-]?\d+)?$"
)

OVERWRITE_PSYCHE_TRAIT_TOOL: Final[dict[str, Any]] = {
    "name": "overwrite_psyche_trait",
    "description": "Modificar un único atributo permitido de psyche_state.json.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["target_key", "new_value", "reason"],
        "properties": {
            "target_key": {
                "type": "string",
                "enum": sorted(_ALLOWED_TRAITS),
            },
            "new_value": {"type": "string"},
            "reason": {"type": "string", "maxLength": 2000},
        },
    },
}


class _PsycheEventHandler(FileSystemEventHandler):
    """Filtrar eventos del archivo de estado y delegar su validación."""

    def __init__(self, editor: "RecursiveEditor") -> None:
        super().__init__()
        self._editor = editor

    def on_modified(self, event: FileSystemEvent) -> None:
        """Validar después de una modificación del archivo objetivo."""
        self._editor._handle_watchdog_path(event.src_path, event.is_directory)

    def on_created(self, event: FileSystemEvent) -> None:
        """Validar después de la creación del archivo objetivo."""
        self._editor._handle_watchdog_path(event.src_path, event.is_directory)

    def on_moved(self, event: FileSystemEvent) -> None:
        """Validar el destino de un reemplazo atómico del archivo objetivo."""
        destination = getattr(event, "dest_path", event.src_path)
        self._editor._handle_watchdog_path(destination, event.is_directory)


class RecursiveEditor:
    """Editar sólo psyche_state con validación, respaldo Git y recuperación cerrada."""

    def __init__(self, psyche_dir: Path, repository_dir: Path, audit_path: Path) -> None:
        """Configurar rutas y los bloqueos compartidos con Watchdog."""
        self.psyche_dir = Path(psyche_dir).expanduser().resolve()
        self.repository_dir = Path(repository_dir).expanduser().resolve()
        self.audit_path = Path(audit_path).expanduser().resolve()
        self.state_path = self.psyche_dir / "psyche_state.json"
        self.core_path = self.psyche_dir / "core_directives.json"
        self._async_lock = asyncio.Lock()
        self._thread_lock = threading.RLock()
        self._observer: Observer | None = None

    async def overwrite_psyche_trait(
        self, target_key: str, new_value: str, reason: str
    ) -> dict[str, Any]:
        """Validar y escribir un atributo mutable sin tocar directivas ni staging ajeno."""
        async with self._async_lock:
            return await asyncio.to_thread(
                self._overwrite_psyche_trait_sync, target_key, new_value, reason
            )

    async def validate_and_recover(self) -> bool:
        """Validar el estado y restaurarlo desde el último commit válido si hace falta."""
        async with self._async_lock:
            return await asyncio.to_thread(self._validate_and_recover_sync)

    def start_watchdog(self) -> None:
        """Validar al arrancar y observar cambios del archivo de estado."""
        with self._thread_lock:
            if self._observer is not None:
                return
            if not self._validate_and_recover_locked():
                raise RuntimeError("No existe un commit válido para recuperar la psique")
            observer = Observer()
            observer.schedule(
                _PsycheEventHandler(self), str(self.psyche_dir), recursive=False
            )
            observer.start()
            self._observer = observer

    def stop_watchdog(self) -> None:
        """Detener el observador sin bloquear su hilo de eventos."""
        with self._thread_lock:
            observer = self._observer
            self._observer = None
        if observer is not None:
            observer.stop()
            observer.join(timeout=5.0)

    def _overwrite_psyche_trait_sync(
        self, target_key: str, new_value: str, reason: str
    ) -> dict[str, Any]:
        """Implementar la operación síncrona dentro de un hilo de trabajo."""
        self._validate_target_key(target_key)
        clean_reason = self._validate_reason(reason)
        with self._thread_lock:
            current = self._load_or_recover_current_state_locked()
            candidate_data = current.model_dump(mode="python")
            parsed_value = self._parse_value(target_key, new_value)
            self._set_candidate_value(candidate_data, target_key, parsed_value)
            candidate = PsycheState.model_validate(candidate_data)
            previous_commit = self._commit_state_snapshot_locked()
            self._append_audit(target_key, clean_reason, previous_commit)
            self._atomic_write_model(candidate)
            return {
                "ok": True,
                "target_key": target_key,
                "value": parsed_value,
                "previous_commit": previous_commit,
            }

    def _validate_target_key(self, target_key: str) -> None:
        """Aplicar la allowlist y bloquear claves de rutas o del núcleo."""
        if not isinstance(target_key, str) or target_key not in _ALLOWED_TRAITS:
            raise ValueError("target_key no está permitido")
        if any(part in target_key for part in ("/", "\\", "..")):
            raise ValueError("target_key contiene una ruta no permitida")

    @staticmethod
    def _validate_reason(reason: str) -> str:
        """Validar el motivo que se conservará en la auditoría local."""
        if not isinstance(reason, str):
            raise TypeError("reason debe ser texto")
        clean_reason = reason.strip()
        if not clean_reason or len(clean_reason) > 2000 or "\x00" in clean_reason:
            raise ValueError("reason vacío, demasiado largo o inválido")
        return clean_reason

    @staticmethod
    def _parse_value(target_key: str, new_value: str) -> str | float:
        """Convertir valores textuales con sintaxis numérica estricta y finita."""
        if not isinstance(new_value, str):
            raise TypeError("new_value debe ser texto")
        if target_key == "mood":
            if len(new_value) > 120:
                raise ValueError("mood supera 120 caracteres")
            return new_value
        if not _NUMERIC_TEXT.fullmatch(new_value):
            raise ValueError("new_value no es una cifra válida")
        value = float(new_value)
        if not math.isfinite(value):
            raise ValueError("new_value no puede ser NaN ni infinito")
        lower, upper = _NUMERIC_RANGES[target_key]
        if not lower <= value <= upper:
            raise ValueError("new_value está fuera de rango")
        return value

    @staticmethod
    def _set_candidate_value(
        candidate_data: dict[str, Any], target_key: str, value: str | float
    ) -> None:
        """Asignar sólo una propiedad allowlisted al candidato en memoria."""
        if target_key.startswith("vad."):
            candidate_data["vad"][target_key.split(".", 1)[1]] = value
        else:
            candidate_data[target_key] = value

    def _load_or_recover_current_state_locked(self) -> PsycheState:
        """Cargar estado válido o recuperarlo antes de intentar editarlo."""
        try:
            return self._read_state_model()
        except (OSError, ValueError, TypeError):
            if not self._validate_and_recover_locked():
                raise RuntimeError("psyche_state.json inválido y sin commit recuperable")
            return self._read_state_model()

    def _ensure_safe_state_path(self) -> None:
        """No seguir enlaces ni permitir que estado y núcleo compartan archivo."""
        if self.state_path.is_symlink():
            raise ValueError("El estado no puede ser un enlace simbólico")
        if self.state_path.exists():
            if not self.state_path.is_file() or self.state_path.stat().st_nlink != 1:
                raise ValueError("El estado debe ser un archivo regular sin enlaces")
            if self.core_path.exists() and self.state_path.samefile(self.core_path):
                raise ValueError("El núcleo no es un destino de edición")

    def _read_state_model(self) -> PsycheState:
        """Leer y validar el JSON mutable sin exponer su contenido en errores."""
        try:
            self._ensure_safe_state_path()
            raw = self.state_path.read_text(encoding="utf-8")
            return PsycheState.model_validate_json(raw)
        except (OSError, ValueError, TypeError):
            raise ValueError("psyche_state.json no cumple el esquema estricto") from None

    def _validate_and_recover_sync(self) -> bool:
        """Ejecutar la recuperación bajo el mismo bloqueo que las ediciones."""
        with self._thread_lock:
            return self._validate_and_recover_locked()

    def _validate_and_recover_locked(self) -> bool:
        """Recuperar sólo el archivo de estado desde la historia Git válida."""
        try:
            self._read_state_model()
            return True
        except (OSError, ValueError, TypeError):
            pass

        relative_state = self._relative_state_path()
        commits = self._git(
            ["rev-list", "HEAD", "--", relative_state], check=False
        )
        if commits.returncode != 0:
            return False
        for commit in commits.stdout.splitlines():
            shown = self._git(
                ["show", f"{commit}:{relative_state}"], check=False
            )
            if shown.returncode != 0:
                continue
            try:
                model = PsycheState.model_validate_json(shown.stdout)
            except (ValueError, TypeError):
                continue
            self._atomic_write_model(model)
            return True
        return False

    def _relative_state_path(self) -> str:
        """Obtener una ruta Git relativa y bloquear escapes del repositorio."""
        try:
            relative = self.state_path.relative_to(self.repository_dir)
        except ValueError:
            raise ValueError("psyche_state.json debe estar dentro del repositorio") from None
        return relative.as_posix()

    def _commit_state_snapshot_locked(self) -> str:
        """Crear el commit previo usando un índice temporal que excluye staging ajeno."""
        self._ensure_safe_state_path()
        relative_state = self._relative_state_path()
        descriptor, temporary_index = tempfile.mkstemp(prefix="capri-git-index-")
        os.close(descriptor)
        Path(temporary_index).unlink(missing_ok=True)
        environment = os.environ.copy()
        environment["GIT_INDEX_FILE"] = temporary_index
        environment["GIT_TERMINAL_PROMPT"] = "0"
        try:
            self._git(["read-tree", "HEAD"], env=environment)
            self._git(["add", "--", relative_state], env=environment)
            difference = self._git(
                ["diff", "--cached", "--quiet", "--", relative_state],
                env=environment,
                check=False,
            )
            if difference.returncode not in {0, 1}:
                raise RuntimeError("Git no pudo comparar el estado de Capri")
            self._git(
                [
                    "commit",
                    "--allow-empty",
                    "-m",
                    "Capri: respaldo previo de psyche_state",
                    "-m",
                    "Co-authored-by: Manus <dev-agent@manus.ai>",
                ],
                env=environment,
            )
            return self._git(["rev-parse", "HEAD"], env=environment).stdout.strip()
        finally:
            Path(temporary_index).unlink(missing_ok=True)

    def _git(
        self,
        arguments: list[str],
        *,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Ejecutar Git sin imprimir stdout, stderr ni datos del usuario."""
        completed = subprocess.run(
            ["git", "-C", str(self.repository_dir), *arguments],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if check and completed.returncode != 0:
            raise RuntimeError("La operación Git de Capri falló")
        return completed

    def _atomic_write_model(self, model: PsycheState) -> None:
        """Escribir el estado validado con reemplazo atómico y fsync."""
        self._ensure_safe_state_path()
        self.psyche_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            model.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=False,
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.", suffix=".tmp", dir=self.state_path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.state_path)
        finally:
            Path(temporary_name).unlink(missing_ok=True)

    def _append_audit(self, target_key: str, reason: str, previous_commit: str) -> None:
        """Añadir una línea de auditoría local sin registrar valores de estado."""
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "target_key": target_key,
            "reason": reason,
            "previous_commit": previous_commit,
            "operation": "overwrite_intent",
        }
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _handle_watchdog_path(self, path: str, is_directory: bool) -> None:
        """Validar eventos tras un replace atómico sin competir con el escritor."""
        if is_directory:
            return
        try:
            candidate_path = Path(path).absolute()
        except (OSError, ValueError):
            return
        if candidate_path != self.state_path:
            return
        with self._thread_lock:
            try:
                self._read_state_model()
            except (OSError, ValueError, TypeError):
                try:
                    if not self._validate_and_recover_locked():
                        logging.getLogger(__name__).warning("Psique inválida sin revisión recuperable")
                except Exception:
                    logging.getLogger(__name__).warning("No se pudo recuperar la psique de forma segura")


_configured_editor: RecursiveEditor | None = None


def configure_editor(editor: RecursiveEditor) -> None:
    """Configurar la instancia que utilizará la función MCP de módulo."""
    if not isinstance(editor, RecursiveEditor):
        raise TypeError("editor debe ser una instancia de RecursiveEditor")
    global _configured_editor
    _configured_editor = editor


async def overwrite_psyche_trait(
    target_key: str, new_value: str, reason: str
) -> dict[str, Any]:
    """Delegar overwrite_psyche_trait en el editor configurado por el padre."""
    if _configured_editor is None:
        raise RuntimeError("RecursiveEditor no está configurado")
    return await _configured_editor.overwrite_psyche_trait(target_key, new_value, reason)
