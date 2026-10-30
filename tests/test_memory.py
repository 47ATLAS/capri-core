"""Pruebas del contrato de memoria local y remoto."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from capri_core.memory.mem0_client import LocalMemory, Mem0Memory


@pytest.mark.asyncio
async def test_local_memory_resuelve_conflictos_y_persiste_historial(tmp_path):
    """Una clave conflictiva conserva un solo hecho activo y sus versiones."""
    database = tmp_path / "memory.sqlite3"
    memory = LocalMemory(database)

    first = await memory.add(
        "Vivo en CDMX",
        conflict_key="ubicacion",
        metadata={"source": "test"},
    )
    second = await memory.add(
        "Vivo en Azcapotzalco",
        conflict_key="ubicacion",
        metadata={"source": "update"},
    )

    assert second["id"] == first["id"]
    records = await memory.list_all()
    assert len(records) == 1
    assert records[0]["text"] == "Vivo en Azcapotzalco"
    assert records[0]["metadata"]["versions"][0]["text"] == "Vivo en CDMX"
    assert await memory.search("Azcapotzalco") == records

    reopened = LocalMemory(database)
    assert await reopened.list_all() == records
    await memory.close()
    await reopened.close()


@pytest.mark.asyncio
async def test_mem0_http_real_normaliza_filtros_y_paginacion():
    """El adaptador usa rutas REST, auth Token y exporta todas las páginas."""
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Token test-key"
        if request.url.path == "/v3/memories/add/":
            body = json.loads(request.content)
            assert body["user_id"] == "user-1"
            assert body["messages"][0]["content"] == "Prefiero vim"
            return httpx.Response(
                200,
                json={
                    "id": "m-added",
                    "memory": "Prefiero vim",
                    "metadata": {"kind": "preference"},
                    "updated_at": "2026-01-01T00:00:00+00:00",
                },
            )
        if request.url.path == "/v3/memories/search/":
            body = json.loads(request.content)
            assert body["filters"] == {"user_id": "user-1"}
            assert body["top_k"] == 2
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "m-search",
                            "memory": "Prefiero vim",
                            "metadata": {},
                            "updated_at": "2026-01-01T00:00:00+00:00",
                        }
                    ]
                },
            )
        if request.url.path == "/v1/memories/":
            page = int(parse_qs(request.url.query.decode())["page"][0])
            assert parse_qs(request.url.query.decode())["user_id"] == ["user-1"]
            result = {
                "id": f"m-{page}",
                "memory": f"Hecho {page}",
                "metadata": {"page": page},
                "updated_at": f"2026-01-0{page}T00:00:00+00:00",
            }
            if page == 1:
                return httpx.Response(
                    200,
                    json={"count": 2, "next": None, "results": [result]},
                )
            return httpx.Response(
                200,
                json={"count": 2, "next": None, "results": [result]},
            )
        return httpx.Response(404, json={"detail": "not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    memory = Mem0Memory(
        api_key="test-key",
        base_url="https://api.mem0.ai",
        user_id="user-1",
        client=client,
    )
    await memory.initialize()
    assert requests == []

    added = await memory.add("Prefiero vim", metadata={"kind": "preference"})
    found = await memory.search("editor", limit=2)
    all_records = await memory.list_all()

    assert added["id"] == "m-added"
    assert found[0]["text"] == "Prefiero vim"
    assert [record["id"] for record in all_records] == ["m-1", "m-2"]
    assert sum(request.url.path == "/v1/memories/" for request in requests) == 2
    await memory.close()
    await client.aclose()
