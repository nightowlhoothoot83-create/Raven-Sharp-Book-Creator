"""Publishing infrastructure for Raven Sharp Book Creator.

This module provides platform-aware upload packages and job state. It does not
pretend to directly publish where a supported public publishing API is not
available. Instead it creates validated manifests and clear manual-upload jobs.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional
import uuid


PLATFORMS: Dict[str, Dict[str, Any]] = {
    "amazon_kdp_paperback": {
        "label": "Amazon KDP Paperback",
        "delivery_mode": "manual_upload_package",
        "required_files": ["interior_pdf", "cover_pdf"],
        "optional_files": ["metadata_json", "keywords_csv"],
        "finish_level": "kdp_paperback",
        "notes": "Creates KDP-ready interior, cover specification, metadata, and upload checklist.",
    },
    "amazon_kdp_hardcover": {
        "label": "Amazon KDP Hardcover",
        "delivery_mode": "manual_upload_package",
        "required_files": ["interior_pdf", "cover_template_notes"],
        "optional_files": ["metadata_json", "keywords_csv"],
        "finish_level": "kdp_hardcover",
        "notes": "Creates the interior and metadata package. The final case-wrap cover must use the current KDP Cover Calculator template.",
    },
    "google_play_books": {
        "label": "Google Play Books",
        "delivery_mode": "partner_center_upload_package",
        "required_files": ["book_pdf_or_epub", "cover_image"],
        "optional_files": ["metadata_json", "onix_metadata"],
        "finish_level": "google_play",
        "notes": "Creates a Google Play Books upload package for Partner Center. Direct publishing remains disabled until an approved partner integration is configured.",
    },
    "generic_print": {
        "label": "Generic Print Provider",
        "delivery_mode": "download_package",
        "required_files": ["interior_pdf"],
        "optional_files": ["cover_pdf", "metadata_json"],
        "finish_level": "generic_print",
        "notes": "Creates a printer-neutral 300 DPI package with trim, bleed, and safe-margin specifications.",
    },
}


@dataclass
class PublishingJob:
    id: str
    user_id: str
    book_id: str
    platform: str
    status: str
    delivery_mode: str
    files: Dict[str, str]
    metadata: Dict[str, Any]
    checks: Dict[str, Any]
    created_at: str
    updated_at: str
    action_required: Optional[str] = None
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def platform_config(platform: str) -> Dict[str, Any]:
    if platform not in PLATFORMS:
        raise ValueError(f"Unsupported publishing platform: {platform}")
    return dict(PLATFORMS[platform])


def normalize_metadata(book: Mapping[str, Any], payload: Mapping[str, Any]) -> Dict[str, Any]:
    keywords = payload.get("keywords") or book.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [item.strip() for item in keywords.split(",") if item.strip()]
    categories = payload.get("categories") or book.get("categories") or []
    if isinstance(categories, str):
        categories = [item.strip() for item in categories.split(",") if item.strip()]

    return {
        "title": str(payload.get("title") or book.get("title") or "").strip(),
        "subtitle": str(payload.get("subtitle") or book.get("subtitle") or "").strip(),
        "author": str(payload.get("author") or book.get("author") or "").strip(),
        "contributors": payload.get("contributors") or book.get("contributors") or [],
        "description": str(payload.get("description") or book.get("description") or "").strip(),
        "keywords": keywords,
        "categories": categories,
        "language": str(payload.get("language") or book.get("language") or "en"),
        "publisher": str(payload.get("publisher") or book.get("publisher") or "Raven Sharp"),
        "publication_date": payload.get("publication_date") or book.get("publication_date"),
        "isbn": str(payload.get("isbn") or book.get("isbn") or "").strip(),
        "copyright_year": payload.get("copyright_year") or book.get("copyright_year"),
        "rights_statement": str(payload.get("rights_statement") or book.get("rights_statement") or "All rights reserved."),
        "adult_content": bool(payload.get("adult_content", False)),
        "price": payload.get("price"),
        "currency": str(payload.get("currency") or "AUD"),
        "territories": payload.get("territories") or ["WORLD"],
    }


def validate_metadata(platform: str, metadata: Mapping[str, Any]) -> List[Dict[str, str]]:
    issues: List[Dict[str, str]] = []

    def add(level: str, code: str, message: str, fix: str = "") -> None:
        issues.append({"level": level, "code": code, "message": message, "fix": fix})

    if not metadata.get("title"):
        add("error", "missing_title", "A title is required.", "Add the book title.")
    if not metadata.get("author"):
        add("error", "missing_author", "An author or contributor name is required.", "Add the author name.")
    if not metadata.get("description"):
        add("warning", "missing_description", "No sales description is included.", "Add a reader-facing description.")
    if len(metadata.get("keywords") or []) < 3:
        add("warning", "few_keywords", "Fewer than three search keywords are included.", "Add specific search phrases.")
    if platform == "google_play_books" and not metadata.get("isbn"):
        add("info", "identifier_review", "No ISBN is supplied. Partner Center may use a Google-generated identifier depending on the account and book type.")
    if platform.startswith("amazon_kdp") and not metadata.get("categories"):
        add("warning", "missing_categories", "No KDP categories are supplied.", "Choose the most relevant categories before upload.")
    return issues


def build_manifest(
    platform: str,
    book: Mapping[str, Any],
    payload: Mapping[str, Any],
    files: Optional[Mapping[str, str]] = None,
    preflight: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    config = platform_config(platform)
    metadata = normalize_metadata(book, payload)
    metadata_issues = validate_metadata(platform, metadata)
    all_issues = list((preflight or {}).get("issues") or []) + metadata_issues
    errors = sum(1 for issue in all_issues if issue.get("level") == "error")
    warnings = sum(1 for issue in all_issues if issue.get("level") == "warning")
    file_map = dict(files or {})
    missing_files = [name for name in config["required_files"] if not file_map.get(name)]
    for name in missing_files:
        all_issues.append({
            "level": "error",
            "code": f"missing_{name}",
            "message": f"Required file is missing: {name.replace('_', ' ')}.",
            "fix": "Generate or attach the required file before completing the package.",
        })
    errors += len(missing_files)

    status = "blocked" if errors else ("review" if warnings else "ready")
    return {
        "schema_version": "1.0",
        "platform": platform,
        "platform_label": config["label"],
        "delivery_mode": config["delivery_mode"],
        "book_id": book.get("id"),
        "metadata": metadata,
        "files": file_map,
        "required_files": config["required_files"],
        "optional_files": config["optional_files"],
        "preflight": {
            "status": status,
            "ready": status == "ready",
            "errors": errors,
            "warnings": warnings,
            "issues": all_issues,
        },
        "manual_steps": manual_steps(platform),
        "generated_at": utc_now(),
    }


def manual_steps(platform: str) -> List[str]:
    if platform == "amazon_kdp_paperback":
        return [
            "Open the Amazon KDP Bookshelf and create or edit the paperback edition.",
            "Enter the metadata from metadata.json.",
            "Upload interior.pdf with the matching bleed setting.",
            "Upload cover.pdf or use Cover Creator only when no full-wrap cover is supplied.",
            "Run KDP Print Previewer and resolve every blocking issue.",
            "Choose territories and pricing, then submit for review.",
        ]
    if platform == "amazon_kdp_hardcover":
        return [
            "Open the Amazon KDP Bookshelf and create or edit the hardcover edition.",
            "Enter the metadata from metadata.json.",
            "Upload interior.pdf.",
            "Generate the current hardcover template in KDP Cover Calculator using the final trim, paper, and page count.",
            "Lay out and upload the case-wrap cover against that template.",
            "Run KDP Print Previewer and resolve every blocking issue before submission.",
        ]
    if platform == "google_play_books":
        return [
            "Open Google Play Books Partner Center and add a new book.",
            "Enter the metadata from metadata.json or the supplied ONIX file when available.",
            "Upload book.epub when generated, otherwise upload book.pdf for fixed-layout content.",
            "Upload cover.jpg or cover.png.",
            "Set territories, pricing, preview, and DRM preferences.",
            "Review processing results and publish from Partner Center.",
        ]
    return [
        "Send the package to the selected printer.",
        "Confirm trim, bleed, colour profile, binding, and paper choices with the printer.",
        "Approve a digital or physical proof before the full print run.",
    ]


def new_job(
    user_id: str,
    book_id: str,
    platform: str,
    manifest: Mapping[str, Any],
) -> PublishingJob:
    now = utc_now()
    ready = bool((manifest.get("preflight") or {}).get("ready"))
    return PublishingJob(
        id=str(uuid.uuid4()),
        user_id=user_id,
        book_id=book_id,
        platform=platform,
        status="ready_for_manual_upload" if ready else "blocked",
        delivery_mode=str(manifest.get("delivery_mode") or "manual_upload_package"),
        files=dict(manifest.get("files") or {}),
        metadata=dict(manifest.get("metadata") or {}),
        checks=dict(manifest.get("preflight") or {}),
        created_at=now,
        updated_at=now,
        action_required=(
            "Upload the generated package in the platform's publishing portal."
            if ready else
            "Resolve the blocking preflight issues before creating the upload package."
        ),
    )


__all__ = [
    "PLATFORMS",
    "PublishingJob",
    "build_manifest",
    "manual_steps",
    "new_job",
    "normalize_metadata",
    "platform_config",
    "validate_metadata",
]
