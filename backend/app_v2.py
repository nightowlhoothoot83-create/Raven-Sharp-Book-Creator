"""Raven Sharp Book Creator v2 extensions.

Adds the missing generic-brand workflow without rewriting the stable legacy API:
- upload a PDF/DOCX/TXT/MD/JSON brand bible or blueprint
- persist the extracted guidance into the existing brand profile
- create a short-lived, one-time handoff package for Raven Sharp Video Creator
"""
from __future__ import annotations

import base64
import io
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import server as legacy

app = legacy.app
v2 = APIRouter(prefix="/api/v2", tags=["v2"])

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_BIBLE_CHARS = 60_000
VIDEO_CREATOR_URL = os.environ.get("VIDEO_CREATOR_URL", "https://content.raven-sharp.com")


class BrandDocumentUploadIn(BaseModel):
    filename: str
    mime: str = "application/octet-stream"
    content_base64: str
    append: bool = True


def _extract_document_text(data: bytes, filename: str, mime: str) -> str:
    name = (filename or "").lower()
    mime = (mime or "").lower()
    try:
        if name.endswith(".pdf") or mime == "application/pdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            return "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
        if name.endswith(".docx") or "wordprocessingml" in mime:
            from docx import Document
            doc = Document(io.BytesIO(data))
            return "\n".join(p.text.strip() for p in doc.paragraphs if p.text.strip()).strip()
        if name.endswith(".json") or mime == "application/json":
            obj = json.loads(data.decode("utf-8", errors="replace"))
            return json.dumps(obj, ensure_ascii=False, indent=2)
        if name.endswith((".txt", ".md", ".markdown")) or mime.startswith("text/"):
            return data.decode("utf-8", errors="replace").strip()
    except Exception as exc:
        raise HTTPException(400, f"Could not read brand document: {exc}")
    raise HTTPException(400, "Unsupported brand document. Use PDF, DOCX, TXT, MD or JSON.")


@v2.post("/brand-profiles/{profile_id}/upload-document")
async def upload_brand_document(
    profile_id: str,
    payload: BrandDocumentUploadIn,
    user: dict = Depends(legacy.get_user),
):
    profile = await legacy.db.brand_profiles.find_one(
        {"id": profile_id, "user_id": user["id"]}, {"_id": 0}
    )
    if not profile:
        raise HTTPException(404, "Brand profile not found")

    try:
        raw = base64.b64decode(payload.content_base64, validate=True)
    except Exception:
        raise HTTPException(400, "Invalid base64 document data")
    if not raw:
        raise HTTPException(400, "Brand document is empty")
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise HTTPException(413, "Brand document is too large (10 MB maximum)")

    extracted = _extract_document_text(raw, payload.filename, payload.mime)
    if not extracted:
        raise HTTPException(422, "No readable text was found in the brand document")

    extracted_for_prompt = extracted[:MAX_BIBLE_CHARS]
    existing = (profile.get("brand_bible") or "").strip()
    section = f"SOURCE DOCUMENT: {payload.filename}\n{extracted_for_prompt}"
    brand_bible = f"{existing}\n\n{section}".strip() if payload.append and existing else section
    brand_bible = brand_bible[-MAX_BIBLE_CHARS:]

    url = ""
    try:
        url = await legacy.upload_to_r2(
            raw,
            f"book-creator-brand-documents/{user['id']}/{profile_id}",
            payload.filename,
            payload.mime,
        )
    except Exception:
        legacy.log.exception("Brand document R2 upload failed; extracted text will still be saved")

    document_record = {
        "filename": payload.filename,
        "mime": payload.mime,
        "url": url,
        "bytes": len(raw),
        "extracted_chars": len(extracted),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    await legacy.db.brand_profiles.update_one(
        {"id": profile_id, "user_id": user["id"]},
        {
            "$set": {"brand_bible": brand_bible, "updated_at": datetime.now(timezone.utc).isoformat()},
            "$push": {"source_documents": document_record},
        },
    )
    return {
        "ok": True,
        "profile_id": profile_id,
        "document": document_record,
        "brand_bible_chars": len(brand_bible),
        "truncated": len(extracted) > MAX_BIBLE_CHARS,
    }


@v2.post("/books/{book_id}/video-handoff")
async def create_video_handoff(book_id: str, user: dict = Depends(legacy.get_user)):
    book = await legacy.db.books.find_one(
        {"id": book_id, "user_id": user["id"]}, {"_id": 0}
    )
    if not book:
        raise HTTPException(404, "Book not found")

    profile = None
    if book.get("brand_profile_id"):
        profile = await legacy.db.brand_profiles.find_one(
            {"id": book["brand_profile_id"], "user_id": user["id"]}, {"_id": 0}
        )

    pages = book.get("pages") or []
    bundle = {
        "source": "raven_sharp_book_creator",
        "book_id": book["id"],
        "title": book.get("title") or "Untitled Book",
        "suggested_project_title": f"{book.get('title') or 'Untitled Book'} — Video",
        "pages": [
            {
                "page": i + 1,
                "text": p.get("text", ""),
                "image_url": p.get("image_url"),
            }
            for i, p in enumerate(pages)
        ],
        "reference_images": [p.get("image_url") for p in pages if p.get("image_url")],
        "brand": {
            "name": (profile or {}).get("name", ""),
            "brand_bible": (profile or {}).get("brand_bible", ""),
            "primary_color": (profile or {}).get("primary_color"),
            "secondary_color": (profile or {}).get("secondary_color"),
            "logo_url": (profile or {}).get("logo_url"),
            "characters": (profile or {}).get("characters", []),
        } if profile else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    await legacy.db.video_handoffs.insert_one({
        "token": token,
        "user_id": user["id"],
        "bundle": bundle,
        "created_at": datetime.now(timezone.utc),
        "expires_at": expires_at,
        "redeemed_at": None,
    })
    target = f"{VIDEO_CREATOR_URL.rstrip('/')}?book_handoff={quote(token)}"
    return {"ok": True, "token": token, "expires_at": expires_at.isoformat(), "target_url": target}


@v2.get("/handoffs/{token}")
async def redeem_video_handoff(token: str):
    now = datetime.now(timezone.utc)
    handoff = await legacy.db.video_handoffs.find_one({"token": token})
    if not handoff:
        raise HTTPException(404, "Handoff not found")
    expires = handoff.get("expires_at")
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires and expires < now:
        raise HTTPException(410, "Handoff expired")
    if handoff.get("redeemed_at"):
        raise HTTPException(410, "Handoff has already been used")
    await legacy.db.video_handoffs.update_one({"_id": handoff["_id"]}, {"$set": {"redeemed_at": now}})
    return handoff["bundle"]


@v2.get("/capabilities")
async def v2_capabilities():
    return {
        "generic_brand_profiles": True,
        "brand_document_upload": ["pdf", "docx", "txt", "md", "json"],
        "reference_image_upload": True,
        "book_to_video_handoff": True,
    }


app.include_router(v2)
