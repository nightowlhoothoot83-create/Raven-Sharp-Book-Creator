"""
Raven Sharp Book Creator — FastAPI Backend
AI-generated children's books with per-user brand profiles + KDP-ready export presets
Part of Ascension Digital Group
"""
from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import os, uuid, json, logging, asyncio, base64
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
    "kdp_8.5x11": {
        "label": "KDP 8.5\" x 11\" (letter — activity/workbook)",
        "trim_width_in": 8.5, "trim_height_in": 11.0,
        "bleed": True, "dpi": 300,
        "notes": "Amazon KDP letter size — good for activity books and workbooks.",
    },
}

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

class BookUpdateIn(BaseModel):
    title: Optional[str] = None
    output_format: Optional[str] = None
    pages: Optional[List[BookPageIn]] = None

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

@api.post("/billing/webhook")
async def stripe_webhook(request: Request):
    try:
        event = json.loads(await request.body())
        if event["type"] == "checkout.session.completed":
            s = event["data"]["object"]
            await db.users.update_one(
                {"id": s["metadata"]["user_id"]},
                {"$set": {"tier": s["metadata"]["tier"], "books_this_month": 0,
                          "subscription_id": s.get("subscription")}})
        elif event["type"] in ["customer.subscription.deleted", "customer.subscription.paused"]:
            sub_id = event["data"]["object"]["id"]
            await db.users.update_one({"subscription_id": sub_id}, {"$set": {"tier": "free"}})
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

@api.get("/books/{book_id}/export-spec")
async def get_export_spec(book_id: str, user: dict = Depends(get_user)):
    """Returns the exact pixel dimensions/DPI/bleed the frontend's jsPDF
    export step should use for this book's chosen output format."""
    book = await db.books.find_one({"id": book_id, "user_id": user["id"]}, {"_id": 0})
    if not book:
        raise HTTPException(404, "Book not found")
    return {"output_format": book["output_format"], **_output_dims_px(book["output_format"])}

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
