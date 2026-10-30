"""Pruebas offline: cualquier transporte HTTP real se considera un error."""
import socket

import httpx
import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Los tests no pueden usar red real.")
    async def blocked_async(*args, **kwargs):
        raise AssertionError("Los tests no pueden usar red real.")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)
    monkeypatch.setattr(socket, "create_connection", blocked)
