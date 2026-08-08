from __future__ import annotations

import json
from typing import Mapping, Tuple

from ceavpn.config import Settings
from ceavpn.database import Database


def handle_provider_settings_request(
    *,
    settings: Settings,
    db: Database,
    headers: Mapping[str, str],
    body: bytes,
) -> Tuple[int, str, str]:
    return 404, "application/json", '{"ok": false, "error": "not_found"}'


def handle_provider_status_request(
    *,
    settings: Settings,
    db: Database,
    headers: Mapping[str, str],
) -> Tuple[int, str, str]:
    return 404, "application/json", '{"ok": false, "error": "not_found"}'
