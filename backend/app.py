"""Extended Raven Sharp Book Creator API.

This module keeps the existing server intact and mounts the print-finishing,
cover, barcode, publishing-package, and owner-log infrastructure around it.
Railway runs ``uvicorn app:app`` from the backend directory.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from fastapi import APIRouter, Depends, HTTPException

import server
from cover_specs import (
    BINDINGS,
    COVER_FINISHES,
    PRINTER_PROFILES,
    PROFILE_REVIEW_DATE,
    barcode_spec,
    cover_spec,
    preflight_cover,
    validate_isbn13,
)
from print_specs import (
    FINISH_LEVELS,
    PAPER_TYPES,
    TRIM_PRESETS,
    build_export_spec,
    preflight_book,
)
from publishing import PLATFORMS, build_manifest, new_job

app = server.app
db = server.db
OWNER_EMAIL = server.OWNER_EMAIL
router = APIRouter(prefix="/api")
APP_SLUG = "book-creator"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_document(document: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not document:
        return document
    document.pop("_id", None)
    return document


async def _record_event(
    event_type: str,
    user: Mapping[str, Any],
    status: str = "ok",
    details: Optional[Mapping[str, Any]] = None,
) -> None:
    event = {
        "id": __import__("uuid").uuid4().hex,
        "app_slug": APP_SLUG,
        "event_type": event_type,
        "status": status,
        "user_id": user.get("id"),
        "user_email": user.get("email"),
        "details": dict(details or {}),
        "created_at": _utcnow(),
    }
    try:
        await db.system_events.insert_one(event)
    except Exception as exc:  # logging must never break a customer export
        server.log.error("Could not record system event %s: %s", event_type, exc)


def _is_owner(user: Mapping[str, Any]) -> bool:
    return user.get("tier") == "owner" or str(user.get("email", "")).lower() == OWNER_EMAIL.lower()


def _page_images(book: Mapping[str, Any]) -> List[Dict[str, Any]]:
    images: List[Dict[str, Any]] = []
    for page in book.get("pages") or []:
        if not isinstance(page, Mapping):
            images.append({"present": False, "width": 0, "height": 0})
            continue
        images.append(
            {
                "present": bool(page.get("image_url") or page.get("imageB64") or page.get("image_base64")),
                "width": page.get("image_width") or page.get("width") or 0,
                "height": page.get("image_height") or page.get("height") or 0,
            }
        )
    return images


def _book_preflight_payload(book: Mapping[str, Any], payload: Mapping[str, Any]) -> Dict[str, Any]:
    merged = dict(payload)
    merged.setdefault("title", book.get("title"))
    merged.setdefault("author", book.get("author"))
    merged.setdefault("description", book.get("description"))
    merged.setdefault("keywords", book.get("keywords") or [])
    merged.setdefault("page_count", len(book.get("pages") or []))
    merged.setdefault("cover_present", bool(book.get("cover_url") or book.get("coverB64") or book.get("cover_base64")))
    merged.setdefault("images", _page_images(book))
    return merged


def _combine_preflight(*reports: Mapping[str, Any]) -> Dict[str, Any]:
    issues: List[Dict[str, Any]] = []
    errors = 0
    warnings = 0
    for report in reports:
        issues.extend(list(report.get("issues") or []))
        errors += int(report.get("errors") or 0)
        warnings += int(report.get("warnings") or 0)
    status = "blocked" if errors else ("review" if warnings else "ready")
    return {
        "status": status,
        "ready": status == "ready",
        "errors": errors,
        "warnings": warnings,
        "issues": issues,
    }


@router.get("/finishing/options")
async def finishing_options() -> Dict[str, Any]:
    """Public options used to build the Finish, Covers, and Barcode section."""
    return {
        "finish_levels": FINISH_LEVELS,
        "trim_presets": TRIM_PRESETS,
        "paper_types": PAPER_TYPES,
        "publishing_platforms": PLATFORMS,
        "printer_profiles": PRINTER_PROFILES,
        "bindings": BINDINGS,
        "cover_finishes": COVER_FINISHES,
        "printer_profile_review_date": PROFILE_REVIEW_DATE,
    }


@router.post("/finishing/spec")
async def finishing_spec(payload: Dict[str, Any], user: Dict[str, Any] = Depends(server.get_user)) -> Dict[str, Any]:
    try:
        spec = build_export_spec(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await _record_event("export_spec_created", user, details={"finish_level": spec["finish_level"], "trim_key": spec["trim_key"]})
    return spec


@router.post("/finishing/preflight")
async def finishing_preflight(payload: Dict[str, Any], user: Dict[str, Any] = Depends(server.get_user)) -> Dict[str, Any]:
    try:
        report = preflight_book(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await _record_event(
        "print_preflight",
        user,
        status=report["status"],
        details={"finish_level": payload.get("finish_level"), "errors": report["errors"], "warnings": report["warnings"]},
    )
    return report


@router.get("/cover/options")
async def cover_options() -> Dict[str, Any]:
    return {
        "printer_profiles": PRINTER_PROFILES,
        "bindings": BINDINGS,
        "cover_finishes": COVER_FINISHES,
        "profile_review_date": PROFILE_REVIEW_DATE,
    }


@router.post("/cover/spec")
async def calculate_cover_spec(payload: Dict[str, Any], user: Dict[str, Any] = Depends(server.get_user)) -> Dict[str, Any]:
    try:
        spec = cover_spec(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await _record_event(
        "cover_spec_created",
        user,
        details={"printer_profile": spec["printer_profile"], "binding": spec["binding"], "barcode_mode": spec["barcode"]["mode"]},
    )
    return spec


@router.post("/cover/preflight")
async def calculate_cover_preflight(payload: Dict[str, Any], user: Dict[str, Any] = Depends(server.get_user)) -> Dict[str, Any]:
    try:
        report = preflight_cover(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await _record_event(
        "cover_preflight",
        user,
        status=report["status"],
        details={
            "printer_profile": payload.get("printer_profile"),
            "errors": report["errors"],
            "warnings": report["warnings"],
        },
    )
    return report


@router.post("/barcode/validate")
async def validate_barcode(payload: Dict[str, Any], user: Dict[str, Any] = Depends(server.get_user)) -> Dict[str, Any]:
    result = validate_isbn13(payload.get("isbn"))
    result["barcode"] = barcode_spec(payload)
    await _record_event("isbn_validated", user, status="ok" if result["valid"] else "error", details={"valid": result["valid"]})
    return result


async def _build_manifest_for_user(
    book_id: str,
    payload: Mapping[str, Any],
    user: Mapping[str, Any],
) -> Dict[str, Any]:
    book = await db.books.find_one({"id": book_id, "user_id": user["id"]}, {"_id": 0})
    if not book:
        raise HTTPException(404, "Book not found")

    platform = str(payload.get("platform") or "")
    if platform not in PLATFORMS:
        raise HTTPException(400, f"Unsupported publishing platform: {platform}")

    finish_payload = _book_preflight_payload(book, payload)
    finish_payload["finish_level"] = PLATFORMS[platform]["finish_level"]
    if finish_payload["finish_level"] == "google_play":
        finish_payload["finish_level"] = "digital_pdf"

    try:
        book_report = preflight_book(finish_payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    reports: List[Mapping[str, Any]] = [book_report]
    if platform.startswith("amazon_kdp") or platform == "generic_print":
        cover_payload = dict(payload.get("cover") or {})
        cover_payload.setdefault("printer_profile", "amazon_kdp" if platform.startswith("amazon_kdp") else "local_commercial_printer")
        cover_payload.setdefault("isbn", payload.get("isbn") or book.get("isbn"))
        cover_payload.setdefault("front_cover_present", finish_payload["cover_present"])
        cover_payload.setdefault("back_cover_present", bool(book.get("back_cover_url") or payload.get("back_cover_present")))
        cover_payload.setdefault("trim_width_in", book_report["spec"]["trim"]["width_in"])
        cover_payload.setdefault("trim_height_in", book_report["spec"]["trim"]["height_in"])
        cover_payload.setdefault("spine_width_in", (book_report["spec"].get("cover") or {}).get("spine_width_in", 0))
        try:
            reports.append(preflight_cover(cover_payload))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    combined = _combine_preflight(*reports)
    manifest = build_manifest(
        platform,
        book,
        payload,
        files=payload.get("files") or {},
        preflight=combined,
    )
    manifest["book_preflight"] = book_report
    return manifest


@router.post("/publishing/manifest")
async def create_publishing_manifest(payload: Dict[str, Any], user: Dict[str, Any] = Depends(server.get_user)) -> Dict[str, Any]:
    book_id = str(payload.get("book_id") or "")
    if not book_id:
        raise HTTPException(400, "book_id is required")
    manifest = await _build_manifest_for_user(book_id, payload, user)
    await _record_event(
        "publishing_manifest_created",
        user,
        status=manifest["preflight"]["status"],
        details={"book_id": book_id, "platform": manifest["platform"], "errors": manifest["preflight"]["errors"]},
    )
    return manifest


@router.post("/publishing/jobs")
async def create_publishing_job(payload: Dict[str, Any], user: Dict[str, Any] = Depends(server.get_user)) -> Dict[str, Any]:
    book_id = str(payload.get("book_id") or "")
    if not book_id:
        raise HTTPException(400, "book_id is required")
    manifest = await _build_manifest_for_user(book_id, payload, user)
    job = new_job(user["id"], book_id, manifest["platform"], manifest).as_dict()
    job["manifest"] = manifest
    await db.publishing_jobs.insert_one(dict(job))
    job.pop("_id", None)
    await _record_event(
        "publishing_job_created",
        user,
        status=job["status"],
        details={"job_id": job["id"], "book_id": book_id, "platform": job["platform"]},
    )
    return job


@router.get("/publishing/jobs")
async def list_publishing_jobs(user: Dict[str, Any] = Depends(server.get_user)) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {} if _is_owner(user) else {"user_id": user["id"]}
    return await db.publishing_jobs.find(query, {"_id": 0}).sort("created_at", -1).to_list(500)


@router.get("/publishing/jobs/{job_id}")
async def get_publishing_job(job_id: str, user: Dict[str, Any] = Depends(server.get_user)) -> Dict[str, Any]:
    query: Dict[str, Any] = {"id": job_id}
    if not _is_owner(user):
        query["user_id"] = user["id"]
    job = await db.publishing_jobs.find_one(query, {"_id": 0})
    if not job:
        raise HTTPException(404, "Publishing job not found")
    return job


@router.get("/owner/publishing-log")
async def owner_publishing_log(
    limit: int = 200,
    event_type: Optional[str] = None,
    user: Dict[str, Any] = Depends(server.get_user),
) -> List[Dict[str, Any]]:
    if not _is_owner(user):
        raise HTTPException(403, "Owner access required")
    query: Dict[str, Any] = {"app_slug": APP_SLUG}
    if event_type:
        query["event_type"] = event_type
    capped_limit = min(max(limit, 1), 1000)
    return await db.system_events.find(query, {"_id": 0}).sort("created_at", -1).to_list(capped_limit)


@app.on_event("startup")
async def _ensure_finishing_indexes() -> None:
    try:
        await db.publishing_jobs.create_index([("user_id", 1), ("created_at", -1)])
        await db.publishing_jobs.create_index("id", unique=True)
        await db.system_events.create_index([("app_slug", 1), ("created_at", -1)])
        await db.system_events.create_index([("event_type", 1), ("created_at", -1)])
    except Exception as exc:
        server.log.error("Could not create finishing indexes: %s", exc)


app.include_router(router)
