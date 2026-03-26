"""
ChartPulse AI — Backend API with Supabase Database
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

app = FastAPI(title="ChartPulse AI API", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

async def sb(method, table, data=None, params=None):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=representation"}
    async with httpx.AsyncClient(timeout=15) as client:
        if method == "GET":
            r = await client.get(url, headers=headers, params=params or {})
        elif method == "POST":
            r = await client.post(url, headers=headers, json=data)
        elif method == "PATCH":
            r = await client.patch(url, headers=headers, json=data, params=params or {})
        else:
            raise ValueError(f"Unknown method: {method}")
    if r.status_code >= 400:
        raise ValueError(f"Supabase error {r.status_code}: {r.text[:300]}")
    try:
        return r.json()
    except:
        return None

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
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    try:
        uid = jwt.decode(auth[7:], SECRET_KEY, algorithms=["HS256"]).get("sub")
    except:
        raise HTTPException(401, "Invalid token")
    rows = await sb("GET", "users", params={"id": f"eq.{uid}", "select": "*"})
    if not rows:
        raise HTTPException(401, "User not found")
    return rows[0]

async def detect_country(request: Request):
    ip = request.headers.get("X-Forwarded-For", request.client.host)
    if ip and "," in ip:
        ip = ip.split(",")[0].strip()
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"http://ip-api.com/json/{ip}?fields=country,countryCode")
            if r.status_code == 200:
                d = r.json()
                return d.get("country", "Unknown"), d.get("countryCode", "")
    except:
        pass
    return "Unknown", ""

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

@app.post("/v1/auth/register")
async def register(req: RegisterReq, request: Request):
    existing = await sb("GET", "users", params={"email": f"eq.{req.email}", "select": "id"})
    if existing:
        raise HTTPException(400, "Email already registered")
    country = req.country or ""
    country_code = ""
    if not country:
        country, country_code = await detect_country(request)
    user_id = secrets.token_hex(16)
    await sb("POST", "users", {"id": user_id, "email": req.email, "password_hash": hash_password(req.password), "phone": req.phone or "", "country": country, "country_code": country_code, "plan": "Free", "daily_limit": 5, "plan_expires": None, "created_at": datetime.utcnow().isoformat()})
    return {"token": create_token(user_id), "user": {"id": user_id, "email": req.email, "plan": "Free", "daily_limit": 5, "analyses_remaining": 5}}

@app.post("/v1/auth/login")
async def login(req: LoginReq):
    rows = await sb("GET", "users", params={"email": f"eq.{req.email}", "select": "*"})
    if not rows or not verify_password(req.password, rows[0]["password_hash"]):
        raise HTTPException(401, "Invalid credentials")
    user = rows[0]
    today = datetime.utcnow().date().isoformat()
    analyses = await sb("GET", "analyses", params={"user_id": f"eq.{user['id']}", "created_at": f"gte.{today}T00:00:00", "select": "id"})
    remaining = max(0, (user.get("daily_limit") or 5) - (len(analyses) if analyses else 0))
    return {"token": create_token(user["id"]), "user": {"id": user["id"], "email": user["email"], "plan": user.get("plan", "Free"), "daily_limit": user.get("daily_limit", 5), "analyses_remaining": remaining}}

STRATEGY_PROMPTS = {"price_action": "Analyze using pure price action: candlestick patterns, market structure, trend direction, key swing levels.", "smc": "Analyze using Smart Money Concepts: order blocks, fair value gaps, liquidity sweeps, institutional order flow.", "supply_demand": "Analyze using supply and demand zones: fresh vs tested zones, zone quality, DBR/RBD.", "fibonacci": "Analyze using Fibonacci retracements (0.382, 0.5, 0.618, 0.786) and extensions.", "ema_ma": "Analyze using EMA 9/20/50/200, golden/death crosses, EMA pullbacks.", "wyckoff": "Analyze using Wyckoff: accumulation/distribution, spring/upthrust.", "elliott": "Analyze using Elliott Wave: wave count (impulse 1-5 or corrective A-B-C).", "ichimoku": "Analyze using Ichimoku: Tenkan/Kijun cross, cloud position, Chikou span.", "volume": "Analyze using volume: spikes, VWAP, POC, high/low volume nodes.", "harmonic": "Analyze using Harmonic patterns: Gartley, Bat, Butterfly, Crab, Cypher."}

def build_prompt(strategies, persona, language):
    pm = {"scalper": "SCALPER (1m-5m, tight SL)", "intraday": "INTRADAY trader (15m-1H)", "swing": "SWING trader (4H-Daily)", "position": "POSITION trader (Daily-Weekly)"}
    lm = {"en": "English", "ar": "Arabic", "fr": "French", "es": "Spanish", "zh": "Chinese", "tr": "Turkish"}
    st = "\n".join(f"{i+1}. {s.replace('_',' ').title()}: {STRATEGY_PROMPTS.get(s,'')}" for i, s in enumerate(strategies) if s in STRATEGY_PROMPTS)
    return f"""You are ChartPulse AI. Analyze this chart:
{st}
You are analyzing for a {pm.get(persona, pm['intraday'])}.
Respond in {lm.get(language, 'English')}.
RESPOND ONLY IN JSON (no markdown, no backticks):
{{"symbol":"symbol","timeframe":"tf","consensus":{{"direction":"LONG/SHORT/NO TRADE","confidence":0-100,"entry":"price","take_profit":"price","stop_loss":"price","risk_reward":"ratio"}},"strategies":[{{"name":"name","verdict":"LONG/SHORT/NEUTRAL","confidence":1-5,"key_finding":"summary"}}],"reasoning":"2-3 sentences"}}"""

@app.post("/v1/analyze")
async def analyze_chart(req: AnalyzeReq, request: Request):
    user = await get_current_user(request)
    plan = user.get("plan", "Free")
    dl = user.get("daily_limit") or 5
    ms = 3 if plan == "Free" else 10
    expires = user.get("plan_expires")
    if expires:
        try:
            if datetime.fromisoformat(expires) < datetime.utcnow():
                plan, dl, ms = "Free", 5, 3
                await sb("PATCH", "users", {"plan": "Free", "daily_limit": 5}, params={"id": f"eq.{user['id']}"})
        except: pass
    today = datetime.utcnow().date().isoformat()
    ta = await sb("GET", "analyses", params={"user_id": f"eq.{user['id']}", "created_at": f"gte.{today}T00:00:00", "select": "id"})
    tc = len(ta) if ta else 0
    if tc >= dl:
        raise HTTPException(402, "Daily limit reached. Upgrade your plan.")
    strategies = req.strategies[:ms]
    if not strategies:
        raise HTTPException(400, "Select at least one strategy")
    if len(req.image_base64) < 100:
        raise HTTPException(400, "Invalid image")
    start = time.time()
    prompt = build_prompt(strategies, req.persona, req.language)
    result = None
    last_error = None
    for n, fn, k in [("claude", call_claude, ANTHROPIC_API_KEY), ("gemini", call_gemini, GEMINI_API_KEY), ("openai", call_openai, OPENAI_API_KEY)]:
        if not k: continue
        try:
            result = await fn(req.image_base64, prompt)
            break
        except Exception as e:
            last_error = e
    if not result:
        raise HTTPException(500, f"Analysis failed: {last_error}")
    import re
    clean = re.sub(r'```json\s*', '', result)
    clean = re.sub(r'```\s*', '', clean).strip()
    match = re.search(r'\{[\s\S]*\}', clean)
    if not match:
        raise HTTPException(500, "Could not parse AI response")
    analysis = json.loads(match.group(0))
    ems = int((time.time() - start) * 1000)
    await sb("POST", "analyses", {"user_id": user["id"], "symbol": analysis.get("symbol", "?"), "direction": analysis.get("consensus", {}).get("direction", ""), "confidence": analysis.get("consensus", {}).get("confidence", 0), "timeframe": analysis.get("timeframe", ""), "entry_price": analysis.get("consensus", {}).get("entry", ""), "tp_price": analysis.get("consensus", {}).get("take_profit", ""), "sl_price": analysis.get("consensus", {}).get("stop_loss", ""), "execution_ms": ems, "created_at": datetime.utcnow().isoformat()})
    return {"analysis": analysis, "analyses_remaining": dl - tc - 1, "plan": plan, "execution_ms": ems}

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

PLANS = {"m1": {"name": "1 Month", "price_cents": 700, "months": 1, "daily_limit": 50}, "m3": {"name": "3 Months", "price_cents": 1500, "months": 3, "daily_limit": 9999}, "m6": {"name": "6 Months", "price_cents": 2400, "months": 6, "daily_limit": 9999}}

@app.post("/v1/subscribe")
async def subscribe(req: SubscribeReq, request: Request):
    user = await get_current_user(request)
    plan = PLANS.get(req.plan)
    if not plan: raise HTTPException(400, "Invalid plan")
    if not STRIPE_SECRET: raise HTTPException(500, "Payments not configured")
    import stripe
    stripe.api_key = STRIPE_SECRET
    session = stripe.checkout.Session.create(payment_method_types=["card"], line_items=[{"price_data": {"currency": "usd", "product_data": {"name": f"ChartPulse AI - {plan['name']}"}, "unit_amount": plan["price_cents"]}, "quantity": 1}], mode="payment", success_url="http://www.khartoumbar.com/success.html", cancel_url="http://www.khartoumbar.com/dashboard.html", metadata={"user_email": user["email"], "plan_name": plan["name"], "months": str(plan["months"]), "daily_limit": str(plan["daily_limit"])})
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
                await sb("PATCH", "users", {"plan": meta.get("plan_name", ""), "daily_limit": int(meta.get("daily_limit", 50)), "plan_expires": (datetime.utcnow() + timedelta(days=int(meta.get("months", 1)) * 30)).isoformat()}, params={"id": f"eq.{rows[0]['id']}"})
    return {"received": True}

@app.get("/v1/stats")
async def stats(request: Request):
    user = await get_current_user(request)
    today = datetime.utcnow().date().isoformat()
    a = await sb("GET", "analyses", params={"user_id": f"eq.{user['id']}", "created_at": f"gte.{today}T00:00:00", "select": "id"})
    plan = user.get("plan", "Free")
    expires = user.get("plan_expires")
    es = "—"
    if expires:
        try:
            ed = datetime.fromisoformat(expires)
            if ed < datetime.utcnow(): plan, es = "Free", "Expired"; await sb("PATCH", "users", {"plan": "Free", "daily_limit": 5}, params={"id": f"eq.{user['id']}"})
            else: es = ed.strftime("%b %d, %Y")
        except: pass
    return {"total_analyses": len(a) if a else 0, "plan": plan, "expires": es, "daily_limit": user.get("daily_limit") or 5}

@app.get("/v1/user/history")
async def history(request: Request):
    user = await get_current_user(request)
    rows = await sb("GET", "analyses", params={"user_id": f"eq.{user['id']}", "select": "symbol,direction,confidence,timeframe,entry_price,tp_price,sl_price,created_at", "order": "created_at.desc", "limit": "50"})
    return {"analyses": rows or []}

@app.get("/v1/detect-country")
async def detect_country_ep(request: Request):
    c, cc = await detect_country(request)
    return {"country": c, "country_code": cc}

@app.get("/health")
async def health():
    return {"status": "ok", "db": "supabase"}
