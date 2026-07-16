"""
Raven Sharp Book Creator — FastAPI Backend
AI-generated children's books with per-user brand profiles + KDP-ready export presets
Part of Ascension Digital Group
"""
from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import os, uuid, json, logging, asyncio, base64, hmac, hashlib
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any

import bcrypt, jwt, httpx
from fastapi import FastAPI, APIRouter, HTTPException, Request, Response, Depends, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field

# ── Config ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ravensharp-bookcreator")

# --- Self-healing startup config -------------------------------------------
# Same pattern as Image Optimiser / POD: MONGO_URL has no safe default, fail
# fast with one clear diagnostic. Everything else degrades with a loud warning
# rather than crashing the whole service.
_startup_warnings = []

MONGO_URL = os.environ.get("MONGO_URL")
if not MONGO_URL:
    log.critical(
        "STARTUP FAILURE: MONGO_URL is not set on this deployment. "
        "The app cannot start without a database connection string. "
        "Set MONGO_URL in Railway's environment variables for this service and redeploy."
    )
    raise RuntimeError("Missing required environment variable: MONGO_URL")

DB_NAME = os.environ.get("DB_NAME")
if not DB_NAME:
    DB_NAME = "ravensharp_bookcreator"
    _startup_warnings.append(f"DB_NAME was not set — defaulting to '{DB_NAME}'.")

JWT_SECRET = os.environ.get("JWT_SECRET")
if not JWT_SECRET:
    import secrets as _secrets
    JWT_SECRET = _secrets.token_hex(32)
    _startup_warnings.append(
        "JWT_SECRET was not set — auto-generated a temporary one for this boot. "
        "Existing user sessions will be invalidated on every restart until a permanent "
        "JWT_SECRET is set in Railway's environment variables."
    )

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    _startup_warnings.append(
        "GEMINI_API_KEY was not set — book generation endpoints will return a clear 500 "
        "error instead of silently failing. Set it in Railway's environment variables."
    )

STRIPE_KEY  = os.environ.get("STRIPE_API_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
if STRIPE_KEY and not STRIPE_WEBHOOK_SECRET:
    _startup_warnings.append(
        "STRIPE_WEBHOOK_SECRET was not set — the billing webhook cannot verify requests actually "
        "came from Stripe. Until this is set, /billing/webhook will REJECT all events rather than "
        "process unverified ones (see stripe_webhook() — this is a deliberate fail-closed choice, "
        "not a bug). Get this from your Stripe Dashboard -> Developers -> Webhooks -> your endpoint."
    )
RESEND_KEY  = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM_EMAIL", "Raven Sharp <noreply@raven-sharp.com>")
if not RESEND_KEY:
    _startup_warnings.append(
        "RESEND_API_KEY was not set — password reset emails will NOT be sent to customers. "
        "Only the owner account will see a usable reset token (for testing)."
    )

# Cloudflare R2 — stores brand assets (logos, character reference images) and
# generated book page images.
R2_ENDPOINT   = os.environ.get("R2_ENDPOINT", "")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY", "")
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY", "")
R2_BUCKET     = os.environ.get("R2_BUCKET", "adg-images")
if not (R2_ENDPOINT and R2_ACCESS_KEY and R2_SECRET_KEY):
    _startup_warnings.append(
        "R2 storage is not fully configured — brand asset uploads and generated page "
        "images will fail until R2_ENDPOINT/R2_ACCESS_KEY/R2_SECRET_KEY are set."
    )

for _w in _startup_warnings:
    log.warning("STARTUP: %s", _w)

OWNER_EMAIL  = os.environ.get("OWNER_EMAIL", "ascensiondigitalagency@outlook.com")
# NOTE: books.raven-sharp.com is a placeholder — the subdomain hasn't been
# created yet. Update FRONTEND_URL/CORS once DNS + Cloudflare Pages routing
# for this subdomain actually exists.
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")
BACKEND_URL = os.environ.get("BACKEND_URL", "")  # this service's own public URL — informational/for docs, not currently required by any code path
CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "CORS_ORIGINS",
        ",".join([
            FRONTEND_URL,
            "https://books.raven-sharp.com",
            "https://raven-sharp-book-creator.pages.dev",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]),
    ).split(",")
    if origin.strip()
]

client = AsyncIOMotorClient(MONGO_URL)
db     = client[DB_NAME]

app = FastAPI(title="Raven Sharp Book Creator API")
api = APIRouter(prefix="/api")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=r"https://.*\.raven-sharp\.com",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Tier config ──────────────────────────────────────────────────────────────
# Placeholder pricing — adjust once you've decided actual price points.
TIERS = {
    "free":    {"books_per_month": 1,  "pages_per_book": 10, "brand_profiles": 1, "watermark": True,  "price": 0},
    "creator": {"books_per_month": 10, "pages_per_book": 30, "brand_profiles": 3, "watermark": False, "price": 19},
    "studio":  {"books_per_month": 50, "pages_per_book": 60, "brand_profiles": 10,"watermark": False, "price": 49},
    "owner":   {"books_per_month": 99999, "pages_per_book": 999, "brand_profiles": 999, "watermark": False, "price": 0},
}

# TODO: replace with real Stripe Price IDs once created in the Stripe dashboard
# for the Book Creator product (separate from Image Optimiser/POD's prices).
STRIPE_PRICES = {
    "creator": {"monthly": "price_REPLACE_CREATOR_MONTHLY", "annual": "price_REPLACE_CREATOR_ANNUAL"},
    "studio":  {"monthly": "price_REPLACE_STUDIO_MONTHLY",  "annual": "price_REPLACE_STUDIO_ANNUAL"},
}

# ── KDP / output format presets ──────────────────────────────────────────────
# Trim sizes in inches. Bleed is Amazon KDP's standard 0.125" on trim edges.
# "digital" has no bleed/print DPI requirements — screen-only PDF.
KDP_BLEED_IN = 0.125
OUTPUT_PRESETS = {
    "digital": {
        "label": "Digital PDF (ebook / screen only)",
        "trim_width_in": 8.0, "trim_height_in": 8.0,
        "bleed": False, "dpi": 150,
        "notes": "No bleed or print-safety margins needed — for on-screen reading only.",
    },
    "print_at_home_letter": {
        "label": "Print at Home (US Letter, no bleed)",
        "trim_width_in": 8.5, "trim_height_in": 11.0,
        "bleed": False, "dpi": 300,
        "notes": "For home/office printers — NO bleed (most home printers can't print edge-to-edge). "
                 "Includes a 0.25in safe margin instead so nothing important sits too close to the edge "
                 "where a printer's own unprintable border would cut it off.",
        "safe_margin_in": 0.25,
    },
    "google_play_books": {
        "label": "Google Play Books (EPUB, reflowable digital)",
        "trim_width_in": 8.0, "trim_height_in": 8.0,  # cover reference size only — interior is reflowable text, not fixed pages
        "bleed": False, "dpi": 150,
        "notes": "Google Play Books uses reflowable EPUB for text-based books, not fixed page images — "
                 "only the cover needs a fixed-size image (this preset's dimensions are for that cover). "
                 "For heavily-illustrated picture books, Google Play Books also accepts a fixed-layout "
                 "EPUB or PDF; use the 'digital' preset's dimensions for that case instead.",
    },
    "kdp_8.5x8.5": {
        "label": "KDP 8.5\" x 8.5\" (square picture book — most common)",
        "trim_width_in": 8.5, "trim_height_in": 8.5,
        "bleed": True, "dpi": 300,
        "notes": "Amazon KDP square picture book trim size.",
    },
    "kdp_8x10": {
        "label": "KDP 8\" x 10\" (portrait picture book)",
        "trim_width_in": 8.0, "trim_height_in": 10.0,
        "bleed": True, "dpi": 300,
        "notes": "Amazon KDP portrait picture book trim size.",
    },
    "kdp_6x9": {
        "label": "KDP 6\" x 9\" (standard — chapter books)",
        "trim_width_in": 6.0, "trim_height_in": 9.0,
        "bleed": True, "dpi": 300,
        "notes": "Amazon KDP standard trim — best for text-heavier chapter books.",
    },
    "kdp_5x8": {
        "label": "KDP 5\" x 8\" (compact chapter book / novella)",
        "trim_width_in": 5.0, "trim_height_in": 8.0,
        "bleed": True, "dpi": 300,
        "notes": "Amazon KDP's smaller standard trim — common for novellas and text-only chapter books.",
    },
    "kdp_8.5x11": {
        "label": "KDP 8.5\" x 11\" (letter — activity/workbook)",
        "trim_width_in": 8.5, "trim_height_in": 11.0,
        "bleed": True, "dpi": 300,
        "notes": "Amazon KDP letter size — good for activity books and workbooks.",
    },
    "pod_ingramspark_6x9": {
        "label": "Generic Print-on-Demand 6\" x 9\" (IngramSpark / Lulu)",
        "trim_width_in": 6.0, "trim_height_in": 9.0,
        "bleed": True, "dpi": 300,
        "notes": "Same trim/bleed as KDP 6x9, but IngramSpark and Lulu have their own separate barcode "
                 "placement and cover-template requirements — check that platform's own cover template "
                 "tool before finalizing the cover specifically (interior pages are compatible either way).",
    },
    "pod_ingramspark_8.5x8.5": {
        "label": "Generic Print-on-Demand 8.5\" x 8.5\" (IngramSpark / Lulu, square)",
        "trim_width_in": 8.5, "trim_height_in": 8.5,
        "bleed": True, "dpi": 300,
        "notes": "Same trim/bleed as KDP's square size — same IngramSpark/Lulu cover-template caveat as above.",
    },
}

# Output formats where "generate a print-ready file" genuinely means
# something (has fixed page images) — used to decide what a multi-export
# request should actually attempt.
PRINT_READY_FORMATS = [k for k in OUTPUT_PRESETS if k not in ("google_play_books",)]

def _output_dims_px(preset_key: str) -> dict:
    """Full bleed-inclusive pixel dimensions at the preset's required DPI —
    what the page image should actually be generated/exported at."""
    p = OUTPUT_PRESETS.get(preset_key)
    if not p:
        raise HTTPException(400, f"Unknown output preset: {preset_key}")
    bleed = KDP_BLEED_IN if p["bleed"] else 0
    full_w_in = p["trim_width_in"] + (bleed * 2)
    full_h_in = p["trim_height_in"] + (bleed * 2)
    return {
        "trim_width_in": p["trim_width_in"], "trim_height_in": p["trim_height_in"],
        "bleed_in": bleed, "dpi": p["dpi"],
        "full_width_px": round(full_w_in * p["dpi"]),
        "full_height_px": round(full_h_in * p["dpi"]),
    }


def calc_spine_width_in(page_count: int, paper_type: str = "white", interior: str = "bw") -> float:
    """Standard KDP/IngramSpark industry formula for paperback spine width.
    Per-page thickness varies by paper colour and whether the interior is
    black & white or colour — this didn't exist anywhere in the app before,
    meaning cover files couldn't actually be assembled with a correct spine
    at all for any print format."""
    if page_count < 24:
        page_count = 24  # KDP's own minimum bindable page count
    per_page_in = {
        ("white", "bw"): 0.002252,
        ("cream", "bw"): 0.0025,
        ("white", "color"): 0.002252,
        ("cream", "color"): 0.002252,
    }.get((paper_type, interior), 0.002252)
    return round(page_count * per_page_in, 4)


def calc_full_cover_dims_in(preset_key: str, page_count: int, paper_type: str = "white") -> dict:
    """Full wraparound cover dimensions (back + spine + front), the actual
    single flat file a print platform needs — not just a front-cover image."""
    p = OUTPUT_PRESETS.get(preset_key)
    if not p:
        raise HTTPException(400, f"Unknown output preset: {preset_key}")
    spine_in = calc_spine_width_in(page_count, paper_type)
    bleed = KDP_BLEED_IN if p["bleed"] else 0
    full_width_in = round((p["trim_width_in"] * 2) + spine_in + (bleed * 2), 4)
    full_height_in = round(p["trim_height_in"] + (bleed * 2), 4)
    return {
        "spine_width_in": spine_in,
        "full_cover_width_in": full_width_in,
        "full_cover_height_in": full_height_in,
        "full_cover_width_px": round(full_width_in * p["dpi"]),
        "full_cover_height_px": round(full_height_in * p["dpi"]),
        "dpi": p["dpi"],
        "safe_area_note": "Keep text/logos at least 0.25in from the spine fold and trim edges.",
    }

@api.get("/output-options")
async def get_output_options():
    """Public — the frontend uses this to render the format picker."""
    return {key: {**val, **_output_dims_px(key)} for key, val in OUTPUT_PRESETS.items()}

# ── Auth helpers (identical pattern to Image Optimiser / POD) ───────────────
def hash_pw(pw): return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def verify_pw(pw, h):
    if isinstance(h, str):
        h = h.encode("utf-8")
    return bcrypt.checkpw(pw.encode("utf-8"), h)

def make_access(uid, email):
    return jwt.encode({"sub": uid, "email": email, "type": "access",
                        "exp": datetime.now(timezone.utc) + timedelta(days=1)},
                       JWT_SECRET, algorithm="HS256")

def make_refresh(uid):
    return jwt.encode({"sub": uid, "type": "refresh",
                        "exp": datetime.now(timezone.utc) + timedelta(days=7)},
                       JWT_SECRET, algorithm="HS256")

def set_cookies(response, access, refresh):
    kw = dict(httponly=True, secure=True, samesite="none", path="/")
    response.set_cookie("access_token",  access,  max_age=86400,  **kw)
    response.set_cookie("refresh_token", refresh, max_age=604800, **kw)

async def get_user(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0})
        if not user:
            raise HTTPException(401, "User not found")
        return user
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except Exception:
        raise HTTPException(401, "Invalid token")

async def send_email(to: str, subject: str, html: str) -> bool:
    if not RESEND_KEY:
        log.warning("send_email skipped (no RESEND_API_KEY configured): to=%s subject=%r", to, subject)
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            resp = await c.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_KEY}", "Content-Type": "application/json"},
                json={"from": RESEND_FROM, "to": [to], "subject": subject, "html": html},
            )
            if resp.status_code >= 400:
                log.error("Resend email failed (%s): %s", resp.status_code, resp.text[:500])
                return False
            return True
    except Exception as e:
        log.error("Resend email exception: %s", e)
        return False

# ── R2 storage (identical pattern to POD) ────────────────────────────────────
async def upload_to_r2(file_bytes: bytes, key_prefix: str, filename: str, mime: str = "image/png") -> str:
    """boto3 is blocking/synchronous — run it in a thread so it doesn't
    freeze the event loop for every other in-flight request."""
    if not (R2_ENDPOINT and R2_ACCESS_KEY and R2_SECRET_KEY):
        log.warning("R2 not fully configured — skipping upload, public_url will be empty")
        return ""

    def _blocking_upload():
        import boto3
        from botocore.config import Config
        import io

        key = f"{key_prefix}/{filename}"
        s3 = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            config=Config(signature_version="s3v4"),
            region_name="auto",
        )
        s3.upload_fileobj(
            io.BytesIO(file_bytes), R2_BUCKET, key,
            ExtraArgs={"ContentType": mime, "ACL": "public-read"},
        )
        public_base = os.environ.get("R2_PUBLIC_URL", f"{R2_ENDPOINT}/{R2_BUCKET}")
        return f"{public_base.rstrip('/')}/{key}"

    try:
        public_url = await asyncio.to_thread(_blocking_upload)
        log.info(f"R2 upload success: {public_url}")
        return public_url
    except Exception as e:
        log.error(f"R2 upload failed: {e}")
        return ""

# ── Gemini helpers ────────────────────────────────────────────────────────────
async def gemini_text(prompt: str) -> str:
    if not GEMINI_API_KEY:
        raise HTTPException(500, "Server misconfigured: GEMINI_API_KEY not set")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.8, "maxOutputTokens": 8192}}
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(url, json=body)
        if not r.is_success:
            raise HTTPException(502, f"Gemini text error {r.status_code}: {r.text[:300]}")
        d = r.json()
        parts = d.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])
        return parts[0].get("text", "") if parts else ""

async def gemini_image(prompt: str, reference_image_b64: Optional[str] = None, reference_mime: str = "image/png") -> Optional[str]:
    """Returns base64 image data, or None if generation failed entirely.
    If a reference image is supplied (e.g. a brand character), it's passed
    as an additional input part so Gemini can condition on it for visual
    consistency."""
    if not GEMINI_API_KEY:
        raise HTTPException(500, "Server misconfigured: GEMINI_API_KEY not set")

    parts = [{"text": prompt}]
    if reference_image_b64:
        parts.append({"inline_data": {"mime_type": reference_mime, "data": reference_image_b64}})

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-image:generateContent?key={GEMINI_API_KEY}"
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(url, json={"contents": [{"parts": parts}]})
        if r.is_success:
            d = r.json()
            for p in d.get("candidates", [{}])[0].get("content", {}).get("parts", []):
                if p.get("inlineData", {}).get("data"):
                    return p["inlineData"]["data"]
        else:
            log.warning("gemini-3.1-flash-image failed: %s", r.text[:300])

        # Fallback to Imagen 4 (text-only, no reference-image conditioning)
        imagen_url = f"https://generativelanguage.googleapis.com/v1beta/models/imagen-4.0-generate-001:predict?key={GEMINI_API_KEY}"
        r2 = await c.post(imagen_url, json={
            "instances": [{"prompt": prompt}],
            "parameters": {"sampleCount": 1, "aspectRatio": "1:1"},
        })
        if not r2.is_success:
            raise HTTPException(502, f"Image generation failed {r2.status_code}: {r2.text[:300]}")
        d2 = r2.json()
        preds = d2.get("predictions", [])
        return preds[0].get("bytesBase64Encoded") if preds else None

def _check_generation_allowed(user: dict):
    tier = user.get("tier", "free")
    limits = TIERS.get(tier, TIERS["free"])
    books_this_month = user.get("books_this_month", 0)
    if books_this_month >= limits["books_per_month"]:
        raise HTTPException(
            403,
            f"Monthly book limit reached for the '{tier}' plan ({limits['books_per_month']}/mo). Upgrade to generate more.",
        )

# ── Models ────────────────────────────────────────────────────────────────────
class RegisterIn(BaseModel):
    email: str; password: str; name: Optional[str] = None

class LoginIn(BaseModel):
    email: str; password: str

class StripeCheckoutIn(BaseModel):
    tier: str; billing: str = "monthly"

class ForgotPasswordIn(BaseModel):
    email: str

class ResetPasswordIn(BaseModel):
    token: str
    new_password: str

class BrandProfileIn(BaseModel):
    name: str
    brand_bible: str = ""            # free-text tone/style/audience/do's-and-don'ts
    primary_color: Optional[str] = None
    secondary_color: Optional[str] = None
    logo_url: Optional[str] = None
    characters: List[Dict[str, Any]] = Field(default_factory=list)  # [{name, description, image_url}]

class GenerateTextIn(BaseModel):
    prompt: str
    brand_profile_id: Optional[str] = None

class GenerateImageIn(BaseModel):
    prompt: str
    brand_profile_id: Optional[str] = None
    character_ref_url: Optional[str] = None   # specific character image to condition on

class BookPageIn(BaseModel):
    text: str = ""
    image_url: Optional[str] = None

class BookCreateIn(BaseModel):
    title: str
    brand_profile_id: Optional[str] = None
    output_format: str = "digital"    # key into OUTPUT_PRESETS
    pages: List[BookPageIn] = Field(default_factory=list)
    copyright_holder: Optional[str] = None   # e.g. "Jane Smith" or a brand/publisher name
    copyright_year: Optional[int] = None
    isbn: Optional[str] = None               # optional — most self-published KDP books don't need one

class BookUpdateIn(BaseModel):
    title: Optional[str] = None
    output_format: Optional[str] = None
    pages: Optional[List[BookPageIn]] = None
    copyright_holder: Optional[str] = None
    copyright_year: Optional[int] = None
    isbn: Optional[str] = None

# ── Auth routes (identical pattern to Image Optimiser / POD) ────────────────
@api.post("/auth/register")
async def register(payload: RegisterIn, response: Response):
    email = payload.email.lower().strip()
    if await db.users.find_one({"email": email}):
        raise HTTPException(400, "Email already registered")
    tier = "owner" if email == OWNER_EMAIL.lower() else "free"
    user = {"id": str(uuid.uuid4()), "email": email,
            "name": payload.name or email.split("@")[0],
            "password_hash": hash_pw(payload.password),
            "tier": tier, "books_this_month": 0,
            "created_at": datetime.now(timezone.utc).isoformat()}
    await db.users.insert_one(user)
    access, refresh = make_access(user["id"], email), make_refresh(user["id"])
    set_cookies(response, access, refresh)
    return {"id": user["id"], "email": email, "name": user["name"],
            "tier": tier, "books_this_month": 0, "created_at": user["created_at"]}

@api.post("/auth/login")
async def login(payload: LoginIn, response: Response):
    email = payload.email.lower().strip()
    user = await db.users.find_one({"email": email})
    if not user or not verify_pw(payload.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    access, refresh = make_access(user["id"], email), make_refresh(user["id"])
    set_cookies(response, access, refresh)
    return {"id": user["id"], "email": email, "name": user.get("name"),
            "tier": user.get("tier", "free"), "books_this_month": user.get("books_this_month", 0),
            "created_at": user["created_at"]}

@api.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")
    return {"ok": True}

@api.get("/auth/me")
async def me(user: dict = Depends(get_user)):
    return {"id": user["id"], "email": user["email"], "name": user.get("name"),
            "tier": user.get("tier", "free"), "books_this_month": user.get("books_this_month", 0),
            "created_at": user["created_at"]}

@api.post("/auth/refresh")
async def refresh_token(request: Request, response: Response):
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(401, "No refresh token")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        user = await db.users.find_one({"id": payload["sub"]})
        if not user:
            raise HTTPException(401, "User not found")
        access, refresh = make_access(user["id"], user["email"]), make_refresh(user["id"])
        set_cookies(response, access, refresh)
        return {"ok": True}
    except Exception:
        raise HTTPException(401, "Invalid refresh token")

@api.post("/auth/forgot-password")
async def forgot_password(payload: ForgotPasswordIn):
    email = payload.email.lower().strip()
    user = await db.users.find_one({"email": email})
    if not user:
        return {"message": "If that email exists, a reset link has been sent."}
    token = str(uuid.uuid4())
    _reset_tokens[token] = {"email": email, "expires": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    reset_link = f"{FRONTEND_URL}/reset-password?token={token}"
    log.info(f"Password reset token for {email}: {token}")
    await send_email(
        to=email, subject="Reset your Raven Sharp Book Creator password",
        html=f"""<p>Someone requested a password reset for your Raven Sharp Book Creator account.</p>
                 <p><a href="{reset_link}">Click here to reset your password</a> — this link expires in 1 hour.</p>
                 <p>If you didn't request this, you can safely ignore this email.</p>""",
    )
    return {"message": "If that email exists, a reset link has been sent.",
            "debug_token": token if email == OWNER_EMAIL.lower() else None}

_reset_tokens: dict = {}

@api.post("/auth/reset-password")
async def reset_password(payload: ResetPasswordIn, response: Response):
    entry = _reset_tokens.get(payload.token)
    if not entry:
        raise HTTPException(400, "Invalid or expired reset token")
    if datetime.fromisoformat(entry["expires"]) < datetime.now(timezone.utc):
        del _reset_tokens[payload.token]
        raise HTTPException(400, "Reset token has expired")
    email = entry["email"]
    result = await db.users.update_one({"email": email}, {"$set": {"password_hash": hash_pw(payload.new_password)}})
    if result.matched_count == 0:
        raise HTTPException(404, "User not found")
    del _reset_tokens[payload.token]
    return {"message": "Password reset successfully. Please sign in."}

@api.get("/auth/verify-reset-token/{token}")
async def verify_reset_token(token: str):
    entry = _reset_tokens.get(token)
    if not entry:
        raise HTTPException(400, "Invalid or expired reset token")
    if datetime.fromisoformat(entry["expires"]) < datetime.now(timezone.utc):
        del _reset_tokens[token]
        raise HTTPException(400, "Reset token has expired")
    return {"valid": True, "email": entry["email"]}

# ── Billing (identical pattern to Image Optimiser / POD) ────────────────────
@api.post("/billing/checkout")
async def create_checkout(payload: StripeCheckoutIn, user: dict = Depends(get_user)):
    if not STRIPE_KEY:
        raise HTTPException(500, "Stripe not configured")
    price_id = STRIPE_PRICES.get(payload.tier, {}).get(payload.billing)
    if not price_id:
        raise HTTPException(400, "Invalid tier")
    async with httpx.AsyncClient(timeout=30) as c:
        res = await c.post("https://api.stripe.com/v1/checkout/sessions",
            headers={"Authorization": f"Bearer {STRIPE_KEY}"},
            data={"mode": "subscription",
                  "line_items[0][price]": price_id,
                  "line_items[0][quantity]": "1",
                  "success_url": f"{FRONTEND_URL}/account?session_id={{CHECKOUT_SESSION_ID}}",
                  "cancel_url": f"{FRONTEND_URL}/pricing",
                  "customer_email": user["email"],
                  "metadata[user_id]": user["id"],
                  "metadata[tier]": payload.tier})
        if res.status_code != 200:
            raise HTTPException(500, "Stripe error")
        return {"checkout_url": res.json()["url"]}

def verify_stripe_signature(payload: bytes, sig_header: str, secret: str, tolerance_sec: int = 300) -> bool:
    """Manual implementation of Stripe's documented webhook signature scheme
    (HMAC-SHA256 over '{timestamp}.{payload}'), since the rest of this
    codebase talks to Stripe via raw httpx rather than the official SDK —
    keeping that consistent rather than pulling in the full stripe package
    for just this one check.
    https://docs.stripe.com/webhooks#verify-manually"""
    if not sig_header or not secret:
        return False
    try:
        parts = dict(item.split("=", 1) for item in sig_header.split(",") if "=" in item)
        timestamp = parts.get("t")
        v1 = parts.get("v1")
        if not timestamp or not v1:
            return False
        if abs(datetime.now(timezone.utc).timestamp() - int(timestamp)) > tolerance_sec:
            log.warning("Stripe webhook rejected: timestamp outside tolerance (possible replay)")
            return False
        signed_payload = f"{timestamp}.".encode() + payload
        expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, v1)
    except Exception as e:
        log.warning(f"Stripe signature verification error: {e}")
        return False


@api.post("/billing/webhook")
async def stripe_webhook(request: Request):
    raw_body = await request.body()

    if not STRIPE_WEBHOOK_SECRET:
        # Fail closed, not open — an unset secret must never mean "trust
        # anything posted here." See the STRIPE_WEBHOOK_SECRET startup
        # warning for how to fix this properly.
        log.error("Webhook rejected: STRIPE_WEBHOOK_SECRET is not configured")
        raise HTTPException(503, "Webhook not configured — set STRIPE_WEBHOOK_SECRET")

    sig_header = request.headers.get("stripe-signature", "")
    if not verify_stripe_signature(raw_body, sig_header, STRIPE_WEBHOOK_SECRET):
        log.error("Webhook rejected: invalid or missing Stripe-Signature header")
        raise HTTPException(400, "Invalid signature")

    try:
        event = json.loads(raw_body)
        if event["type"] == "checkout.session.completed":
            s = event["data"]["object"]
            await db.users.update_one(
                {"id": s["metadata"]["user_id"]},
                {"$set": {"tier": s["metadata"]["tier"], "books_this_month": 0,
                          "subscription_id": s.get("subscription"),
                          "payment_failed_at": None, "payment_failure_count": 0}})
        elif event["type"] in ["customer.subscription.deleted", "customer.subscription.paused"]:
            sub_id = event["data"]["object"]["id"]
            await db.users.update_one({"subscription_id": sub_id}, {"$set": {"tier": "free"}})
        elif event["type"] == "invoice.payment_failed":
            # Doesn't downgrade immediately — Stripe retries failed payments on
            # its own schedule (dunning) and will send
            # customer.subscription.deleted (already handled above) once it
            # gives up. This just flags the account so it's visible/queryable
            # rather than the user silently keeping paid access with a card
            # that's actually failing.
            invoice = event["data"]["object"]
            sub_id = invoice.get("subscription")
            if sub_id:
                await db.users.update_one(
                    {"subscription_id": sub_id},
                    {"$set": {"payment_failed_at": datetime.now(timezone.utc).isoformat()},
                     "$inc": {"payment_failure_count": 1}})
                log.warning(f"Payment failed for subscription {sub_id}")
    except Exception as e:
        log.error(f"Webhook error: {e}")
    return {"ok": True}

# ── Brand profiles ────────────────────────────────────────────────────────────
@api.post("/brand-profiles")
async def create_brand_profile(payload: BrandProfileIn, user: dict = Depends(get_user)):
    tier = user.get("tier", "free")
    limit = TIERS.get(tier, TIERS["free"])["brand_profiles"]
    existing = await db.brand_profiles.count_documents({"user_id": user["id"]})
    if existing >= limit:
        raise HTTPException(403, f"Brand profile limit reached for the '{tier}' plan ({limit}). Upgrade for more.")
    profile = {"id": str(uuid.uuid4()), "user_id": user["id"], **payload.dict(),
               "created_at": datetime.now(timezone.utc).isoformat()}
    await db.brand_profiles.insert_one(profile)
    profile.pop("_id", None)
    return profile

@api.get("/brand-profiles")
async def list_brand_profiles(user: dict = Depends(get_user)):
    profiles = await db.brand_profiles.find({"user_id": user["id"]}, {"_id": 0}).to_list(200)
    return profiles

@api.get("/brand-profiles/{profile_id}")
async def get_brand_profile(profile_id: str, user: dict = Depends(get_user)):
    profile = await db.brand_profiles.find_one({"id": profile_id, "user_id": user["id"]}, {"_id": 0})
    if not profile:
        raise HTTPException(404, "Brand profile not found")
    return profile

@api.put("/brand-profiles/{profile_id}")
async def update_brand_profile(profile_id: str, payload: BrandProfileIn, user: dict = Depends(get_user)):
    result = await db.brand_profiles.update_one(
        {"id": profile_id, "user_id": user["id"]}, {"$set": payload.dict()})
    if result.matched_count == 0:
        raise HTTPException(404, "Brand profile not found")
    return await db.brand_profiles.find_one({"id": profile_id}, {"_id": 0})

@api.delete("/brand-profiles/{profile_id}")
async def delete_brand_profile(profile_id: str, user: dict = Depends(get_user)):
    result = await db.brand_profiles.delete_one({"id": profile_id, "user_id": user["id"]})
    if result.deleted_count == 0:
        raise HTTPException(404, "Brand profile not found")
    return {"ok": True}

class AssetUploadIn(BaseModel):
    image_base64: str
    mime: str = "image/png"
    filename: str = "asset"

@api.post("/brand-profiles/{profile_id}/upload-asset")
async def upload_brand_asset(profile_id: str, payload: AssetUploadIn, user: dict = Depends(get_user)):
    """Uploads a logo or character reference image to R2 and returns its
    public URL — the frontend then attaches that URL to the brand profile's
    logo_url or characters[].image_url field via the PUT endpoint above."""
    profile = await db.brand_profiles.find_one({"id": profile_id, "user_id": user["id"]})
    if not profile:
        raise HTTPException(404, "Brand profile not found")
    image_bytes = base64.b64decode(payload.image_base64)
    key = f"{uuid.uuid4()}-{payload.filename}"
    url = await upload_to_r2(image_bytes, f"book-creator-assets/{user['id']}", key, payload.mime)
    if not url:
        raise HTTPException(500, "Upload failed — R2 not configured or upload error")
    return {"url": url}

# ── Generation ────────────────────────────────────────────────────────────────
async def _resolve_brand_context(brand_profile_id: Optional[str], user_id: str) -> str:
    """Turns a saved brand profile into prompt context text."""
    if not brand_profile_id:
        return ""
    profile = await db.brand_profiles.find_one({"id": brand_profile_id, "user_id": user_id})
    if not profile:
        return ""
    lines = [f"Brand: {profile.get('name','')}"]
    if profile.get("brand_bible"):
        lines.append(f"Brand guidelines: {profile['brand_bible']}")
    if profile.get("characters"):
        char_lines = [f"- {c.get('name','')}: {c.get('description','')}" for c in profile["characters"]]
        lines.append("Established characters (keep consistent):\n" + "\n".join(char_lines))
    return "\n".join(lines)

@api.post("/generate/text")
async def generate_text_endpoint(payload: GenerateTextIn, user: dict = Depends(get_user)):
    _check_generation_allowed(user)
    context = await _resolve_brand_context(payload.brand_profile_id, user["id"])
    full_prompt = f"{context}\n\n{payload.prompt}" if context else payload.prompt
    text = await gemini_text(full_prompt)
    return {"text": text}

@api.post("/generate/image")
async def generate_image_endpoint(payload: GenerateImageIn, user: dict = Depends(get_user)):
    _check_generation_allowed(user)
    context = await _resolve_brand_context(payload.brand_profile_id, user["id"])
    full_prompt = f"{context}\n\n{payload.prompt}" if context else payload.prompt

    ref_b64, ref_mime = None, "image/png"
    if payload.character_ref_url:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(payload.character_ref_url)
            if r.is_success:
                ref_b64 = base64.b64encode(r.content).decode()
                ref_mime = r.headers.get("content-type", "image/png")

    image_b64 = await gemini_image(full_prompt, ref_b64, ref_mime)
    if not image_b64:
        raise HTTPException(502, "Image generation returned no result")
    return {"imageB64": image_b64}

# ── Books ──────────────────────────────────────────────────────────────────────
@api.post("/books")
async def create_book(payload: BookCreateIn, user: dict = Depends(get_user)):
    if payload.output_format not in OUTPUT_PRESETS:
        raise HTTPException(400, f"Unknown output_format. Choose one of: {list(OUTPUT_PRESETS.keys())}")
    limits = TIERS.get(user.get("tier", "free"), TIERS["free"])
    if len(payload.pages) > limits["pages_per_book"]:
        raise HTTPException(403, f"Page limit for the '{user.get('tier','free')}' plan is {limits['pages_per_book']}.")
    book = {"id": str(uuid.uuid4()), "user_id": user["id"], "title": payload.title,
            "brand_profile_id": payload.brand_profile_id, "output_format": payload.output_format,
            "pages": [p.dict() for p in payload.pages],
            "copyright_holder": payload.copyright_holder,
            "copyright_year": payload.copyright_year or datetime.now(timezone.utc).year,
            "isbn": payload.isbn,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat()}
    await db.books.insert_one(book)
    await db.users.update_one({"id": user["id"]}, {"$inc": {"books_this_month": 1}})
    book.pop("_id", None)
    return book

@api.get("/books")
async def list_books(user: dict = Depends(get_user)):
    return await db.books.find({"user_id": user["id"]}, {"_id": 0}).sort("updated_at", -1).to_list(500)

@api.get("/books/{book_id}")
async def get_book(book_id: str, user: dict = Depends(get_user)):
    book = await db.books.find_one({"id": book_id, "user_id": user["id"]}, {"_id": 0})
    if not book:
        raise HTTPException(404, "Book not found")
    return book

@api.put("/books/{book_id}")
async def update_book(book_id: str, payload: BookUpdateIn, user: dict = Depends(get_user)):
    updates = {k: v for k, v in payload.dict(exclude_unset=True).items() if v is not None}
    if "output_format" in updates and updates["output_format"] not in OUTPUT_PRESETS:
        raise HTTPException(400, f"Unknown output_format. Choose one of: {list(OUTPUT_PRESETS.keys())}")
    if "pages" in updates:
        updates["pages"] = [p if isinstance(p, dict) else p.dict() for p in updates["pages"]]
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = await db.books.update_one({"id": book_id, "user_id": user["id"]}, {"$set": updates})
    if result.matched_count == 0:
        raise HTTPException(404, "Book not found")
    return await db.books.find_one({"id": book_id}, {"_id": 0})

@api.delete("/books/{book_id}")
async def delete_book(book_id: str, user: dict = Depends(get_user)):
    result = await db.books.delete_one({"id": book_id, "user_id": user["id"]})
    if result.deleted_count == 0:
        raise HTTPException(404, "Book not found")
    return {"ok": True}

@api.get("/books/{book_id}/export-for-video")
async def export_book_for_video(book_id: str, user: dict = Depends(get_user)):
    """Packages this book as a script + reference images, structured for
    handoff to Content Creator (a separate app/database — no direct server-
    to-server trust between them, so this just returns a bundle the frontend
    fetches with the user's Book Creator token, then posts to Content
    Creator's own /projects endpoint using their Content Creator token)."""
    book = await db.books.find_one({"id": book_id, "user_id": user["id"]}, {"_id": 0})
    if not book:
        raise HTTPException(404, "Book not found")
    pages = book.get("pages", [])
    full_script = "\n\n".join(
        f"Scene {i+1}: {p.get('text','').strip()}" for i, p in enumerate(pages) if p.get("text", "").strip()
    )
    return {
        "source": "book_creator",
        "book_id": book_id,
        "title": book["title"],
        "suggested_project_title": f"{book['title']} — Video",
        "full_script": full_script,
        "reference_images": [p["image_url"] for p in pages if p.get("image_url")],
        "page_count": len(pages),
        "brand_profile_id": book.get("brand_profile_id"),  # same brand concept exists in Content Creator — reuse if the frontend matches profiles by name
    }

@api.get("/books/{book_id}/export-spec")
async def get_export_spec(book_id: str, user: dict = Depends(get_user)):
    """Returns the exact pixel dimensions/DPI/bleed the frontend's jsPDF
    export step should use for this book's chosen output format, plus the
    full wraparound cover spec (front + spine + back) when the format is a
    bound print format — this didn't exist before, so cover files couldn't
    actually be assembled with a correct spine width for any print format."""
    book = await db.books.find_one({"id": book_id, "user_id": user["id"]}, {"_id": 0})
    if not book:
        raise HTTPException(404, "Book not found")
    fmt = book["output_format"]
    spec = {"output_format": fmt, **_output_dims_px(fmt)}
    if OUTPUT_PRESETS[fmt]["bleed"]:
        spec["cover"] = calc_full_cover_dims_in(fmt, len(book.get("pages", [])))
    spec["copyright"] = {
        "holder": book.get("copyright_holder"),
        "year": book.get("copyright_year"),
        "isbn": book.get("isbn"),
    }
    return spec

class MultiExportIn(BaseModel):
    formats: List[str]  # e.g. ["kdp_8.5x8.5", "google_play_books", "print_at_home_letter"]

@api.post("/books/{book_id}/export-spec/multi")
async def get_multi_export_spec(book_id: str, payload: MultiExportIn, user: dict = Depends(get_user)):
    """Returns export specs for SEVERAL formats at once, so one book can
    produce multiple platform-ready files (e.g. KDP paperback + Google Play
    Books + a print-at-home PDF) without re-running the whole export flow
    per platform."""
    book = await db.books.find_one({"id": book_id, "user_id": user["id"]}, {"_id": 0})
    if not book:
        raise HTTPException(404, "Book not found")
    unknown = [f for f in payload.formats if f not in OUTPUT_PRESETS]
    if unknown:
        raise HTTPException(400, f"Unknown output_format(s): {unknown}. Choose from: {list(OUTPUT_PRESETS.keys())}")
    page_count = len(book.get("pages", []))
    formats_out = {}
    for f in payload.formats:
        entry = {**OUTPUT_PRESETS[f], **_output_dims_px(f)}
        if OUTPUT_PRESETS[f]["bleed"]:
            entry["cover"] = calc_full_cover_dims_in(f, page_count)
        formats_out[f] = entry
    return {
        "book_id": book_id,
        "copyright": {"holder": book.get("copyright_holder"), "year": book.get("copyright_year"), "isbn": book.get("isbn")},
        "formats": formats_out,
    }

# ── Health ───────────────────────────────────────────────────────────────────
@api.get("/health/detailed")
async def health_detailed():
    checks = {}
    try:
        await db.command("ping")
        checks["mongo"] = {"status": "ok"}
    except Exception as e:
        checks["mongo"] = {"status": "error", "detail": str(e)}
    checks["gemini_configured"] = bool(GEMINI_API_KEY)
    checks["stripe_configured"] = bool(STRIPE_KEY)
    checks["resend_configured"] = bool(RESEND_KEY)
    checks["r2_configured"] = bool(R2_ENDPOINT and R2_ACCESS_KEY and R2_SECRET_KEY)
    return checks

@api.get("/")
async def root():
    return {"service": "Raven Sharp Book Creator API", "status": "ok"}

app.include_router(api)

@app.on_event("startup")
async def startup():
    log.info("Raven Sharp Book Creator API starting up. DB=%s", DB_NAME)

@app.on_event("shutdown")
async def shutdown():
    client.close()

@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    log.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"error": "Internal server error"})

@app.get("/health")
async def health():
    return {"status": "ok"}
