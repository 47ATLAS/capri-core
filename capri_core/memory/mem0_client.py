"""Proveedores de memoria local y remoto para Capri."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urljoin, urlsplit

import httpx


LOGGER = logging.getLogger(__name__)


@runtime_checkable
class MemoryProvider(Protocol):
    """Contrato asíncrono común de los proveedores de memoria."""

    async def initialize(self) -> None:
        """Prepara el proveedor para recibir operaciones."""
        ...

    async def add(
        self,
        text: str,
        conflict_key: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Agrega o reemplaza un hecho y devuelve su registro."""
        ...

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        """Busca hechos relevantes para una consulta."""
        ...

    async def list_all(self) -> list[dict]:
        """Devuelve todos los hechos del proveedor."""
        ...

    async def close(self) -> None:
        """Libera recursos del proveedor."""
        ...


def _utc_now() -> str:
    """Devuelve la hora UTC con formato ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


def _metadata_copy(metadata: dict | None) -> dict[str, Any]:
    """Valida y copia metadata sin compartir referencias mutables."""
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise TypeError("metadata debe ser un diccionario")
    return copy.deepcopy(metadata)


def _validate_add_arguments(text: str, conflict_key: str | None) -> None:
    """Valida los argumentos compartidos por los proveedores."""
    if not isinstance(text, str):
        raise TypeError("text debe ser una cadena")
    if conflict_key is not None and not isinstance(conflict_key, str):
        raise TypeError("conflict_key debe ser una cadena o None")


def _record(
    *,
    record_id: str,
    text: str,
    metadata: dict | None,
    conflict_key: str | None,
    updated_at: str,
) -> dict:
    """Construye un registro con el contrato público estable."""
    return {
        "id": record_id,
        "text": text,
        "metadata": _metadata_copy(metadata),
        "conflict_key": conflict_key,
        "updated_at": updated_at,
    }


class LocalMemory:
    """Memoria persistente en SQLite con búsqueda léxica local."""

    def __init__(self, path: Path):
        """Crea un proveedor apuntando a la base SQLite indicada."""
        self.path = Path(path)
        self._initialized = False
        self._closed = False

    def _connect(self) -> sqlite3.Connection:
        """Abre una conexión independiente para una operación de un hilo."""
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_sync(self) -> None:
        """Crea el esquema SQLite de forma idempotente."""
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    conflict_key TEXT UNIQUE,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memories_updated_at
                    ON memories(updated_at);
                """
            )
            connection.commit()
        finally:
            connection.close()

    async def initialize(self) -> None:
        """Crea la base local sin bloquear el bucle de eventos."""
        if self._closed:
            raise RuntimeError("el proveedor de memoria está cerrado")
        if self._initialized:
            return
        await asyncio.to_thread(self._initialize_sync)
        self._initialized = True

    async def _ensure_ready(self) -> None:
        """Inicializa perezosamente el proveedor cuando es necesario."""
        if self._closed:
            raise RuntimeError("el proveedor de memoria está cerrado")
        if not self._initialized:
            await self.initialize()

    @staticmethod
    def _decode_metadata(value: str) -> dict:
        """Decodifica metadata almacenada y conserva un objeto válido."""
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}

    @classmethod
    def _row_to_record(cls, row: sqlite3.Row) -> dict:
        """Convierte una fila SQLite al contrato público."""
        metadata = cls._decode_metadata(row["metadata"])
        return _record(
            record_id=row["id"],
            text=row["text"],
            metadata=metadata,
            conflict_key=row["conflict_key"],
            updated_at=row["updated_at"],
        )

    def _add_sync(
        self,
        text: str,
        conflict_key: str | None,
        metadata: dict | None,
    ) -> dict:
        """Inserta o actualiza un hecho dentro de una transacción SQLite."""
        supplied_metadata = _metadata_copy(metadata)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                existing = None
                if conflict_key is not None:
                    existing = connection.execute(
                        """
                        SELECT id, text, metadata, conflict_key, updated_at
                        FROM memories
                        WHERE conflict_key = ?
                        """,
                        (conflict_key,),
                    ).fetchone()

                updated_at = _utc_now()
                if existing is None:
                    record_id = str(uuid.uuid4())
                    connection.execute(
                        """
                        INSERT INTO memories
                            (id, text, metadata, conflict_key, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            record_id,
                            text,
                            json.dumps(
                                supplied_metadata,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            conflict_key,
                            updated_at,
                        ),
                    )
                    return _record(
                        record_id=record_id,
                        text=text,
                        metadata=supplied_metadata,
                        conflict_key=conflict_key,
                        updated_at=updated_at,
                    )

                previous_metadata = self._decode_metadata(existing["metadata"])
                versions = previous_metadata.get("versions", [])
                if not isinstance(versions, list):
                    versions = []
                versions = copy.deepcopy(versions)
                previous_metadata = {key: value for key, value in previous_metadata.items()
                                     if key != "versions"}
                versions.append(
                    {
                        "id": existing["id"],
                        "text": existing["text"],
                        "metadata": previous_metadata,
                        "conflict_key": existing["conflict_key"],
                        "updated_at": existing["updated_at"],
                    }
                )
                supplied_metadata["versions"] = versions
                connection.execute(
                    """
                    UPDATE memories
                    SET text = ?, metadata = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        text,
                        json.dumps(
                            supplied_metadata,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        updated_at,
                        existing["id"],
                    ),
                )
                return _record(
                    record_id=existing["id"],
                    text=text,
                    metadata=supplied_metadata,
                    conflict_key=conflict_key,
                    updated_at=updated_at,
                )
        finally:
            connection.close()

    async def add(
        self,
        text: str,
        conflict_key: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Agrega un hecho o reemplaza el activo con la misma clave."""
        _validate_add_arguments(text, conflict_key)
        _metadata_copy(metadata)
        await self._ensure_ready()
        return await asyncio.to_thread(self._add_sync, text, conflict_key, metadata)

    def _list_all_sync(self) -> list[dict]:
        """Lee todos los registros ordenados por actualización descendente."""
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT id, text, metadata, conflict_key, updated_at
                FROM memories
                ORDER BY updated_at DESC, rowid DESC
                """
            ).fetchall()
            return [self._row_to_record(row) for row in rows]
        finally:
            connection.close()

    async def list_all(self) -> list[dict]:
        """Devuelve todos los registros persistidos localmente."""
        await self._ensure_ready()
        return await asyncio.to_thread(self._list_all_sync)

    def _search_sync(self, query: str, limit: int) -> list[dict]:
        """Calcula coincidencias léxicas sin embeddings ni red."""
        records = self._list_all_sync()
        terms = [term.casefold() for term in re.findall(r"\w+", query, flags=re.UNICODE)]
        if not terms:
            return records[:limit]

        ranked: list[tuple[int, int, dict]] = []
        for index, item in enumerate(records):
            haystack = item["text"].casefold()
            score = sum(haystack.count(term) for term in terms)
            if score:
                ranked.append((score, -index, item))
        ranked.sort(key=lambda value: (value[0], value[1]), reverse=True)
        return [item for _, _, item in ranked[:limit]]

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        """Busca por coincidencia de términos en el texto local."""
        if not isinstance(query, str):
            raise TypeError("query debe ser una cadena")
        if not isinstance(limit, int):
            raise TypeError("limit debe ser un entero")
        if limit < 0:
            raise ValueError("limit no puede ser negativo")
        await self._ensure_ready()
        if limit == 0:
            return []
        return await asyncio.to_thread(self._search_sync, query, limit)

    async def close(self) -> None:
        """Marca el proveedor como cerrado; las conexiones son por operación."""
        self._closed = True


class Mem0Memory:
    """Adaptador HTTP asíncrono para Mem0 Platform sin depender de su SDK."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        user_id: str,
        timeout: float = 30,
        client: httpx.AsyncClient | None = None,
    ):
        """Configura Mem0 sin realizar ninguna llamada de red."""
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key es obligatorio para Mem0Memory")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url es obligatorio para Mem0Memory")
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id es obligatorio para Mem0Memory")
        if timeout <= 0:
            raise ValueError("timeout debe ser positivo")

        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._initialized = False
        self._closed = False

    @property
    def _headers(self) -> dict[str, str]:
        """Construye headers de autenticación sin exponer la clave en logs."""
        return {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "application/json",
        }

    async def initialize(self) -> None:
        """Crea el cliente HTTP de forma perezosa, sin hacer requests."""
        if self._closed:
            raise RuntimeError("el proveedor de memoria está cerrado")
        if self._initialized:
            return
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        self._initialized = True

    async def _ensure_ready(self) -> None:
        """Garantiza que exista un cliente HTTP utilizable."""
        if self._closed:
            raise RuntimeError("el proveedor de memoria está cerrado")
        if not self._initialized:
            await self.initialize()

    async def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        fallback_paths: tuple[str, ...] = (),
        **kwargs: Any,
    ) -> httpx.Response:
        """Ejecuta una request y permite compatibilidad entre versiones de API."""
        await self._ensure_ready()
        if self._client is None:  # pragma: no cover: protege una invariante interna
            raise RuntimeError("cliente HTTP no inicializado")

        candidates = (path_or_url, *fallback_paths)
        last_error: httpx.HTTPStatusError | None = None
        for candidate in candidates:
            url = candidate if candidate.startswith("http") else urljoin(
                f"{self.base_url}/", candidate.lstrip("/")
            )
            target, origin = urlsplit(url), urlsplit(self.base_url)
            if (target.scheme, target.netloc) != (origin.scheme, origin.netloc) or target.username:
                raise ValueError("Mem0 devolvió una URL fuera del proveedor configurado")
            response = await self._client.request(
                method,
                url,
                headers=self._headers,
                **kwargs,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                last_error = error
                if response.status_code == 404 and candidate != candidates[-1]:
                    continue
                raise
            return response
        if last_error is not None:  # pragma: no cover: todos los candidatos fallaron
            raise last_error
        raise RuntimeError("no hay endpoint HTTP configurado")

    @staticmethod
    def _response_json(response: httpx.Response) -> Any:
        """Obtiene JSON y deja que httpx reporte respuestas inválidas."""
        return response.json()

    @staticmethod
    def _normalise_remote(
        raw: Any,
        *,
        fallback_text: str = "",
        fallback_metadata: dict | None = None,
        fallback_conflict_key: str | None = None,
    ) -> dict:
        """Normaliza las variantes de respuesta de Mem0 al contrato Capri."""
        if not isinstance(raw, dict):
            raw = {}
        nested = raw.get("memory")
        if isinstance(nested, dict):
            source = {**raw, **nested}
        else:
            source = raw

        metadata = source.get("metadata")
        if not isinstance(metadata, dict):
            metadata = _metadata_copy(fallback_metadata)
        conflict_key = source.get("conflict_key")
        if conflict_key is None:
            conflict_key = metadata.get("conflict_key", fallback_conflict_key)
        record_id = source.get("id") or source.get("memory_id") or source.get("event_id")
        if record_id is None:
            record_id = ""
        text = source.get("text")
        if text is None:
            value = source.get("memory")
            text = value if isinstance(value, str) else fallback_text
        updated_at = source.get("updated_at") or source.get("created_at") or _utc_now()
        return _record(
            record_id=str(record_id),
            text=str(text),
            metadata=metadata,
            conflict_key=conflict_key,
            updated_at=str(updated_at),
        )

    @classmethod
    def _extract_items(cls, data: Any) -> tuple[list[Any], Any]:
        """Extrae resultados y cursor de las formas de respuesta documentadas."""
        if isinstance(data, list):
            return data, None
        if not isinstance(data, dict):
            return [], None
        items = data.get("results")
        if items is None:
            items = data.get("memories")
        if items is None and isinstance(data.get("data"), list):
            items = data["data"]
        if not isinstance(items, list):
            items = []
        return items, data.get("next")

    async def _remote_update(
        self,
        memory_id: str,
        text: str,
        metadata: dict | None,
    ) -> dict:
        """Actualiza una memoria remota usando el endpoint REST de Mem0."""
        body: dict[str, Any] = {"text": text, "user_id": self.user_id}
        if metadata is not None:
            body["metadata"] = _metadata_copy(metadata)
        response = await self._request(
            "PUT",
            f"/v1/memories/{memory_id}/",
            fallback_paths=(f"/v3/memories/{memory_id}/",),
            json=body,
        )
        return self._normalise_remote(
            self._response_json(response),
            fallback_text=text,
            fallback_metadata=metadata,
        )

    async def update(
        self,
        memory_id: str,
        text: str,
        metadata: dict | None = None,
    ) -> dict:
        """Expone actualización REST para resolver conflictos remotos."""
        if not isinstance(memory_id, str) or not memory_id:
            raise ValueError("memory_id es obligatorio")
        if not isinstance(text, str):
            raise TypeError("text debe ser una cadena")
        _metadata_copy(metadata)
        return await self._remote_update(memory_id, text, metadata)

    async def add(
        self,
        text: str,
        conflict_key: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Agrega un hecho o actualiza el hecho remoto con igual clave."""
        _validate_add_arguments(text, conflict_key)
        supplied_metadata = _metadata_copy(metadata)
        if conflict_key is not None:
            supplied_metadata["conflict_key"] = conflict_key
            existing = await self.list_all()
            current = next(
                (
                    item
                    for item in existing
                    if item.get("conflict_key") == conflict_key
                ),
                None,
            )
            if current is not None and current.get("id"):
                return await self._remote_update(
                    str(current["id"]), text, supplied_metadata
                )

        payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": text}],
            "user_id": self.user_id,
        }
        if supplied_metadata:
            payload["metadata"] = supplied_metadata
        response = await self._request(
            "POST",
            "/v3/memories/add/",
            fallback_paths=("/v1/memories/",),
            json=payload,
        )
        return self._normalise_remote(
            self._response_json(response),
            fallback_text=text,
            fallback_metadata=supplied_metadata,
            fallback_conflict_key=conflict_key,
        )

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        """Busca en Mem0 con filtro obligatorio por user_id."""
        if not isinstance(query, str):
            raise TypeError("query debe ser una cadena")
        if not isinstance(limit, int):
            raise TypeError("limit debe ser un entero")
        if limit < 0:
            raise ValueError("limit no puede ser negativo")
        if limit == 0:
            return []
        response = await self._request(
            "POST",
            "/v3/memories/search/",
            fallback_paths=("/v1/memories/search/",),
            json={
                "query": query,
                "filters": {"user_id": self.user_id},
                "top_k": limit,
            },
        )
        items, _ = self._extract_items(self._response_json(response))
        return [
            self._normalise_remote(item)
            for item in items[:limit]
        ]

    async def list_all(self) -> list[dict]:
        """Exporta todas las memorias remotas siguiendo cada página."""
        page = 1
        page_size = 100
        records: list[dict] = []
        next_url: str | None = None
        seen_cursors: set[str] = set()

        while True:
            if next_url is None:
                response = await self._request(
                    "GET",
                    "/v1/memories/",
                    fallback_paths=("/v3/memories/",),
                    params={
                        "user_id": self.user_id,
                        "page": page,
                        "page_size": page_size,
                    },
                )
            else:
                response = await self._request("GET", next_url)

            data = self._response_json(response)
            items, cursor = self._extract_items(data)
            records.extend(self._normalise_remote(item) for item in items)

            if isinstance(cursor, str) and cursor:
                if cursor in seen_cursors:
                    raise RuntimeError("Mem0 devolvió paginación repetida; no se exportará un respaldo incompleto")
                seen_cursors.add(cursor)
                next_url = cursor
                continue

            count = data.get("count") if isinstance(data, dict) else None
            if isinstance(count, int) and len(records) < count and items:
                page += 1
                next_url = None
                continue
            if isinstance(count, int) and len(records) < count:
                raise RuntimeError("La exportación de Mem0 está incompleta")
            break

        return records

    async def close(self) -> None:
        """Cierra solamente el cliente HTTP creado por este adaptador."""
        if self._closed:
            return
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._closed = True
