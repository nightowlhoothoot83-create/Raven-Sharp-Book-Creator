"""Print and digital finishing specifications for Raven Sharp Book Creator.

The functions in this module are deliberately provider-aware. They calculate
interior page dimensions, paperback cover dimensions, spine width, minimum
page counts, safe margins, and a structured preflight report.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping

KDP_BLEED_IN = 0.125
KDP_MAX_PDF_MB = 650


FINISH_LEVELS: Dict[str, Dict[str, Any]] = {
    "draft": {
        "label": "Draft Preview",
        "description": "Fast screen PDF for reviewing text and illustrations.",
        "dpi": 120,
        "print_ready": False,
        "cover_required": False,
    },
    "digital_pdf": {
        "label": "Digital Book PDF",
        "description": "High-quality screen PDF for direct download and sharing.",
        "dpi": 150,
        "print_ready": False,
        "cover_required": False,
    },
    "kdp_paperback": {
        "label": "Amazon KDP Paperback",
        "description": "Single-page 300 DPI interior plus a separate full-wrap cover.",
        "dpi": 300,
        "print_ready": True,
        "cover_required": True,
    },
    "kdp_hardcover": {
        "label": "Amazon KDP Hardcover",
        "description": "300 DPI interior with hardcover checks. Use KDP's cover calculator template for the case-wrap cover.",
        "dpi": 300,
        "print_ready": True,
        "cover_required": True,
    },
    "generic_print": {
        "label": "Generic Print-Ready PDF",
        "description": "300 DPI PDF with configurable trim, bleed, and safe margins.",
        "dpi": 300,
        "print_ready": True,
        "cover_required": False,
    },
}


TRIM_PRESETS: Dict[str, Dict[str, Any]] = {
    "square_8_5": {"label": '8.5 x 8.5 in Square', "width_in": 8.5, "height_in": 8.5},
    "portrait_8x10": {"label": '8 x 10 in Portrait', "width_in": 8.0, "height_in": 10.0},
    "standard_6x9": {"label": '6 x 9 in Standard', "width_in": 6.0, "height_in": 9.0},
    "workbook_8_5x11": {"label": '8.5 x 11 in Workbook', "width_in": 8.5, "height_in": 11.0},
    "hardcover_5_5x8_5": {"label": '5.5 x 8.5 in Hardcover', "width_in": 5.5, "height_in": 8.5},
    "hardcover_7x10": {"label": '7 x 10 in Hardcover', "width_in": 7.0, "height_in": 10.0},
    "hardcover_8_25x11": {"label": '8.25 x 11 in Hardcover', "width_in": 8.25, "height_in": 11.0},
}


PAPER_TYPES: Dict[str, Dict[str, Any]] = {
    "premium_color_white": {
        "label": "Premium color on white paper",
        "spine_factor_in": 0.002347,
        "paperback_min_pages": 24,
        "hardcover_min_pages": 76,
    },
    "standard_color_white": {
        "label": "Standard color on white paper",
        "spine_factor_in": 0.002347,
        "paperback_min_pages": 72,
        "hardcover_min_pages": None,
    },
    "black_white_white": {
        "label": "Black ink on white paper",
        "spine_factor_in": 0.002252,
        "paperback_min_pages": 24,
        "hardcover_min_pages": 76,
    },
    "black_white_cream": {
        "label": "Black ink on cream paper",
        "spine_factor_in": 0.0025,
        "paperback_min_pages": 24,
        "hardcover_min_pages": 76,
    },
    "black_white_groundwood": {
        "label": "Black ink on groundwood paper",
        "spine_factor_in": 0.00235,
        "paperback_min_pages": 24,
        "hardcover_min_pages": None,
    },
}


@dataclass(frozen=True)
class Issue:
    level: str
    code: str
    message: str
    fix: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "fix": self.fix,
        }


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_trim(trim_key: str, custom_width: Any = None, custom_height: Any = None) -> Dict[str, Any]:
    if trim_key == "custom":
        width = _number(custom_width, 8.5)
        height = _number(custom_height, 8.5)
        if not 4 <= width <= 8.5:
            raise ValueError("Custom trim width must be between 4 and 8.5 inches")
        if not 6 <= height <= 11.69:
            raise ValueError("Custom trim height must be between 6 and 11.69 inches")
        return {"label": f"{width:g} x {height:g} in Custom", "width_in": width, "height_in": height}
    if trim_key not in TRIM_PRESETS:
        raise ValueError(f"Unknown trim preset: {trim_key}")
    return dict(TRIM_PRESETS[trim_key])


def interior_dimensions(trim_width_in: float, trim_height_in: float, bleed: bool, dpi: int) -> Dict[str, Any]:
    """Return KDP single-page dimensions.

    A full-bleed KDP interior is 0.125 inches wider and 0.25 inches taller
    than trim. The extra width belongs to the outer edge, not both sides.
    """
    page_width_in = trim_width_in + (KDP_BLEED_IN if bleed else 0)
    page_height_in = trim_height_in + (KDP_BLEED_IN * 2 if bleed else 0)
    return {
        "trim_width_in": trim_width_in,
        "trim_height_in": trim_height_in,
        "page_width_in": round(page_width_in, 5),
        "page_height_in": round(page_height_in, 5),
        "bleed_in": KDP_BLEED_IN if bleed else 0,
        "dpi": dpi,
        "page_width_px": round(page_width_in * dpi),
        "page_height_px": round(page_height_in * dpi),
    }


def gutter_margin_in(page_count: int) -> float:
    if page_count <= 150:
        return 0.375
    if page_count <= 300:
        return 0.5
    if page_count <= 500:
        return 0.625
    return 0.75


def minimum_page_count(binding: str, paper_type: str) -> int:
    paper = PAPER_TYPES.get(paper_type, PAPER_TYPES["premium_color_white"])
    key = "hardcover_min_pages" if binding == "hardcover" else "paperback_min_pages"
    minimum = paper.get(key)
    if minimum is None:
        raise ValueError(f"{paper['label']} is not available for {binding}")
    return int(minimum)


def paperback_spine_width(page_count: int, paper_type: str) -> float:
    paper = PAPER_TYPES.get(paper_type)
    if not paper:
        raise ValueError(f"Unknown paper type: {paper_type}")
    return round(page_count * float(paper["spine_factor_in"]), 5)


def paperback_cover_dimensions(trim_width_in: float, trim_height_in: float, page_count: int, paper_type: str, dpi: int = 300) -> Dict[str, Any]:
    spine = paperback_spine_width(page_count, paper_type)
    width = (KDP_BLEED_IN * 2) + (trim_width_in * 2) + spine
    height = (KDP_BLEED_IN * 2) + trim_height_in
    return {
        "cover_width_in": round(width, 5),
        "cover_height_in": round(height, 5),
        "cover_width_px": round(width * dpi),
        "cover_height_px": round(height * dpi),
        "spine_width_in": spine,
        "spine_text_allowed": page_count > 79,
        "outside_safe_margin_in": 0.25,
        "fold_variance_in": 0.0625,
        "dpi": dpi,
    }


def build_export_spec(payload: Mapping[str, Any]) -> Dict[str, Any]:
    finish_key = str(payload.get("finish_level", "kdp_paperback"))
    if finish_key not in FINISH_LEVELS:
        raise ValueError(f"Unknown finish level: {finish_key}")

    finish = FINISH_LEVELS[finish_key]
    trim_key = str(payload.get("trim_key", "square_8_5"))
    trim = get_trim(trim_key, payload.get("custom_width_in"), payload.get("custom_height_in"))
    page_count = max(1, _integer(payload.get("page_count"), 1))
    dpi = _integer(payload.get("dpi"), int(finish["dpi"]))
    bleed = bool(payload.get("bleed", finish_key in {"kdp_paperback", "kdp_hardcover", "generic_print"}))
    paper_type = str(payload.get("paper_type", "premium_color_white"))
    binding = "hardcover" if finish_key == "kdp_hardcover" else "paperback"

    interior = interior_dimensions(trim["width_in"], trim["height_in"], bleed, dpi)
    inside_margin = gutter_margin_in(page_count) if finish["print_ready"] else 0.25
    outside_margin = 0.375 if bleed and finish["print_ready"] else 0.25

    cover: Dict[str, Any]
    if finish_key == "kdp_paperback":
        cover = {
            "mode": "generated_full_wrap",
            **paperback_cover_dimensions(trim["width_in"], trim["height_in"], page_count, paper_type, dpi),
        }
    elif finish_key == "kdp_hardcover":
        cover = {
            "mode": "kdp_cover_calculator_template_required",
            "reason": "Hardcover case-wrap dimensions include hinges and wrap areas and must use the current KDP cover calculator template.",
        }
    else:
        cover = {"mode": "not_required"}

    minimum = 1
    if finish_key == "kdp_paperback":
        minimum = minimum_page_count("paperback", paper_type)
    elif finish_key == "kdp_hardcover":
        minimum = minimum_page_count("hardcover", paper_type)

    return {
        "finish_level": finish_key,
        "finish": dict(finish),
        "trim_key": trim_key,
        "trim": trim,
        "paper_type": paper_type,
        "paper": dict(PAPER_TYPES.get(paper_type, PAPER_TYPES["premium_color_white"])),
        "binding": binding,
        "page_count": page_count,
        "minimum_page_count": minimum,
        "interior": interior,
        "margins": {
            "inside_in": inside_margin,
            "outside_in": outside_margin,
            "top_bottom_in": outside_margin,
        },
        "cover": cover,
        "requirements": {
            "single_pages_not_spreads": finish_key.startswith("kdp_"),
            "minimum_image_dpi": 300 if finish["print_ready"] else dpi,
            "maximum_recommended_image_dpi": 600 if finish["print_ready"] else None,
            "maximum_pdf_mb": KDP_MAX_PDF_MB if finish_key.startswith("kdp_") else None,
            "fonts_embedded": finish["print_ready"],
            "transparent_layers_flattened": finish["print_ready"],
            "no_crop_marks": finish_key.startswith("kdp_"),
        },
    }


def preflight_book(payload: Mapping[str, Any]) -> Dict[str, Any]:
    spec = build_export_spec(payload)
    issues: List[Issue] = []
    finish_key = spec["finish_level"]
    page_count = spec["page_count"]
    title = str(payload.get("title", "")).strip()
    author = str(payload.get("author", "")).strip()
    description = str(payload.get("description", "")).strip()
    keywords = payload.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [part.strip() for part in keywords.split(",") if part.strip()]

    if not title:
        issues.append(Issue("error", "missing_title", "A book title is required.", "Add the title before exporting."))
    if finish_key.startswith("kdp_") and not author:
        issues.append(Issue("error", "missing_author", "KDP metadata needs an author or contributor name.", "Add the author name in Finish and Export."))
    if finish_key.startswith("kdp_") and not description:
        issues.append(Issue("warning", "missing_description", "The upload package has no book description.", "Add a sales description for the KDP listing."))
    if finish_key.startswith("kdp_") and len(keywords) < 3:
        issues.append(Issue("warning", "few_keywords", "Fewer than three search keywords are supplied.", "Add specific phrases readers might search for."))

    if page_count < spec["minimum_page_count"]:
        issues.append(Issue(
            "error",
            "page_count_too_low",
            f"{spec['finish']['label']} requires at least {spec['minimum_page_count']} pages for the selected paper type; this export has {page_count}.",
            "Add front matter, activities, back matter, or choose a different finish level.",
        ))
    if finish_key.startswith("kdp_") and page_count % 2:
        issues.append(Issue("error", "odd_page_count", "KDP print page count must resolve to an even number.", "Add one final blank or notes page."))

    images = payload.get("images") or []
    missing_images = 0
    low_res_images = 0
    required_width = spec["interior"]["page_width_px"]
    required_height = round(spec["interior"]["page_height_px"] * 0.72)
    for item in images:
        if not isinstance(item, Mapping) or not item.get("present"):
            missing_images += 1
            continue
        width = _integer(item.get("width"), 0)
        height = _integer(item.get("height"), 0)
        if spec["finish"]["print_ready"] and (width < required_width * 0.9 or height < required_height * 0.9):
            low_res_images += 1

    if missing_images:
        issues.append(Issue("error", "missing_images", f"{missing_images} illustrated page(s) have no image.", "Regenerate or replace the missing illustrations."))
    if low_res_images:
        issues.append(Issue(
            "warning",
            "low_resolution_images",
            f"{low_res_images} illustration(s) are below the target pixel dimensions for a true 300 DPI export.",
            "Upscale those images before describing the package as print-ready.",
        ))

    if finish_key.startswith("kdp_") and not payload.get("cover_present"):
        issues.append(Issue("error", "missing_cover", "A print cover image is required.", "Generate or upload a front cover before exporting."))
    if payload.get("watermark", False) and spec["finish"]["print_ready"]:
        issues.append(Issue("error", "watermark_present", "Print-ready files cannot contain a preview watermark.", "Export from a plan that removes the watermark."))

    if finish_key == "kdp_hardcover":
        issues.append(Issue(
            "info",
            "hardcover_template_required",
            "The interior can be exported here, but the hardcover case-wrap cover must be laid out against the current KDP Cover Calculator template.",
            "Download the KDP hardcover template using the final page count and print choices.",
        ))

    errors = sum(1 for issue in issues if issue.level == "error")
    warnings = sum(1 for issue in issues if issue.level == "warning")
    status = "blocked" if errors else ("review" if warnings else "ready")
    return {
        "status": status,
        "ready": status == "ready",
        "errors": errors,
        "warnings": warnings,
        "issues": [issue.as_dict() for issue in issues],
        "spec": spec,
    }


__all__ = [
    "FINISH_LEVELS",
    "TRIM_PRESETS",
    "PAPER_TYPES",
    "build_export_spec",
    "preflight_book",
    "interior_dimensions",
    "paperback_cover_dimensions",
    "paperback_spine_width",
]
