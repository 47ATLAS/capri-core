"""Pruebas locales y sin red del gestor financiero de Capri."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import status
from pydantic import ValidationError

from capri_core.mcp_tools.finance_manager import (
    ExpenseInput,
    FinanceManager,
    build_finance_router,
    configure_finance_manager,
    query_recent_expenses,
)


@pytest.mark.parametrize(
    "payload",
    [
        {"amount": 0, "concept": "café"},
        {"amount": -1, "concept": "café"},
        {"amount": "1.001", "concept": "café"},
        {"amount": "1000000000.00", "concept": "café"},
        {"amount": True, "concept": "café"},
        {"amount": "NaN", "concept": "café"},
        {"amount": "Infinity", "concept": "café"},
        {"amount": "1.00", "concept": "   "},
        {"amount": "1.00", "concept": "café", "unexpected": 1},
        {"amount": "1.00", "concept": "café", "category": "   "},
    ],
)
def test_expense_input_rechaza_payload_invalido(payload: dict[str, object]) -> None:
    """Los límites financieros y de texto se validan sin redondear."""
    with pytest.raises(ValidationError):
        ExpenseInput.model_validate(payload)


def test_expense_input_recorta_texto_y_preserva_decimal() -> None:
    """Los campos textuales se recortan y el monto sigue siendo Decimal."""
    expense = ExpenseInput(
        amount=Decimal("10.50"),
        concept="  comida  ",
        category="  hogar ",
    )
    assert expense.amount == Decimal("10.50")
    assert isinstance(expense.amount, Decimal)
    assert expense.concept == "comida"
    assert expense.category == "hogar"


@pytest.mark.asyncio
async def test_add_expense_guarda_centavos_null_y_utc(tmp_path: Path) -> None:
    """Un gasto se guarda como INTEGER, conserva NULL y devuelve dos decimales."""
    manager = FinanceManager(tmp_path / "finance.db")
    await manager.initialize()

    result = await manager.add_expense(
        ExpenseInput(amount=Decimal("123.40"), concept="  transporte ")
    )

    assert result["amount"] == "123.40"
    assert result["currency"] == "MXN"
    assert result["category"] is None
    assert result["needs_category"] is True
    assert result["concept"] == "transporte"
    created_at = datetime.fromisoformat(str(result["created_at"]).replace("Z", "+00:00"))
    assert created_at.utcoffset() is not None
    assert created_at.utcoffset().total_seconds() == 0

    connection = sqlite3.connect(manager.path)
    try:
        row = connection.execute(
            "SELECT amount_cents, category, typeof(amount_cents) FROM expenses WHERE id = ?",
            (result["id"],),
        ).fetchone()
    finally:
        connection.close()
    assert row == (12340, None, "integer")


@pytest.mark.asyncio
async def test_query_orden_descendente_y_herramienta_configurada(tmp_path: Path) -> None:
    """La consulta ordena por id descendente y el punto de entrada global delega."""
    manager = FinanceManager(tmp_path / "finance.db")
    first = await manager.add_expense(ExpenseInput(amount="1.00", concept="uno"))
    second = await manager.add_expense(ExpenseInput(amount="2.00", concept="dos"))
    configure_finance_manager(manager)

    recent = await manager.query_recent_expenses(limit=10)
    through_tool = await query_recent_expenses()

    assert [item["id"] for item in recent] == [second["id"], first["id"]]
    assert [item["id"] for item in through_tool] == [second["id"], first["id"]]


@pytest.mark.asyncio
async def test_categorize_expense_actualiza_categoria(tmp_path: Path) -> None:
    """La herramienta interna categoriza y elimina la marca de pendiente."""
    manager = FinanceManager(tmp_path / "finance.db")
    created = await manager.add_expense(ExpenseInput(amount="80.00", concept="libro"))

    categorized = await manager.categorize_expense(created["id"], "  educación ")

    assert categorized["category"] == "educación"
    assert categorized["needs_category"] is False
    assert categorized["amount"] == "80.00"
    with pytest.raises(ValueError):
        await manager.categorize_expense(999999, "otro")


@pytest.mark.asyncio
async def test_sql_injection_se_guarda_como_dato(tmp_path: Path) -> None:
    """Los conceptos se parametrizan y no pueden alterar el esquema."""
    manager = FinanceManager(tmp_path / "finance.db")
    malicious = "x'); DROP TABLE expenses; --"
    created = await manager.add_expense(ExpenseInput(amount="1.25", concept=malicious))

    recent = await manager.query_recent_expenses()

    assert recent[0]["concept"] == malicious
    assert created["amount"] == "1.25"


@pytest.mark.asyncio
async def test_concurrencia_de_escrituras_aisladas(tmp_path: Path) -> None:
    """Varias escrituras concurrentes conservan todos los ids y el orden."""
    manager = FinanceManager(tmp_path / "finance.db")
    expenses = [
        ExpenseInput(amount=Decimal("1.00"), concept=f"gasto-{index}")
        for index in range(20)
    ]

    results = await asyncio.gather(*(manager.add_expense(expense) for expense in expenses))
    recent = await manager.query_recent_expenses(limit=20)

    ids = [int(result["id"]) for result in results]
    recent_ids = [int(result["id"]) for result in recent]
    assert len(set(ids)) == 20
    assert recent_ids == sorted(ids, reverse=True)


def test_router_solo_expone_post_de_alta() -> None:
    """El router no agrega endpoint de categorización ni autenticación duplicada."""
    manager = FinanceManager(Path("finance.db"))
    router = build_finance_router(manager)

    assert len(router.routes) == 1
    route = router.routes[0]
    assert route.path == "/api/finance/add_expense"
    assert route.methods == {"POST"}
    assert route.status_code == status.HTTP_201_CREATED
