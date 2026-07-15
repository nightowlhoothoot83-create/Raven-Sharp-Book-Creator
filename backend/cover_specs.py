"""Cover, barcode, and commercial printer requirements.

Printer profiles are editable defaults, not promises that a printer will never
change its specification. The frontend should always show the profile date and
ask the customer to confirm against the printer's current template before a
large print run.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Mapping, Optional

PROFILE_REVIEW_DATE = "2026-07-15"

PRINTER_PROFILES: Dict[str, Dict[str, Any]] = {
    "amazon_kdp": {
        "label": "Amazon KDP",
        "type": "print_on_demand",
        "template_required": True,
        "default_bleed_in": 0.125,
        "default_safe_margin_in": 0.25,
        "image_dpi": 300,
        "colour_mode": "Printer-managed; upload a print-ready PDF and review in KDP Previewer",
        "pdf_standard": "PDF accepted by KDP Previewer",
        "crop_marks": False,
        "cover_layout": "single full-wrap page: back + spine + front",
        "barcode": {
            "required_for_retail": True,
            "modes": ["platform_places", "customer_supplied"],
            "symbology": "EAN-13 / Bookland ISBN-13",
            "placement": "back cover, inside the printer template's barcode zone",
        },
        "notes": [
            "Use the current KDP Cover Calculator template for the final trim, binding, paper, and page count.",
            "Keep important text and logos out of trim, fold, spine, and barcode exclusion zones.",
            "Run the final files through KDP Print Previewer before submission.",
        ],
    },
    "ingramspark": {
        "label": "IngramSpark",
        "type": "print_on_demand_distribution",
        "template_required": True,
        "default_bleed_in": 0.125,
        "default_safe_margin_in": 0.25,
        "image_dpi": 300,
        "colour_mode": "CMYK recommended for predictable print colour",
        "pdf_standard": "Printer-ready PDF; use the current IngramSpark file-creation guide and template",
        "crop_marks": False,
        "cover_layout": "single full-wrap page: back + spine + front",
        "barcode": {
            "required_for_retail": True,
            "modes": ["printer_places", "customer_supplied"],
            "symbology": "EAN-13 / Bookland ISBN-13",
            "placement": "use the barcode zone in the generated cover template",
        },
        "notes": [
            "Generate a fresh cover template after trim, binding, paper, and page count are final.",
            "Embed fonts and flatten unsupported transparency before upload.",
            "Order a proof before approving distribution.",
        ],
    },
    "lulu": {
        "label": "Lulu",
        "type": "print_on_demand",
        "template_required": True,
        "default_bleed_in": 0.125,
        "default_safe_margin_in": 0.25,
        "image_dpi": 300,
        "colour_mode": "CMYK recommended where the chosen product supports it",
        "pdf_standard": "Printer-ready PDF matching the current Lulu template",
        "crop_marks": False,
        "cover_layout": "single full-wrap page: back + spine + front",
        "barcode": {
            "required_for_retail": True,
            "modes": ["printer_places", "customer_supplied"],
            "symbology": "EAN-13 / Bookland ISBN-13",
            "placement": "use the barcode area shown by the Lulu cover template",
        },
        "notes": [
            "Match the exact template for the selected product and page count.",
            "Do not add crop marks unless the current product instructions request them.",
            "Review an electronic proof and preferably a physical proof.",
        ],
    },
    "local_commercial_printer": {
        "label": "Custom Commercial Printer",
        "type": "custom",
        "template_required": False,
        "default_bleed_in": 0.125,
        "default_safe_margin_in": 0.25,
        "image_dpi": 300,
        "colour_mode": "CMYK with the printer's requested ICC profile",
        "pdf_standard": "PDF/X-1a or PDF/X-4, as requested by the printer",
        "crop_marks": None,
        "cover_layout": "printer-defined; usually a single full-wrap page",
        "barcode": {
            "required_for_retail": True,
            "modes": ["printer_places", "customer_supplied", "not_required"],
            "symbology": "EAN-13 / Bookland ISBN-13 when retail distribution is required",
            "placement": "printer-defined barcode exclusion zone",
        },
        "notes": [
            "Ask for the printer's written specification sheet before exporting.",
            "Confirm trim, bleed, safe area, spine width, binding, paper stock, colour profile, and PDF standard.",
            "Confirm whether crop marks, registration marks, and slug information are required.",
            "Approve a contract proof or physical proof before the full run.",
        ],
    },
}

COVER_FINISHES = {
    "matte": "Matte",
    "gloss": "Gloss",
    "uncoated": "Uncoated",
    "soft_touch": "Soft-touch laminate",
    "printer_choice": "Confirm with printer",
}

BINDINGS = {
    "paperback_perfect": "Paperback / perfect bound",
    "hardcover_case": "Hardcover / case bound",
    "saddle_stitch": "Saddle stitched",
    "spiral": "Spiral / coil bound",
    "board_book": "Board book",
}


def isbn13_check_digit(first_twelve: str) -> int:
    if len(first_twelve) != 12 or not first_twelve.isdigit():
        raise ValueError("ISBN calculation needs exactly 12 digits")
    total = sum(int(ch) * (1 if index % 2 == 0 else 3) for index, ch in enumerate(first_twelve))
    return (10 - (total % 10)) % 10


def normalize_isbn(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def validate_isbn13(value: Any) -> Dict[str, Any]:
    isbn = normalize_isbn(value)
    if not isbn:
        return {"valid": False, "isbn": "", "message": "No ISBN supplied"}
    if len(isbn) != 13:
        return {"valid": False, "isbn": isbn, "message": "ISBN-13 must contain 13 digits"}
    if not isbn.startswith(("978", "979")):
        return {"valid": False, "isbn": isbn, "message": "A retail book ISBN normally begins with 978 or 979"}
    expected = isbn13_check_digit(isbn[:12])
    valid = expected == int(isbn[-1])
    return {
        "valid": valid,
        "isbn": isbn,
        "expected_check_digit": expected,
        "message": "Valid ISBN-13" if valid else f"Invalid check digit; expected {expected}",
    }


def barcode_spec(payload: Mapping[str, Any]) -> Dict[str, Any]:
    mode = str(payload.get("barcode_mode") or "platform_places")
    isbn_result = validate_isbn13(payload.get("isbn"))
    price_addon = "".join(ch for ch in str(payload.get("price_addon") or "") if ch.isdigit())
    issues: List[Dict[str, str]] = []

    if mode == "customer_supplied" and not isbn_result["valid"]:
        issues.append({
            "level": "error",
            "code": "invalid_isbn_barcode",
            "message": isbn_result["message"],
            "fix": "Enter a valid ISBN-13 or let the platform/printer place the barcode.",
        })
    if price_addon and len(price_addon) != 5:
        issues.append({
            "level": "warning",
            "code": "invalid_price_addon",
            "message": "The optional EAN-5 price add-on must contain five digits.",
            "fix": "Remove it or enter the printer-approved five-digit price code.",
        })

    return {
        "mode": mode,
        "isbn": isbn_result,
        "symbology": "EAN-13 / Bookland",
        "price_addon": price_addon,
        "background": "solid white",
        "foreground": "solid black",
        "quiet_zone_required": True,
        "vector_preferred": True,
        "issues": issues,
    }


def cover_spec(payload: Mapping[str, Any]) -> Dict[str, Any]:
    printer_key = str(payload.get("printer_profile") or "amazon_kdp")
    profile = PRINTER_PROFILES.get(printer_key)
    if not profile:
        raise ValueError(f"Unknown printer profile: {printer_key}")

    trim_width = float(payload.get("trim_width_in") or 8.5)
    trim_height = float(payload.get("trim_height_in") or 8.5)
    spine_width = max(0.0, float(payload.get("spine_width_in") or 0.0))
    bleed = max(0.0, float(payload.get("bleed_in") if payload.get("bleed_in") is not None else profile["default_bleed_in"]))
    safe = max(0.0, float(payload.get("safe_margin_in") if payload.get("safe_margin_in") is not None else profile["default_safe_margin_in"]))
    dpi = int(payload.get("dpi") or profile["image_dpi"])

    cover_width = trim_width * 2 + spine_width + bleed * 2
    cover_height = trim_height + bleed * 2
    barcode = barcode_spec(payload)

    requirements = {
        "printer_profile": printer_key,
        "profile_label": profile["label"],
        "profile_review_date": PROFILE_REVIEW_DATE,
        "template_required": profile["template_required"],
        "trim_width_in": trim_width,
        "trim_height_in": trim_height,
        "spine_width_in": spine_width,
        "bleed_in": bleed,
        "safe_margin_in": safe,
        "cover_width_in": round(cover_width, 5),
        "cover_height_in": round(cover_height, 5),
        "cover_width_px": round(cover_width * dpi),
        "cover_height_px": round(cover_height * dpi),
        "dpi": dpi,
        "colour_mode": str(payload.get("colour_mode") or profile["colour_mode"]),
        "pdf_standard": str(payload.get("pdf_standard") or profile["pdf_standard"]),
        "crop_marks": profile["crop_marks"] if payload.get("crop_marks") is None else bool(payload.get("crop_marks")),
        "cover_layout": profile["cover_layout"],
        "binding": str(payload.get("binding") or "paperback_perfect"),
        "cover_finish": str(payload.get("cover_finish") or "matte"),
        "barcode": barcode,
        "notes": list(profile["notes"]),
    }
    return requirements


def preflight_cover(payload: Mapping[str, Any]) -> Dict[str, Any]:
    spec = cover_spec(payload)
    issues = list(spec["barcode"]["issues"])

    if not payload.get("front_cover_present"):
        issues.append({
            "level": "error",
            "code": "missing_front_cover",
            "message": "No front cover artwork is attached.",
            "fix": "Generate or upload front cover artwork.",
        })
    if spec["binding"] in {"paperback_perfect", "hardcover_case"} and not payload.get("back_cover_present"):
        issues.append({
            "level": "warning",
            "code": "missing_back_cover_artwork",
            "message": "The full-wrap cover has no back cover artwork or background.",
            "fix": "Add a back cover design, description, or continuous background.",
        })
    if spec["spine_width_in"] <= 0 and spec["binding"] in {"paperback_perfect", "hardcover_case"}:
        issues.append({
            "level": "error",
            "code": "missing_spine_width",
            "message": "The full-wrap cover cannot be finalised without a spine width.",
            "fix": "Choose the final page count, paper, binding, and printer template.",
        })
    if spec["dpi"] < 300:
        issues.append({
            "level": "error",
            "code": "cover_dpi_too_low",
            "message": "Print cover output is below 300 DPI.",
            "fix": "Export the cover at 300 DPI or the printer's higher requirement.",
        })
    if spec["printer_profile"] == "local_commercial_printer" and not payload.get("printer_spec_confirmed"):
        issues.append({
            "level": "warning",
            "code": "printer_spec_not_confirmed",
            "message": "The custom printer's current written specification has not been confirmed.",
            "fix": "Tick the confirmation only after checking the printer's latest template and requirements.",
        })
    if spec["template_required"] and not payload.get("printer_template_confirmed"):
        issues.append({
            "level": "warning",
            "code": "cover_template_not_confirmed",
            "message": "This printer requires a cover template generated from final production choices.",
            "fix": "Download and confirm the current printer template before final export.",
        })

    errors = sum(1 for item in issues if item["level"] == "error")
    warnings = sum(1 for item in issues if item["level"] == "warning")
    status = "blocked" if errors else ("review" if warnings else "ready")
    return {
        "status": status,
        "ready": status == "ready",
        "errors": errors,
        "warnings": warnings,
        "issues": issues,
        "spec": spec,
    }


__all__ = [
    "BINDINGS",
    "COVER_FINISHES",
    "PRINTER_PROFILES",
    "PROFILE_REVIEW_DATE",
    "barcode_spec",
    "cover_spec",
    "isbn13_check_digit",
    "normalize_isbn",
    "preflight_cover",
    "validate_isbn13",
]
