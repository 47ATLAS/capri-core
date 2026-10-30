"""Gestión local de gastos para Capri, sin enviar datos financieros a terceros."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, field_validator


_MAX_AMOUNT = Decimal("999999999.99")
_CENTS = Decimal("100")
_BUSY_TIMEOUT_MS = 5_000


class ExpenseInput(BaseModel):
    """Payload validado para registrar un gasto."""

    amount: Decimal
    concept: str
    category: str | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("amount", mode="before")
    @classmethod
    def validate_amount(cls, value: Any) -> Decimal:
        """Validar monto finito, positivo y representable exactamente en centavos."""
        if isinstance(value, bool):
            raise ValueError("amount debe ser un número decimal, no booleano")
        if not isinstance(value, (Decimal, int, str)):
            raise ValueError("amount debe ser decimal, entero o texto numérico")
        try:
            decimal_value = Decimal(value)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("amount debe ser un número decimal válido") from exc
        if not decimal_value.is_finite():
            raise ValueError("amount debe ser finito")
        if decimal_value <= 0:
            raise ValueError("amount debe ser positivo")
        if decimal_value > _MAX_AMOUNT:
            raise ValueError("amount excede el máximo permitido")
        if decimal_value.as_tuple().exponent < -2:
            raise ValueError("amount no puede tener más de dos decimales")
        return decimal_value

    @field_validator("concept", mode="before")
    @classmethod
    def validate_concept(cls, value: Any) -> str:
        """Recortar y validar el concepto del gasto."""
        if not isinstance(value, str):
            raise ValueError("concept debe ser texto")
        concept = value.strip()
        if not 1 <= len(concept) <= 240:
            raise ValueError("concept debe tener entre 1 y 240 caracteres")
        return concept

    @field_validator("category", mode="before")
    @classmethod
    def validate_category(cls, value: Any) -> str | None:
        """Recortar y validar la categoría opcional."""
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("category debe ser texto o null")
        category = value.strip()
        if not 1 <= len(category) <= 80:
            raise ValueError("category debe tener entre 1 y 80 caracteres")
        return category


class FinanceManager:
    """Persistir gastos en SQLite local usando montos enteros en centavos."""

    def __init__(self, path: Path) -> None:
        """Crear un gestor asociado a la ruta SQLite indicada."""
        self.path = Path(path)
        # db_path es un alias útil para consumidores existentes, sin abrir conexiones.
        self.db_path = self.path
        self._initialized = False
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Crear la tabla de gastos de forma segura y sin bloquear el event loop."""
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    async def add_expense(self, expense: ExpenseInput) -> dict[str, Any]:
        """Guardar un gasto y devolver su representación pública normalizada."""
        await self.initialize()
        validated = expense if isinstance(expense, ExpenseInput) else ExpenseInput.model_validate(expense)
        return await asyncio.to_thread(self._add_expense_sync, validated)

    async def query_recent_expenses(self, limit: int = 10) -> list[dict[str, Any]]:
        """Consultar gastos recientes ordenados por identificador descendente."""
        _validate_limit(limit)
        await self.initialize()
        return await asyncio.to_thread(self._query_recent_expenses_sync, limit)

    async def categorize_expense(self, expense_id: int, category: str) -> dict[str, Any]:
        """Asignar una categoría a un gasto existente; no expone un endpoint adicional."""
        _validate_expense_id(expense_id)
        clean_category = _validate_category_value(category)
        await self.initialize()
        return await asyncio.to_thread(
            self._categorize_expense_sync,
            expense_id,
            clean_category,
        )

    def _initialize_sync(self) -> None:
        """Crear el esquema en un hilo trabajador."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = _connect(self.path)
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS expenses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    amount_cents INTEGER NOT NULL
                        CHECK (amount_cents > 0 AND amount_cents <= 99999999999),
                    concept TEXT NOT NULL,
                    category TEXT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

    def _add_expense_sync(self, expense: ExpenseInput) -> dict[str, Any]:
        """Insertar un gasto usando parámetros SQLite y devolver la fila insertada."""
        amount_cents = int(expense.amount * _CENTS)
        created_at = _utc_now()
        connection = _connect(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO expenses (amount_cents, concept, category, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (amount_cents, expense.concept, expense.category, created_at),
            )
            expense_id = int(cursor.lastrowid)
            row = connection.execute(
                """
                SELECT id, amount_cents, concept, category, created_at
                FROM expenses
                WHERE id = ?
                """,
                (expense_id,),
            ).fetchone()
            connection.commit()
            if row is None:
                raise RuntimeError("No se pudo leer el gasto recién insertado")
            return _row_to_public_dict(row)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _query_recent_expenses_sync(self, limit: int) -> list[dict[str, Any]]:
        """Leer gastos recientes dentro de una conexión aislada."""
        connection = _connect(self.path)
        try:
            rows = connection.execute(
                """
                SELECT id, amount_cents, concept, category, created_at
                FROM expenses
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [_row_to_public_dict(row) for row in rows]
        finally:
            connection.close()

    def _categorize_expense_sync(self, expense_id: int, category: str) -> dict[str, Any]:
        """Actualizar una categoría dentro de una transacción parametrizada."""
        connection = _connect(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE expenses SET category = ? WHERE id = ?",
                (category, expense_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise ValueError("No existe el gasto indicado")
            row = connection.execute(
                """
                SELECT id, amount_cents, concept, category, created_at
                FROM expenses
                WHERE id = ?
                """,
                (expense_id,),
            ).fetchone()
            connection.commit()
            if row is None:
                raise RuntimeError("No se pudo leer el gasto categorizado")
            return _row_to_public_dict(row)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def build_finance_router(manager: FinanceManager) -> APIRouter:
    """Construir la única ruta financiera; la autenticación vive en el padre."""
    if not isinstance(manager, FinanceManager):
        raise TypeError("manager debe ser una instancia de FinanceManager")

    router = APIRouter()

    @router.post("/api/finance/add_expense", status_code=status.HTTP_201_CREATED)
    async def add_expense(expense: ExpenseInput) -> dict[str, Any]:
        """Registrar un gasto validado mediante la protección global del servidor."""
        return await manager.add_expense(expense)

    return router


_configured_manager: FinanceManager | None = None


def configure_finance_manager(manager: FinanceManager) -> None:
    """Enlazar el gestor usado por la herramienta interna de consulta."""
    if not isinstance(manager, FinanceManager):
        raise TypeError("manager debe ser una instancia de FinanceManager")
    global _configured_manager
    _configured_manager = manager


async def query_recent_expenses(limit: int = 10) -> list[dict[str, Any]]:
    """Consultar gastos mediante el punto de entrada de herramienta del SDD."""
    if _configured_manager is None:
        raise RuntimeError("El gestor financiero no está configurado")
    return await _configured_manager.query_recent_expenses(limit)


def _connect(path: Path) -> sqlite3.Connection:
    """Abrir una conexión nueva y configurar espera ante bloqueos SQLite."""
    connection = sqlite3.connect(str(path), timeout=_BUSY_TIMEOUT_MS / 1000)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _utc_now() -> str:
    """Generar una marca ISO-8601 explícitamente UTC."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _row_to_public_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convertir una fila SQLite a la forma pública del contrato financiero."""
    amount = Decimal(int(row["amount_cents"])) / _CENTS
    category = row["category"]
    return {
        "id": int(row["id"]),
        "amount": f"{amount:.2f}",
        "currency": "MXN",
        "concept": str(row["concept"]),
        "category": category if category is None else str(category),
        "needs_category": category is None,
        "created_at": str(row["created_at"]),
    }


def _validate_limit(limit: int) -> None:
    """Validar el límite entero de una consulta."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit debe ser un entero")
    if limit < 0:
        raise ValueError("limit no puede ser negativo")


def _validate_expense_id(expense_id: int) -> None:
    """Validar un identificador SQLite positivo."""
    if isinstance(expense_id, bool) or not isinstance(expense_id, int):
        raise TypeError("expense_id debe ser un entero")
    if expense_id <= 0:
        raise ValueError("expense_id debe ser positivo")


def _validate_category_value(category: str) -> str:
    """Normalizar una categoría usada por la herramienta interna."""
    if not isinstance(category, str):
        raise ValueError("category debe ser texto")
    clean_category = category.strip()
    if not 1 <= len(clean_category) <= 80:
        raise ValueError("category debe tener entre 1 y 80 caracteres")
    return clean_category


__all__ = [
    "ExpenseInput",
    "FinanceManager",
    "build_finance_router",
    "configure_finance_manager",
    "query_recent_expenses",
]
