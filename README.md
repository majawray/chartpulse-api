# ChartPulse AI — Backend (Deploy to Render for FREE)

## Step-by-Step: Deploy to Render.com (5 minutes, $0)

### 1. Push this folder to GitHub
```bash
cd chartpulse-backend
git init
git add .
git commit -m "ChartPulse AI backend"
git remote add origin https://github.com/YOUR_USERNAME/chartpulse-api.git
git push -u origin main
```

### 2. Create a Render account
- Go to https://render.com
- Sign up with GitHub (no credit card needed)

### 3. Deploy the API
- Click **"New +"** → **"Web Service"**
- Connect your GitHub repo (`chartpulse-api`)
- Settings:
  - **Name:** `chartpulse-api`
  - **Region:** Singapore (closest to Middle East) or Frankfurt
  - **Runtime:** Python
  - **Build Command:** `pip install -r requirements.txt`
  - **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
  - **Plan:** Free

### 4. Add Environment Variables
In Render dashboard → your service → **Environment**:

| Key | Value |
|-----|-------|
| `ANTHROPIC_API_KEY` | `sk-ant-api03-your-claude-key` |
| `SECRET_KEY` | (click "Generate" for random value) |
| `STRIPE_SECRET_KEY` | `sk_live_...` (from Stripe dashboard) |
| `STRIPE_WEBHOOK_SECRET` | `whsec_...` (set up in step 5) |

### 5. Set up Stripe Webhook
- Go to Stripe Dashboard → Developers → Webhooks
- Click **"Add endpoint"**
- URL: `https://chartpulse-api.onrender.com/v1/stripe/webhook`
- Events: Select `checkout.session.completed`
- Copy the **Signing secret** (whsec_...) → paste into Render env vars

### 6. Update your website + extension
After Render gives you the URL (e.g. `https://chartpulse-api.onrender.com`):

**Website** (`js/app.js` line 1):
```js
const API_BASE = 'https://chartpulse-api.onrender.com';
```

**Extension** (`src/popup/popup.js` line 1):
```js
const API_BASE = 'https://chartpulse-api.onrender.com';
```

Re-upload `js/app.js` to Namecheap and reload the extension.

### 7. Test it
- Visit `https://chartpulse-api.onrender.com/health`
- You should see: `{"status":"ok","service":"chartpulse-api"}`
- Visit `https://chartpulse-api.onrender.com/docs` for the Swagger API docs

---

## Free Tier Limits
- Render free tier spins down after 15 min of inactivity
- First request after sleep takes ~30 seconds (cold start)
- Once you get paying customers, upgrade to $7/mo for always-on

## Files
```
chartpulse-backend/
├── main.py              # Full FastAPI backend (auth, AI proxy, Stripe, subscriptions)
├── requirements.txt     # Python dependencies
├── render.yaml          # One-click Render deployment config
└── README.md            # This file
```
