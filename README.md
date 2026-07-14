# Raven Sharp Book Creator

AI-generated children's/picture books with per-user brand profiles (logo, colors,
character reference images, brand bible) and KDP-ready export presets.

Part of the Ascension Digital Group / Raven Sharp SaaS suite — same architecture
pattern as Raven Sharp Image Optimiser and Raven Sharp POD Automation:

- **Frontend**: static single-file app (`index.html`), deployed via Cloudflare Pages
- **Backend**: FastAPI on Railway (`backend/server.py`)
- **Auth**: JWT (access + refresh cookies), bcrypt password hashing
- **Billing**: Stripe subscriptions (tiers: free / creator / studio)
- **Storage**: MongoDB Atlas (motor) + Cloudflare R2 for brand assets and generated images
- **Generation**: Google Gemini (gemini-flash-latest for text, gemini-3.1-flash-image
  with imagen-4.0-generate-001 fallback for images)

## Status

🚧 Backend built and tested (import + non-DB endpoints verified). Not yet deployed.

Outstanding before going live:
- [ ] Create Railway service, set env vars (see `backend/.env.example`)
- [ ] Create MongoDB Atlas database
- [ ] Create Stripe products/prices for `creator` and `studio` tiers, replace
      placeholder price IDs in `backend/server.py` (`STRIPE_PRICES`)
- [ ] Set up Stripe webhook endpoint pointing at `/api/billing/webhook`
- [ ] Create `books.raven-sharp.com` subdomain + Cloudflare Pages project, connect
      this repo's Git for auto-deploy
- [ ] Wire frontend (`index.html`) to call the new backend instead of the old
      Cloudflare Worker Gemini proxy — currently still points at the personal-use
      `spewcrewbookcreator` worker

## Local dev

```
cd backend
pip install -r requirements.txt
cp .env.example .env   # fill in real values
uvicorn server:app --reload
```
