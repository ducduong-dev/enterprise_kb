"""ASGI entrypoint for kb-idp.

IDP turns any source format into KBDoc. Internal-only; called by Temporal activities and, for
reprocessing, by portal-api. It holds no ACL logic and never reaches an index — it sees
document bytes and returns structure.
"""

from __future__ import annotations

from fastapi import FastAPI, File, UploadFile
from kb_common.app import create_app
from kb_schemas.kbdoc import KBDoc
from pydantic import BaseModel

from kb_idp.parsers import PARSERS
from kb_idp.service import process

app: FastAPI = create_app("kb-idp")


class ParseResponse(BaseModel):
    requires_ocr: bool
    reason: str | None = None
    kbdoc: KBDoc | None = None


@app.get("/v1/formats")
async def formats() -> dict[str, list[str]]:
    return {"digital_native": sorted(PARSERS), "ocr_required": ["pdf_scanned", "image"]}


@app.post("/v1/parse", response_model=ParseResponse)
async def parse(file: UploadFile = File(...)) -> ParseResponse:
    data = await file.read()
    result = process(data, file.filename)
    return ParseResponse(
        requires_ocr=result.requires_ocr,
        reason=result.reason,
        kbdoc=None if result.requires_ocr else result.kbdoc,
    )


def run() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
