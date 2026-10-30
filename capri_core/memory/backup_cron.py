"""Respaldo diario cifrado de la memoria de Capri."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet, InvalidToken

from .mem0_client import MemoryProvider


LOGGER = logging.getLogger(__name__)


class BackupService:
    """Genera respaldos JSON cifrados y nunca escribe JSON en claro."""

    def __init__(
        self,
        memory: MemoryProvider,
        directory: Path,
        encryption_key: str,
        timezone_name: str = "America/Mexico_City",
        hour: int = 3,
    ):
        """Configura el respaldo sin crear archivos ni llamar a la memoria."""
        if not isinstance(hour, int) or not 0 <= hour <= 23:
            raise ValueError("hour debe ser un entero entre 0 y 23")
        self.memory = memory
        self.directory = Path(directory)
        self.timezone_name = timezone_name
        self.hour = hour
        self.marker_path = self.directory / ".backup-marker"
        self._lock = asyncio.Lock()
        self._timezone = self._load_timezone(timezone_name)
        self._fernet = self._load_fernet(encryption_key)

    @staticmethod
    def _load_timezone(timezone_name: str) -> ZoneInfo:
        """Obtiene la zona solicitada y usa UTC si está ausente en el entorno."""
        try:
            return ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            LOGGER.warning("Zona horaria inválida; se usará UTC")
            return ZoneInfo("UTC")

    @staticmethod
    def _load_fernet(encryption_key: str) -> Fernet | None:
        """Valida una clave Fernet sin registrar su contenido."""
        if not encryption_key:
            LOGGER.warning("Respaldo de memoria deshabilitado: falta la clave")
            return None
        try:
            key = encryption_key.encode("ascii") if isinstance(encryption_key, str) else encryption_key
            return Fernet(key)
        except (TypeError, ValueError, InvalidToken, UnicodeError):
            LOGGER.warning("Respaldo de memoria deshabilitado: clave inválida")
            return None

    @property
    def enabled(self) -> bool:
        """Indica si existe una clave Fernet válida."""
        return self._fernet is not None

    def _now(self) -> datetime:
        """Devuelve la hora actual en la zona configurada."""
        return datetime.now(self._timezone)

    @staticmethod
    def _backup_path(directory: Path, day: date) -> Path:
        """Construye el nombre del archivo cifrado de una fecha local."""
        return directory / f"memory_backup_{day.isoformat()}.json.enc"

    @staticmethod
    def _read_marker_sync(marker_path: Path) -> str | None:
        """Lee el marcador no secreto si existe."""
        try:
            value = marker_path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return None
        return value or None

    @staticmethod
    def _atomic_write_sync(path: Path, payload: bytes) -> None:
        """Escribe bytes mediante temporal del mismo directorio y os.replace."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _payload_bytes(records: list[dict], created_at: str) -> bytes:
        """Serializa el contenido de respaldo antes de cifrarlo."""
        payload: dict[str, Any] = {
            "version": 1,
            "created_at": created_at,
            "records": records,
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    async def backup_once(self) -> Path | None:
        """Crea como máximo un respaldo por día local y devuelve su ruta."""
        if not self.enabled:
            return None

        async with self._lock:
            now = self._now()
            day = now.date()
            backup_path = self._backup_path(self.directory, day)
            marker = await asyncio.to_thread(
                self._read_marker_sync,
                self.marker_path,
            )
            if marker == day.isoformat() and await asyncio.to_thread(
                backup_path.exists
            ):
                return None

            records = await self.memory.list_all()
            if not isinstance(records, list):
                raise TypeError("MemoryProvider.list_all() debe devolver una lista")
            if self._fernet is None:  # pragma: no cover: protegido por enabled
                return None
            encrypted = self._fernet.encrypt(
                self._payload_bytes(records, now.isoformat())
            )
            await asyncio.to_thread(
                self._atomic_write_sync,
                backup_path,
                encrypted,
            )
            await asyncio.to_thread(
                self._atomic_write_sync,
                self.marker_path,
                day.isoformat().encode("ascii"),
            )
            return backup_path

    def _next_target(self, now: datetime) -> datetime:
        """Calcula el siguiente instante de ejecución en la zona local."""
        target = now.replace(
            hour=self.hour,
            minute=0,
            second=0,
            microsecond=0,
        )
        if now >= target:
            target = target + timedelta(days=1)
        return target

    async def run(self, stop: asyncio.Event) -> None:
        """Espera cada hora programada hasta que el evento de parada se active."""
        if not self.enabled:
            return

        while not stop.is_set():
            now = self._now()
            target = now.replace(
                hour=self.hour,
                minute=0,
                second=0,
                microsecond=0,
            )
            if now >= target:
                try:
                    await self.backup_once()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    LOGGER.warning(
                        "No se pudo crear el respaldo de memoria (%s)",
                        type(error).__name__,
                    )
                target = target + timedelta(days=1)

            delay = max(0.0, (target - now).total_seconds())
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue


__all__ = ["BackupService"]
