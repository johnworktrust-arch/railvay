from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Dict, Iterable, List


def _normalize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def row_to_dict(row: sqlite3.Row | Dict[str, Any] | None) -> Dict[str, Any] | None:
    if row is None:
        return None
    return {key: _normalize(value) for key, value in dict(row).items()}


def rows_to_dicts(rows: Iterable[sqlite3.Row | Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [row_to_dict(row) or {} for row in rows]
