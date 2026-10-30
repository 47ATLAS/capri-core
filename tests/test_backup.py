"""Pruebas del servicio de respaldo cifrado."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet

from capri_core.memory.backup_cron import BackupService


class FakeMemory:
    """Proveedor mínimo sin red para verificar el contrato del backup."""

    def __init__(self) -> None:
        self.calls = 0

    async def initialize(self) -> None:
        return None

    async def add(self, text: str, conflict_key=None, metadata=None) -> dict:
        return {}

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        return []

    async def list_all(self) -> list[dict]:
        self.calls += 1
        return [
            {
                "id": "1",
                "text": "dato confidencial",
                "metadata": {"source": "test"},
                "conflict_key": None,
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        ]

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_backup_fernet_roundtrip_y_no_duplica_el_dia(tmp_path: Path):
    """El archivo contiene solo ciphertext y se puede descifrar con la clave."""
    memory = FakeMemory()
    key = Fernet.generate_key().decode("ascii")
    service = BackupService(
        memory=memory,
        directory=tmp_path,
        encryption_key=key,
        timezone_name="UTC",
        hour=0,
    )

    path = await service.backup_once()
    assert path is not None
    ciphertext = path.read_bytes()
    assert b"dato confidencial" not in ciphertext
    payload = json.loads(Fernet(key.encode("ascii")).decrypt(ciphertext))
    assert payload["records"][0]["text"] == "dato confidencial"
    assert await service.backup_once() is None
    assert memory.calls == 1
    assert service.marker_path.read_text(encoding="ascii") == datetime.now(
        ZoneInfo("UTC")
    ).date().isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "not-a-fernet-key"])
async def test_backup_desactivado_sin_clave_valida(tmp_path: Path, key: str):
    """Una clave ausente o inválida no provoca caída ni crea plaintext."""
    memory = FakeMemory()
    service = BackupService(memory, tmp_path, key, timezone_name="UTC")

    assert service.enabled is False
    assert await service.backup_once() is None
    stop = asyncio.Event()
    stop.set()
    await service.run(stop)
    assert list(tmp_path.iterdir()) == []
    assert memory.calls == 0


@pytest.mark.asyncio
async def test_backup_run_se_detiene_con_evento(tmp_path: Path):
    """El bucle de horario responde a stop sin esperar hasta el día siguiente."""
    key = Fernet.generate_key().decode("ascii")
    service = BackupService(
        FakeMemory(),
        tmp_path,
        key,
        timezone_name="UTC",
        hour=(datetime.now(ZoneInfo("UTC")).hour + 1) % 24,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(service.run(stop))
    await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
