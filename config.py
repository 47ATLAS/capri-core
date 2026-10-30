"""Configuración única de Capri; no contiene secretos ni modelos codificados."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    environment: str = "dev"
    cognitive_provider: str = "fake"
    memory_provider: str = "local"
    cloud_api_key: str = field(default="", repr=False)
    cloud_url: str = ""
    cloud_model: str = ""
    ollama_url: str = ""
    ollama_model: str = ""
    mem0_api_key: str = field(default="", repr=False)
    mem0_url: str = ""
    mem0_user_id: str = "capri-owner"
    backup_key: str = field(default="", repr=False)
    data_dir: Path = ROOT / ".data"
    psyche_dir: Path = ROOT / "capri_core" / "psyche"
    repository_dir: Path = ROOT
    timezone: str = "America/Mexico_City"
    low_heart_rate: int = 60
    http_timeout: float = 30.0
    timer_interval: float = 60.0
    backup_hour: int = 3
    elevenlabs_enabled: bool = False
    elevenlabs_api_key: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not self.api_key or not self.api_key.strip():
            raise ValueError("CAPRI_API_KEY es obligatoria.")
        if self.environment not in {"dev", "prod"}:
            raise ValueError("CAPRI_ENV debe ser dev o prod.")
        if self.cognitive_provider not in {"fake", "ollama", "cloud", "auto"}:
            raise ValueError("CAPRI_ENGINE inválido.")
        if self.environment != "dev" and self.cognitive_provider == "fake":
            raise ValueError("El motor fake solo se permite en CAPRI_ENV=dev.")
        if self.memory_provider not in {"local", "mem0", "auto"}:
            raise ValueError("CAPRI_MEMORY inválido.")
        if not 20 <= self.low_heart_rate <= 120:
            raise ValueError("CAPRI_LOW_HEART_RATE fuera de rango.")
        if self.http_timeout <= 0 or self.timer_interval <= 0:
            raise ValueError("Los intervalos deben ser positivos.")
        if not 0 <= self.backup_hour <= 23:
            raise ValueError("CAPRI_BACKUP_HOUR debe estar entre 0 y 23.")
        ZoneInfo(self.timezone)

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> Settings:
        """Leer .env sin modificar el entorno global; variables del proceso prevalecen."""
        values = {**dotenv_values(env_file or ROOT / ".env"), **os.environ}
        env = values.get("CAPRI_ENV", "dev")
        def get(key: str, default: str = "") -> str:
            return str(values.get(key) or default)
        return cls(
            api_key=get("CAPRI_API_KEY"), environment=str(env),
            cognitive_provider=get("CAPRI_ENGINE", "fake" if env == "dev" else "auto"),
            memory_provider=get("CAPRI_MEMORY", "local" if env == "dev" else "auto"),
            cloud_api_key=get("GEMINI_API_KEY"), cloud_url=get("GEMINI_API_URL"),
            cloud_model=get("GEMINI_MODEL"), ollama_url=get("OLLAMA_URL"),
            ollama_model=get("OLLAMA_MODEL"), mem0_api_key=get("MEM0_API_KEY"),
            mem0_url=get("MEM0_API_URL"), mem0_user_id=get("MEM0_USER_ID", "capri-owner"),
            backup_key=get("CAPRI_BACKUP_KEY"), data_dir=Path(get("CAPRI_DATA_DIR", str(ROOT / ".data"))).resolve(),
            timezone=get("CAPRI_TIMEZONE", "America/Mexico_City"),
            low_heart_rate=int(get("CAPRI_LOW_HEART_RATE", "60")),
            http_timeout=float(get("CAPRI_HTTP_TIMEOUT", "30")),
            timer_interval=float(get("CAPRI_TIMER_INTERVAL", "60")),
            backup_hour=int(get("CAPRI_BACKUP_HOUR", "3")),
            elevenlabs_enabled=get("ELEVENLABS_ENABLED", "false").lower() in {"1", "true", "yes"},
            elevenlabs_api_key=get("ELEVENLABS_API_KEY"),
        )
