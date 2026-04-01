"""
ChartPulse AI — Backend API v3
Pricing: Free (3 total), Starter $39/mo (5/day), Pro $79/mo (10/day)
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
import secrets, json, time, httpx, os, hashlib

from pydantic import BaseModel, EmailStr
from typing import List, Optional
from jose import jwt

SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
STRIPE_SECRET = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

app = FastAPI(title="ChartPulse AI API", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

# ─── Plan Config ──────────────────────────────────────────
PLAN_CONFIG = {
    "Free": {"daily_limit": 0, "total_limit": 3, "max_strategies": 3, "personas": ["intraday"], "languages": ["en"], "history_limit": 5, "strategy_breakdown": False},
    "Starter": {"daily_limit": 5, "total_limit": 0, "max_strategies": 10, "personas": ["scalper","intraday","swing","position"], "languages": ["en","ar"], "history_limit": 0, "strategy_breakdown": False},
    "Pro": {"daily_limit": 10, "total_limit": 0, "max_strategies": 10, "personas": ["scalper","intraday","swing","position"], "languages": ["en","ar"], "history_limit": 0, "strategy_breakdown": True},
}

async def sb(method, table, data=None, params=None):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=representation"}
    async with httpx.AsyncClient(timeout=15) as client:
        if method == "GET": r = await client.get(url, headers=headers, params=params or {})
        elif method == "POST": r = await client.post(url, headers=headers, json=data)
        elif method == "PATCH": r = await client.patch(url, headers=headers, json=data, params=params or {})
        else: raise ValueError(f"Unknown: {method}")
    if r.status_code >= 400: raise ValueError(f"Supabase {r.status_code}: {r.text[:300]}")
    try: return r.json()
    except: return None

def hash_password(pw):
    salt = secrets.token_hex(16)
    return f"{salt}:{hashlib.sha256((salt + pw).encode()).hexdigest()}"

def verify_password(pw, stored):
    salt, h = stored.split(":")
    return hashlib.sha256((salt + pw).encode()).hexdigest() == h

def create_token(uid):
    return jwt.encode({"sub": uid, "exp": datetime.utcnow() + timedelta(hours=72)}, SECRET_KEY, algorithm="HS256")

async def get_current_user(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "): raise HTTPException(401, "Missing token")
    try: uid = jwt.decode(auth[7:], SECRET_KEY, algorithms=["HS256"]).get("sub")
    except: raise HTTPException(401, "Invalid token")
    rows = await sb("GET", "users", params={"id": f"eq.{uid}", "select": "*"})
    if not rows: raise HTTPException(401, "User not found")
    return rows[0]

async def detect_country(request: Request):
    ip = request.headers.get("X-Forwarded-For", request.client.host)
    if ip and "," in ip: ip = ip.split(",")[0].strip()
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"http://ip-api.com/json/{ip}?fields=country,countryCode")
            if r.status_code == 200:
                d = r.json()
                return d.get("country", "Unknown"), d.get("countryCode", "")
    except: pass
    return "Unknown", ""

def get_plan_config(user):
    plan = user.get("plan", "Free")
    # Check expiry
    expires = user.get("plan_expires")
    if expires and plan != "Free":
        try:
            if datetime.fromisoformat(expires) < datetime.utcnow():
                plan = "Free"
        except: pass
    return plan, PLAN_CONFIG.get(plan, PLAN_CONFIG["Free"])

class RegisterReq(BaseModel):
    email: EmailStr
    password: str
    phone: Optional[str] = None
    country: Optional[str] = None

class LoginReq(BaseModel):
    email: EmailStr
    password: str

class AnalyzeReq(BaseModel):
    image_base64: str
    strategies: List[str]
    persona: str = "intraday"
    language: str = "en"

class SubscribeReq(BaseModel):
    plan: str


# ─── Auth ─────────────────────────────────────────────────
@app.post("/v1/auth/register")
async def register(req: RegisterReq, request: Request):
    existing = await sb("GET", "users", params={"email": f"eq.{req.email}", "select": "id"})
    if existing: raise HTTPException(400, "Email already registered")
    country, cc = req.country or "", ""
    if not country: country, cc = await detect_country(request)
    uid = secrets.token_hex(16)
    await sb("POST", "users", {"id": uid, "email": req.email, "password_hash": hash_password(req.password), "phone": req.phone or "", "country": country, "country_code": cc, "plan": "Free", "daily_limit": 3, "plan_expires": None, "created_at": datetime.utcnow().isoformat()})
    return {"token": create_token(uid), "user": {"id": uid, "email": req.email, "plan": "Free", "signals_left": 3, "plan_config": PLAN_CONFIG["Free"]}}

@app.post("/v1/auth/login")
async def login(req: LoginReq):
    rows = await sb("GET", "users", params={"email": f"eq.{req.email}", "select": "*"})
    if not rows or not verify_password(req.password, rows[0]["password_hash"]): raise HTTPException(401, "Invalid credentials")
    user = rows[0]
    plan, cfg = get_plan_config(user)
    signals_left = await calc_signals_left(user, plan, cfg)
    return {"token": create_token(user["id"]), "user": {"id": user["id"], "email": user["email"], "plan": plan, "signals_left": signals_left, "plan_config": cfg}}

async def calc_signals_left(user, plan, cfg):
    all_analyses = await sb("GET", "analyses", params={"user_id": f"eq.{user['id']}", "select": "id,created_at"})
    total = len(all_analyses) if all_analyses else 0
    if plan == "Free":
        return max(0, cfg["total_limit"] - total)
    else:
        today = datetime.utcnow().date().isoformat()
        today_count = sum(1 for a in (all_analyses or []) if a.get("created_at","").startswith(today))
        return max(0, cfg["daily_limit"] - today_count)


# ─── Analyze ──────────────────────────────────────────────
STRATEGY_PROMPTS = {"price_action": "Analyze using pure price action: candlestick patterns, market structure, trend direction, key swing levels.", "smc": "Analyze using Smart Money Concepts: order blocks, fair value gaps, liquidity sweeps, institutional order flow.", "supply_demand": "Analyze using supply and demand zones: fresh vs tested zones, zone quality, DBR/RBD.", "fibonacci": "Analyze using Fibonacci retracements (0.382, 0.5, 0.618, 0.786) and extensions.", "ema_ma": "Analyze using EMA 9/20/50/200, golden/death crosses, EMA pullbacks.", "wyckoff": "Analyze using Wyckoff: accumulation/distribution, spring/upthrust.", "elliott": "Analyze using Elliott Wave: wave count (impulse 1-5 or corrective A-B-C).", "ichimoku": "Analyze using Ichimoku: Tenkan/Kijun cross, cloud position, Chikou span.", "volume": "Analyze using volume: spikes, VWAP, POC, high/low volume nodes.", "harmonic": "Analyze using Harmonic patterns: Gartley, Bat, Butterfly, Crab, Cypher."}

def build_prompt(strategies, persona, language, show_breakdown):
    pm = {"scalper": "SCALPER (1m-5m, tight SL)", "intraday": "INTRADAY trader (15m-1H)", "swing": "SWING trader (4H-Daily)", "position": "POSITION trader (Daily-Weekly)"}
    lm = {"en": "English", "ar": "Arabic"}
    st = "\n".join(f"{i+1}. {s.replace('_',' ').title()}: {STRATEGY_PROMPTS.get(s,'')}" for i, s in enumerate(strategies) if s in STRATEGY_PROMPTS)
    breakdown = ""
    if show_breakdown:
        breakdown = ',"strategies":[{"name":"name","verdict":"LONG/SHORT/NEUTRAL","confidence":1-5,"key_finding":"one sentence"}]'
    return f"""You are ChartPulse AI. Analyze this chart:
{st}
You are analyzing for a {pm.get(persona, pm['intraday'])}.
Respond in {lm.get(language, 'English')}.
RESPOND ONLY IN JSON (no markdown, no backticks):
{{"symbol":"symbol","timeframe":"tf","consensus":{{"direction":"LONG/SHORT/NO TRADE","confidence":0-100,"entry":"price","take_profit":"price","stop_loss":"price","risk_reward":"ratio"}}{breakdown},"reasoning":"2-3 sentences"}}"""

@app.post("/v1/analyze")
async def analyze_chart(req: AnalyzeReq, request: Request):
    user = await get_current_user(request)
    plan, cfg = get_plan_config(user)

    # Reset expired plans
    if plan != user.get("plan"):
        await sb("PATCH", "users", {"plan": "Free", "daily_limit": 3}, params={"id": f"eq.{user['id']}"})

    # Check signals left
    signals_left = await calc_signals_left(user, plan, cfg)
    if signals_left <= 0:
        if plan == "Free":
            raise HTTPException(402, "Your 3 free signals are used up. Upgrade to Starter for 5 signals/day.")
        else:
            raise HTTPException(402, f"Daily limit reached ({cfg['daily_limit']} signals). Upgrade your plan for more.")

    # Enforce strategy limit
    strategies = req.strategies[:cfg["max_strategies"]]
    if not strategies: raise HTTPException(400, "Select at least one strategy")

    # Enforce persona restriction
    if req.persona not in cfg["personas"]:
        req.persona = "intraday"

    # Enforce language restriction
    if req.language not in cfg["languages"]:
        req.language = "en"

    if len(req.image_base64) < 100: raise HTTPException(400, "Invalid image")

    start = time.time()
    prompt = build_prompt(strategies, req.persona, req.language, cfg["strategy_breakdown"])

    result = None
    last_error = None
    for n, fn, k in [("claude", call_claude, ANTHROPIC_API_KEY), ("gemini", call_gemini, GEMINI_API_KEY), ("openai", call_openai, OPENAI_API_KEY)]:
        if not k: continue
        try:
            result = await fn(req.image_base64, prompt)
            break
        except Exception as e: last_error = e

    if not result: raise HTTPException(500, f"Analysis failed: {last_error}")

    import re
    clean = re.sub(r'```json\s*', '', result)
    clean = re.sub(r'```\s*', '', clean).strip()
    match = re.search(r'\{[\s\S]*\}', clean)
    if not match: raise HTTPException(500, "Could not parse AI response")
    analysis = json.loads(match.group(0))
    ems = int((time.time() - start) * 1000)

    # If not Pro, remove strategy breakdown from response
    if not cfg["strategy_breakdown"]:
        analysis.pop("strategies", None)

    await sb("POST", "analyses", {"user_id": user["id"], "symbol": analysis.get("symbol", "?"), "direction": analysis.get("consensus", {}).get("direction", ""), "confidence": analysis.get("consensus", {}).get("confidence", 0), "timeframe": analysis.get("timeframe", ""), "entry_price": analysis.get("consensus", {}).get("entry", ""), "tp_price": analysis.get("consensus", {}).get("take_profit", ""), "sl_price": analysis.get("consensus", {}).get("stop_loss", ""), "execution_ms": ems, "created_at": datetime.utcnow().isoformat()})

    new_left = signals_left - 1
    return {"analysis": analysis, "signals_left": new_left, "plan": plan, "execution_ms": ems}


async def call_claude(img, prompt):
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post("https://api.anthropic.com/v1/messages", headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}, json={"model": "claude-sonnet-4-20250514", "max_tokens": 2000, "messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}}, {"type": "text", "text": prompt}]}]})
    if r.status_code != 200: raise ValueError(f"Claude {r.status_code}: {r.text[:200]}")
    return next(b for b in r.json()["content"] if b["type"] == "text")["text"]

async def call_gemini(img, prompt):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}", json={"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "image/png", "data": img}}]}], "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2000}})
    if r.status_code != 200: raise ValueError(f"Gemini {r.status_code}")
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]

async def call_openai(img, prompt):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.openai.com/v1/chat/completions", headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}, json={"model": "gpt-4o", "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}", "detail": "high"}}]}], "max_tokens": 2000, "temperature": 0.3})
    if r.status_code != 200: raise ValueError(f"OpenAI {r.status_code}")
    return r.json()["choices"][0]["message"]["content"]


# ─── Subscriptions ────────────────────────────────────────
PLANS = {
    "starter": {"name": "Starter", "price_cents": 3900, "months": 1, "daily_limit": 5},
    "pro": {"name": "Pro", "price_cents": 7900, "months": 1, "daily_limit": 10},
}

@app.post("/v1/subscribe")
async def subscribe(req: SubscribeReq, request: Request):
    user = await get_current_user(request)
    plan = PLANS.get(req.plan)
    if not plan: raise HTTPException(400, "Invalid plan")
    if not STRIPE_SECRET: raise HTTPException(500, "Payments not configured")
    import stripe
    stripe.api_key = STRIPE_SECRET
    session = stripe.checkout.Session.create(payment_method_types=["card"], line_items=[{"price_data": {"currency": "usd", "product_data": {"name": f"ChartPulse AI — {plan['name']}"}, "unit_amount": plan["price_cents"]}, "quantity": 1}], mode="payment", success_url="https://www.chartpulse.world/success", cancel_url="https://www.chartpulse.world/dashboard", metadata={"user_email": user["email"], "plan_name": plan["name"], "months": str(plan["months"]), "daily_limit": str(plan["daily_limit"])})
    return {"checkout_url": session.url}

@app.post("/v1/stripe/webhook")
async def stripe_webhook(request: Request):
    import stripe
    stripe.api_key = STRIPE_SECRET
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try: event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e: raise HTTPException(400, str(e))
    if event["type"] == "checkout.session.completed":
        meta = event["data"]["object"].get("metadata", {})
        email = meta.get("user_email")
        if email:
            rows = await sb("GET", "users", params={"email": f"eq.{email}", "select": "id"})
            if rows:
                await sb("PATCH", "users", {"plan": meta.get("plan_name", ""), "daily_limit": int(meta.get("daily_limit", 5)), "plan_expires": (datetime.utcnow() + timedelta(days=int(meta.get("months", 1)) * 30)).isoformat()}, params={"id": f"eq.{rows[0]['id']}"})
    return {"received": True}


# ─── Dashboard ────────────────────────────────────────────
@app.get("/v1/stats")
async def stats(request: Request):
    user = await get_current_user(request)
    plan, cfg = get_plan_config(user)
    signals_left = await calc_signals_left(user, plan, cfg)
    all_a = await sb("GET", "analyses", params={"user_id": f"eq.{user['id']}", "select": "id"})
    total = len(all_a) if all_a else 0
    expires = user.get("plan_expires")
    es = "—"
    if expires:
        try:
            ed = datetime.fromisoformat(expires)
            if ed < datetime.utcnow(): plan, es = "Free", "Expired"; await sb("PATCH", "users", {"plan": "Free", "daily_limit": 3}, params={"id": f"eq.{user['id']}"})
            else: es = ed.strftime("%b %d, %Y")
        except: pass
    return {"plan": plan, "signals_left": signals_left, "total_analyses": total, "expires": es, "plan_config": cfg}

@app.get("/v1/user/history")
async def history(request: Request):
    user = await get_current_user(request)
    plan, cfg = get_plan_config(user)
    limit = cfg.get("history_limit") or 50
    if limit == 0: limit = 50
    rows = await sb("GET", "analyses", params={"user_id": f"eq.{user['id']}", "select": "symbol,direction,confidence,timeframe,entry_price,tp_price,sl_price,created_at", "order": "created_at.desc", "limit": str(limit)})
    return {"analyses": rows or []}

@app.get("/v1/user/plan-config")
async def plan_config(request: Request):
    user = await get_current_user(request)
    plan, cfg = get_plan_config(user)
    return {"plan": plan, "config": cfg}

@app.get("/v1/detect-country")
async def detect_country_ep(request: Request):
    c, cc = await detect_country(request)
    return {"country": c, "country_code": cc}

@app.get("/health")
async def health():
    return {"status": "ok", "db": "supabase", "version": "3.0"}
