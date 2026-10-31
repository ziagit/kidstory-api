"""A minimal in-memory stand-in for the Supabase client.

Only the query surface the app actually uses is implemented — enough to exercise
routing, validation and the slug-collision retry without a
network round-trip or real credentials.
"""

import re
from typing import Any

from postgrest.exceptions import APIError


class FakeResponse:
    def __init__(self, data: list[dict[str, Any]], count: int | None = None) -> None:
        self.data = data
        self.count = count


class FakeSelect:
    def __init__(self, rows: list[dict[str, Any]], with_count: bool) -> None:
        self._rows = rows
        self._with_count = with_count
        self._limit: int | None = None
        self._range: tuple[int, int] | None = None

    def eq(self, column: str, value: Any) -> "FakeSelect":
        self._rows = [row for row in self._rows if row.get(column) == value]
        return self

    def contains(self, column: str, values: list[Any]) -> "FakeSelect":
        self._rows = [row for row in self._rows if set(values).issubset(set(row.get(column) or []))]
        return self

    def or_(self, expression: str) -> "FakeSelect":
        terms = re.findall(r'ilike\."\*(.*?)\*"', expression)
        term = terms[0].lower() if terms else ""
        self._rows = [
            row
            for row in self._rows
            if term in row.get("title", "").lower() or term in row.get("excerpt", "").lower()
        ]
        return self

    def order(self, column: str, desc: bool = False) -> "FakeSelect":
        self._rows = sorted(self._rows, key=lambda row: row.get(column) or "", reverse=desc)
        return self

    def limit(self, count: int) -> "FakeSelect":
        self._limit = count
        return self

    def range(self, start: int, end: int) -> "FakeSelect":
        self._range = (start, end)
        return self

    def execute(self) -> FakeResponse:
        total = len(self._rows)
        rows = self._rows
        if self._range:
            start, end = self._range
            rows = rows[start : end + 1]
        elif self._limit is not None:
            rows = rows[: self._limit]
        return FakeResponse([dict(row) for row in rows], total if self._with_count else None)


class FakeWrite:
    def __init__(self, rows: list[dict[str, Any]], op: str, payload: dict[str, Any] | None = None) -> None:
        self._rows = rows
        self._op = op
        self._payload = payload or {}
        self._filters: list[tuple[str, Any]] = []

    def eq(self, column: str, value: Any) -> "FakeWrite":
        self._filters.append((column, value))
        return self

    def _matches(self, row: dict[str, Any]) -> bool:
        return all(row.get(column) == value for column, value in self._filters)

    def execute(self) -> FakeResponse:
        affected = [row for row in self._rows if self._matches(row)]
        if self._op == "update":
            for row in affected:
                row.update(self._payload)
        elif self._op == "delete":
            for row in affected:
                self._rows.remove(row)
        return FakeResponse([dict(row) for row in affected])


class FakeTable:
    def __init__(self, store: dict[str, list[dict[str, Any]]], name: str) -> None:
        self._store = store
        self._rows = store.setdefault(name, [])
        self._name = name

    def select(self, columns: str = "*", count: str | None = None) -> FakeSelect:
        return FakeSelect(self._rows, with_count=count is not None)

    def insert(self, record: dict[str, Any]) -> FakeWrite:
        if any(row.get("slug") == record.get("slug") for row in self._rows):
            raise APIError({"message": "duplicate key value", "code": "23505", "hint": None, "details": None})
        stored = dict(record)
        self._rows.append(stored)
        return FakeWrite([stored], "insert")

    def update(self, values: dict[str, Any]) -> FakeWrite:
        return FakeWrite(self._rows, "update", values)

    def delete(self) -> FakeWrite:
        return FakeWrite(self._rows, "delete")


class FakeAuth:
    """Maps opaque test tokens to user ids; anything unknown is a rejected token."""

    def __init__(self, tokens: dict[str, str]) -> None:
        self._tokens = tokens

    def get_user(self, token: str) -> Any:
        user_id = self._tokens.get(token)
        if not user_id:
            raise ValueError("invalid token")
        return type("UserResponse", (), {"user": type("User", (), {"id": user_id})()})()


class FakeClient:
    def __init__(self, store: dict[str, list[dict[str, Any]]], tokens: dict[str, str]) -> None:
        self._store = store
        self.auth = FakeAuth(tokens)

    def table(self, name: str) -> FakeTable:
        return FakeTable(self._store, name)
