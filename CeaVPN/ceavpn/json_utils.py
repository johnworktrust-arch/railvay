from __future__ import annotations

import json
from typing import Any, Dict


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads_dict(value: Any) -> Dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    loaded = json.loads(value)
    if isinstance(loaded, dict):
        return loaded
    return {}
