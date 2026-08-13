"""ASGI entrypoint for kb-identity-merge.

Legal-number identity resolution and merge drafting.
"""

from __future__ import annotations

from fastapi import FastAPI
from kb_common.app import create_app

app: FastAPI = create_app("kb-identity-merge")


def run() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
