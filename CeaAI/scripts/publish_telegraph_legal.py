#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = ROOT / ".telegraph-access-token"


def api_call(method: str, **params: Any) -> dict[str, Any]:
    body = urllib.parse.urlencode(params).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegra.ph/{method}", data=body, method="POST"
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not payload.get("ok"):
        raise RuntimeError(f"Telegraph API: {payload.get('error', 'unknown error')}")
    return payload["result"]


def load_token() -> str:
    token = os.getenv("TELEGRAPH_ACCESS_TOKEN", "").strip()
    if token:
        return token
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    account = api_call(
        "createAccount",
        short_name="ceaai",
        author_name="Cea AI",
        author_url="https://t.me/ceafamily",
    )
    token = str(account["access_token"])
    TOKEN_FILE.write_text(token, encoding="utf-8")
    TOKEN_FILE.chmod(0o600)
    return token


def render_document(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    unresolved = sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", text)))
    if unresolved:
        raise RuntimeError(f"Unresolved placeholders in {path.name}: {unresolved}")
    return text


def inline_nodes(text: str) -> list[Any]:
    nodes: list[Any] = []
    parts = re.split(r"(https?://\S+|@[A-Za-z0-9_]+|\*\*[^*]+\*\*)", text)
    for part in parts:
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            nodes.append({"tag": "strong", "children": [part[2:-2]]})
        elif part.startswith("http://") or part.startswith("https://"):
            url = part.rstrip(".,)")
            suffix = part[len(url) :]
            nodes.append({"tag": "a", "attrs": {"href": url}, "children": [url]})
            if suffix:
                nodes.append(suffix)
        elif part.startswith("@"):
            nodes.append(
                {
                    "tag": "a",
                    "attrs": {"href": f"https://t.me/{part[1:]}"},
                    "children": [part],
                }
            )
        else:
            nodes.append(part)
    return nodes


def markdown_to_telegraph(text: str) -> tuple[str, list[dict[str, Any]]]:
    lines = text.splitlines()
    title = lines[0].removeprefix("# ").strip()
    content: list[dict[str, Any]] = []
    list_items: list[dict[str, Any]] = []

    def flush_list() -> None:
        if list_items:
            content.append({"tag": "ul", "children": list_items.copy()})
            list_items.clear()

    for raw in lines[1:]:
        line = raw.strip()
        if not line:
            flush_list()
            continue
        if line.startswith("## "):
            flush_list()
            content.append({"tag": "h3", "children": [line[3:]]})
        elif line.startswith("- "):
            list_items.append({"tag": "li", "children": inline_nodes(line[2:])})
        else:
            flush_list()
            content.append({"tag": "p", "children": inline_nodes(line)})
    flush_list()
    return title, content


def publish(path: Path, token: str) -> str:
    title, content = markdown_to_telegraph(render_document(path))
    page = api_call(
        "createPage",
        access_token=token,
        title=title,
        author_name="Cea AI",
        author_url="https://t.me/ceafamily",
        content=json.dumps(content, ensure_ascii=False),
    )
    return str(page["url"])


def main() -> int:
    try:
        token = load_token()
        offer_url = publish(ROOT / "docs/legal/public_offer.md", token)
        privacy_url = publish(ROOT / "docs/legal/privacy_policy.md", token)
    except Exception as exc:
        print(f"Publication failed: {exc}", file=sys.stderr)
        return 1
    print(f"PUBLIC_OFFER_URL={offer_url}")
    print(f"PRIVACY_POLICY_URL={privacy_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
