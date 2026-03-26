"""
ChartPulse AI — Backend API
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
import secrets, json, time, httpx, os, hashlib

from pydantic import BaseModel, EmailStr
from typing import List
from jose import jwt

USERS = {}
TRANSACTIONS = []
ANALYSES = []

SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
STRIPE_SECRET = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

app = FastAPI(title="ChartPulse AI API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{hashed}"

def verify_password(password: str, stored: str) -> bool:
    salt, hashed = stored.split(":")
    return hashlib.sha256((salt + password).encode()).hexdigest() == hashed

def create_token(user_id: str) -> str:
    return jwt.encode(
        {"sub": user_id, "exp": datetime.utcnow() + timedelta(hours=24)},
        SECRET_KEY, algorithm="HS256"
    )

def get_current_user(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    try:
        payload = jwt.decode(auth[7:], SECRET_KEY, algorithms=["HS256"])
        user_id = payload.get("sub")
    except:
        raise HTTPException(401, "Invalid token")
    user = next((u for u in USERS.values() if u["id"] == user_id), None)
    if not user:
        raise HTTPException(401, "User not found")
    return user


class RegisterReq(BaseModel):
    email: EmailStr
    password: str

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
async def register(req: RegisterReq):
    if req.email in USERS:
        raise HTTPException(400, "Email already registered")
    user_id = secrets.token_hex(16)
    USERS[req.email] = {
        "id": user_id,
        "email": req.email,
        "password_hash": hash_password(req.password),
        "plan": "Free",
        "daily_limit": 5,
        "plan_expires": None,
        "created": datetime.utcnow().isoformat(),
    }
    token = create_token(user_id)
    return {
        "token": token,
        "user": {
            "id": user_id,
            "email": req.email,
            "plan": "Free",
            "daily_limit": 5,
            "analyses_remaining": 5,
        }
    }


@app.post("/v1/auth/login")
async def login(req: LoginReq):
    user = USERS.get(req.email)
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "Invalid credentials")
    today = datetime.utcnow().date().isoformat()
    today_count = sum(1 for a in ANALYSES if a["user_id"] == user["id"] and a["timestamp"].startswith(today))
    remaining = max(0, user.get("daily_limit", 5) - today_count)
    token = create_token(user["id"])
    return {
        "token": token,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "plan": user.get("plan", "Free"),
            "daily_limit": user.get("daily_limit", 5),
            "analyses_remaining": remaining,
        }
    }


STRATEGY_PROMPTS = {
    "price_action": "Analyze using pure price action: candlestick patterns, market structure (HH/HL/LH/LL), trend direction, key swing levels.",
    "smc": "Analyze using Smart Money Concepts: order blocks, fair value gaps, liquidity sweeps, breaker blocks, institutional order flow.",
    "supply_demand": "Analyze using supply and demand zones: fresh vs tested zones, zone quality, drop-base-rally/rally-base-drop formations.",
    "fibonacci": "Analyze using Fibonacci retracements (0.382, 0.5, 0.618, 0.786) and extensions (1.272, 1.618).",
    "ema_ma": "Analyze using moving averages: EMA 9/20/50/200 positions, golden/death crosses, EMA pullbacks.",
    "wyckoff": "Analyze using Wyckoff methodology: accumulation/distribution phases, spring/upthrust.",
    "elliott": "Analyze using Elliott Wave Theory: current wave count (impulse 1-5 or corrective A-B-C).",
    "ichimoku": "Analyze using Ichimoku: Tenkan/Kijun cross, price vs cloud, cloud twist, Chikou span.",
    "volume": "Analyze using volume: volume spikes, VWAP, point of control, high/low volume nodes.",
    "harmonic": "Analyze using Harmonic patterns: Gartley, Bat, Butterfly, Crab, Cypher.",
}

def build_prompt(strategies, persona, language):
    persona_map = {
        "scalper": "You are analyzing for a SCALPER (1m-5m, tight SL, 5-20 pip targets).",
        "intraday": "You are analyzing for an INTRADAY trader (15m-1H, moderate SL, 20-100 pip targets).",
        "swing": "You are analyzing for a SWING trader (4H-Daily, wider SL, 100-500 pip targets).",
        "position": "You are analyzing for a POSITION trader (Daily-Weekly, very wide SL).",
    }
    lang_map = {"en": "Respond in English.", "ar": "Respond in Arabic.", "fr": "Respond in French.", "es": "Respond in Spanish.", "zh": "Respond in Chinese.", "tr": "Respond in Turkish."}
    strat_text = "\n".join(f"{i+1}. **{s.replace('_',' ').title()}**: {STRATEGY_PROMPTS.get(s,'')}" for i, s in enumerate(strategies) if s in STRATEGY_PROMPTS)
    return f"""You are ChartPulse AI. Analyze this chart using these strategies:

{strat_text}

{persona_map.get(persona, persona_map['intraday'])}
{lang_map.get(language, lang_map['en'])}

RESPOND ONLY IN THIS EXACT JSON FORMAT (no markdown, no backticks):
{{"symbol":"detected symbol","timeframe":"detected timeframe","consensus":{{"direction":"LONG or SHORT or NO TRADE","confidence":0-100,"entry":"price","take_profit":"price","stop_loss":"price","risk_reward":"ratio"}},"strategies":[{{"name":"name","verdict":"LONG or SHORT or NEUTRAL","confidence":1-5,"key_finding":"summary"}}],"reasoning":"2-3 sentences"}}"""


@app.post("/v1/analyze")
async def analyze_chart(req: AnalyzeReq, request: Request):
    user = get_current_user(request)
    plan = user.get("plan", "Free")
    daily_limit = user.get("daily_limit", 5)
    max_strats = 3 if plan == "Free" else 10

    expires = user.get("plan_expires")
    if expires:
        try:
            if datetime.fromisoformat(expires) < datetime.utcnow():
                plan, daily_limit, max_strats = "Free", 5, 3
                user["plan"], user["daily_limit"] = "Free", 5
        except:
            pass

    today = datetime.utcnow().date().isoformat()
    today_count = sum(1 for a in ANALYSES if a["user_id"] == user["id"] and a["timestamp"].startswith(today))
    if today_count >= daily_limit:
        raise HTTPException(402, "Daily limit reached. Upgrade your plan.")

    strategies = req.strategies[:max_strats]
    if not strategies:
        raise HTTPException(400, "Select at least one strategy")
    if len(req.image_base64) < 100:
        raise HTTPException(400, "Invalid image")

    start = time.time()
    prompt = build_prompt(strategies, req.persona, req.language)

    result = None
    last_error = None
    for name, fn, key in [("claude", call_claude, ANTHROPIC_API_KEY), ("gemini", call_gemini, GEMINI_API_KEY), ("openai", call_openai, OPENAI_API_KEY)]:
        if not key:
            continue
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

    ANALYSES.append({"user_id": user["id"], "symbol": analysis.get("symbol", "?"), "direction": analysis.get("consensus", {}).get("direction", ""), "confidence": analysis.get("consensus", {}).get("confidence", 0), "timestamp": datetime.utcnow().isoformat()})

    return {"analysis": analysis, "analyses_remaining": daily_limit - today_count - 1, "plan": plan, "execution_ms": int((time.time() - start) * 1000)}


async def call_claude(img, prompt):
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post("https://api.anthropic.com/v1/messages", headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}, json={"model": "claude-sonnet-4-20250514", "max_tokens": 2000, "messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}}, {"type": "text", "text": prompt}]}]})
    if r.status_code != 200:
        raise ValueError(f"Claude {r.status_code}: {r.text[:200]}")
    return next(b for b in r.json()["content"] if b["type"] == "text")["text"]

async def call_gemini(img, prompt):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}", json={"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "image/png", "data": img}}]}], "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2000}})
    if r.status_code != 200:
        raise ValueError(f"Gemini {r.status_code}")
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]

async def call_openai(img, prompt):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.openai.com/v1/chat/completions", headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}, json={"model": "gpt-4o", "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}", "detail": "high"}}]}], "max_tokens": 2000, "temperature": 0.3})
    if r.status_code != 200:
        raise ValueError(f"OpenAI {r.status_code}")
    return r.json()["choices"][0]["message"]["content"]


PLANS = {"m1": {"name": "1 Month", "price_cents": 700, "months": 1, "daily_limit": 50}, "m3": {"name": "3 Months", "price_cents": 1500, "months": 3, "daily_limit": 9999}, "m6": {"name": "6 Months", "price_cents": 2400, "months": 6, "daily_limit": 9999}}

@app.post("/v1/subscribe")
async def subscribe(req: SubscribeReq, request: Request):
    user = get_current_user(request)
    plan = PLANS.get(req.plan)
    if not plan:
        raise HTTPException(400, "Invalid plan")
    if not STRIPE_SECRET:
        raise HTTPException(500, "Payments not configured")
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
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(400, str(e))
    if event["type"] == "checkout.session.completed":
        meta = event["data"]["object"].get("metadata", {})
        user = USERS.get(meta.get("user_email"))
        if user:
            user["plan"] = meta.get("plan_name", "")
            user["daily_limit"] = int(meta.get("daily_limit", 50))
            user["plan_expires"] = (datetime.utcnow() + timedelta(days=int(meta.get("months", 1)) * 30)).isoformat()
    return {"received": True}

@app.get("/v1/stats")
async def stats(request: Request):
    user = get_current_user(request)
    today = datetime.utcnow().date().isoformat()
    count = sum(1 for a in ANALYSES if a["user_id"] == user["id"] and a["timestamp"].startswith(today))
    plan = user.get("plan", "Free")
    expires = user.get("plan_expires")
    exp_str = "—"
    if expires:
        try:
            ed = datetime.fromisoformat(expires)
            if ed < datetime.utcnow():
                plan, exp_str = "Free", "Expired"
                user["plan"], user["daily_limit"] = "Free", 5
            else:
                exp_str = ed.strftime("%b %d, %Y")
        except:
            pass
    return {"total_analyses": count, "plan": plan, "expires": exp_str, "daily_limit": user.get("daily_limit", 5)}

@app.get("/v1/user/history")
async def history(request: Request):
    user = get_current_user(request)
    return {"analyses": [a for a in ANALYSES if a["user_id"] == user["id"]][-50:][::-1]}

@app.get("/health")
async def health():
    return {"status": "ok", "service": "chartpulse-api"}
