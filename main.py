"""
ChartPulse AI — Backend Proxy API
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
import secrets, json, time, httpx, os

from pydantic import BaseModel, EmailStr
from typing import List
from jose import jwt
from passlib.context import CryptContext

USERS = {}
TRANSACTIONS = []
ANALYSES = []

SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
STRIPE_SECRET = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
SIGNUP_BONUS = 10

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

app = FastAPI(title="ChartPulse AI API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def create_token(user_id: str, hours: int = 24) -> str:
    return jwt.encode(
        {"sub": user_id, "exp": datetime.utcnow() + timedelta(hours=hours)},
        SECRET_KEY, algorithm="HS256"
    )

def verify_token(token: str) -> str:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload.get("sub")
    except:
        raise HTTPException(401, "Invalid or expired token")

def get_current_user(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    user_id = verify_token(auth[7:])
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
    api_key = secrets.token_hex(24)

    USERS[req.email] = {
        "id": user_id,
        "email": req.email,
        "password_hash": pwd_context.hash(req.password[:72]),
        "credits": SIGNUP_BONUS,
        "api_key": api_key,
        "plan": "Free",
        "daily_limit": 5,
        "plan_expires": None,
        "created": datetime.utcnow().isoformat(),
    }

    TRANSACTIONS.append({
        "user_id": user_id,
        "amount": SIGNUP_BONUS,
        "type": "bonus",
        "description": "Welcome bonus",
        "timestamp": datetime.utcnow().isoformat(),
    })

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
    if not user or not pwd_context.verify(req.password[:72], user["password_hash"]):
        raise HTTPException(401, "Invalid credentials")

    today = datetime.utcnow().date().isoformat()
    today_count = sum(
        1 for a in ANALYSES
        if a["user_id"] == user["id"] and a["timestamp"].startswith(today)
    )
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
    "price_action": "Analyze using pure price action: candlestick patterns (doji, hammer, engulfing, pin bars), market structure (HH/HL/LH/LL), trend direction, and key swing levels.",
    "smc": "Analyze using Smart Money Concepts (SMC): order blocks, fair value gaps (FVGs), liquidity sweeps, breaker blocks, mitigation blocks, and institutional order flow.",
    "supply_demand": "Analyze using supply and demand zones: fresh vs tested zones, zone quality (strength of departure), drop-base-rally/rally-base-drop formations.",
    "fibonacci": "Analyze using Fibonacci retracements (0.382, 0.5, 0.618, 0.786) and extensions (1.272, 1.618). Identify if price is at a key fib level.",
    "ema_ma": "Analyze using moving averages: EMA 9/20/50/200 positions, golden/death crosses, EMA pullbacks, dynamic support/resistance.",
    "wyckoff": "Analyze using Wyckoff methodology: accumulation/distribution phases, spring/upthrust, signs of strength/weakness.",
    "elliott": "Analyze using Elliott Wave Theory: identify current wave count (impulse 1-5 or corrective A-B-C), project targets.",
    "ichimoku": "Analyze using Ichimoku Kinko Hyo: Tenkan/Kijun cross, price vs cloud, cloud twist, Chikou span confirmation.",
    "volume": "Analyze using volume concepts: volume spikes, VWAP position, point of control (POC), high/low volume nodes.",
    "harmonic": "Analyze using Harmonic patterns: Gartley, Bat, Butterfly, Crab, Cypher. Identify if a pattern is forming or completing.",
}

PERSONA_PROMPTS = {
    "scalper": "You are analyzing for a SCALPER (1m-5m charts, tight SL, 5-20 pip targets).",
    "intraday": "You are analyzing for an INTRADAY trader (15m-1H charts, moderate SL, 20-100 pip targets).",
    "swing": "You are analyzing for a SWING trader (4H-Daily charts, wider SL, 100-500 pip targets).",
    "position": "You are analyzing for a POSITION trader (Daily-Weekly charts, very wide SL, weeks/months hold).",
}

LANG_PROMPTS = {
    "en": "Respond in English.",
    "ar": "Respond in Arabic.",
    "fr": "Respond in French.",
    "es": "Respond in Spanish.",
    "zh": "Respond in Chinese.",
    "tr": "Respond in Turkish.",
}

def build_prompt(strategies, persona, language):
    strat_text = "\n\n".join(
        f"{i+1}. **{s.replace('_', ' ').title()}**: {STRATEGY_PROMPTS.get(s, '')}"
        for i, s in enumerate(strategies) if s in STRATEGY_PROMPTS
    )
    return f"""You are ChartPulse AI, an expert multi-strategy trading analyst. Analyze this trading chart screenshot using these strategies simultaneously:

{strat_text}

{PERSONA_PROMPTS.get(persona, PERSONA_PROMPTS['intraday'])}

{LANG_PROMPTS.get(language, LANG_PROMPTS['en'])}

After analyzing with each strategy independently, provide a UNIFIED CONSENSUS signal.

RESPOND ONLY IN THIS EXACT JSON FORMAT (no markdown, no backticks, just raw JSON):
{{
  "symbol": "detected symbol or UNKNOWN",
  "timeframe": "detected timeframe",
  "consensus": {{
    "direction": "LONG" or "SHORT" or "NO TRADE",
    "confidence": number between 0-100,
    "entry": "exact price level",
    "take_profit": "exact price level",
    "stop_loss": "exact price level",
    "risk_reward": "ratio like 1:2.5"
  }},
  "strategies": [
    {{
      "name": "strategy name",
      "verdict": "LONG" or "SHORT" or "NEUTRAL",
      "confidence": number 1-5,
      "key_finding": "one sentence summary"
    }}
  ],
  "reasoning": "2-3 sentence explanation of why the consensus was reached"
}}"""


@app.post("/v1/analyze")
async def analyze_chart(req: AnalyzeReq, request: Request):
    user = get_current_user(request)

    plan = user.get("plan", "Free")
    daily_limit = user.get("daily_limit", 5)
    max_strategies = 3 if plan == "Free" else 10

    plan_expires = user.get("plan_expires")
    if plan_expires:
        try:
            if datetime.fromisoformat(plan_expires) < datetime.utcnow():
                plan = "Free"
                daily_limit = 5
                max_strategies = 3
                user["plan"] = "Free"
                user["daily_limit"] = 5
        except:
            pass

    today = datetime.utcnow().date().isoformat()
    today_count = sum(
        1 for a in ANALYSES
        if a["user_id"] == user["id"] and a["timestamp"].startswith(today)
    )

    if today_count >= daily_limit:
        raise HTTPException(402, f"Daily limit reached ({daily_limit} analyses). Upgrade your plan.")

    strategies = req.strategies[:max_strategies]

    if not strategies:
        raise HTTPException(400, "Select at least one strategy")

    if len(req.image_base64) < 100:
        raise HTTPException(400, "Invalid image data")

    start = time.time()
    prompt = build_prompt(strategies, req.persona, req.language)

    result = None
    providers = [
        ("claude", call_claude, ANTHROPIC_API_KEY),
        ("gemini", call_gemini, GEMINI_API_KEY),
        ("openai", call_openai, OPENAI_API_KEY),
    ]
    last_error = None
    for name, fn, key in providers:
        if not key:
            continue
        try:
            result = await fn(req.image_base64, prompt)
            break
        except Exception as e:
            last_error = e
            continue

    if result is None:
        raise HTTPException(500, f"Analysis failed: {str(last_error) if last_error else 'No AI provider configured'}")

    analysis = parse_json_response(result)
    elapsed_ms = int((time.time() - start) * 1000)

    TRANSACTIONS.append({
        "user_id": user["id"],
        "amount": 0,
        "type": "analysis",
        "description": f"Chart analysis: {analysis.get('symbol', 'Unknown')}",
        "timestamp": datetime.utcnow().isoformat(),
    })

    ANALYSES.append({
        "user_id": user["id"],
        "symbol": analysis.get("symbol", "Unknown"),
        "direction": analysis.get("consensus", {}).get("direction", ""),
        "confidence": analysis.get("consensus", {}).get("confidence", 0),
        "timestamp": datetime.utcnow().isoformat(),
    })

    return {
        "analysis": analysis,
        "analyses_remaining": daily_limit - today_count - 1,
        "plan": plan,
        "execution_ms": elapsed_ms,
    }


async def call_claude(image_b64, prompt):
    if not ANTHROPIC_API_KEY:
        raise ValueError("Claude API key not configured")
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_b64}},
                    {"type": "text", "text": prompt}
                ]}],
            }
        )
    if resp.status_code != 200:
        raise ValueError(f"Claude error {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    text_block = next((b for b in data.get("content", []) if b["type"] == "text"), None)
    if not text_block:
        raise ValueError("No text response from Claude")
    return text_block["text"]


async def call_gemini(image_b64, prompt):
    if not GEMINI_API_KEY:
        raise ValueError("Gemini API key not configured")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json={
            "contents": [{"parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/png", "data": image_b64}}
            ]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2000},
        })
    if resp.status_code != 200:
        raise ValueError(f"Gemini error {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


async def call_openai(image_b64, prompt):
    if not OPENAI_API_KEY:
        raise ValueError("OpenAI API key not configured")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}", "detail": "high"}}
                ]}],
                "max_tokens": 2000,
                "temperature": 0.3,
            }
        )
    if resp.status_code != 200:
        raise ValueError(f"OpenAI error {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def parse_json_response(raw):
    import re
    clean = re.sub(r'```json\s*', '', raw)
    clean = re.sub(r'```\s*', '', clean).strip()
    match = re.search(r'\{[\s\S]*\}', clean)
    if not match:
        raise ValueError("Could not parse AI response")
    return json.loads(match.group(0))


SUBSCRIPTION_PLANS = {
    "m1": {"name": "1 Month", "price_cents": 700, "months": 1, "daily_limit": 50},
    "m3": {"name": "3 Months", "price_cents": 1500, "months": 3, "daily_limit": 9999},
    "m6": {"name": "6 Months", "price_cents": 2400, "months": 6, "daily_limit": 9999},
}

@app.post("/v1/subscribe")
async def subscribe(req: SubscribeReq, request: Request):
    user = get_current_user(request)
    plan = SUBSCRIPTION_PLANS.get(req.plan)
    if not plan:
        raise HTTPException(400, f"Invalid plan: {req.plan}")
    if not STRIPE_SECRET:
        raise HTTPException(500, "Payments not configured yet")
    import stripe
    stripe.api_key = STRIPE_SECRET
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": f"ChartPulse AI — {plan['name']}"},
                "unit_amount": plan["price_cents"],
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url="http://www.khartoumbar.com/success.html?session_id={CHECKOUT_SESSION_ID}",
        cancel_url="http://www.khartoumbar.com/dashboard.html",
        metadata={
            "user_id": user["id"],
            "user_email": user["email"],
            "plan": req.plan,
            "plan_name": plan["name"],
            "months": str(plan["months"]),
            "daily_limit": str(plan["daily_limit"]),
        },
    )
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
        raise HTTPException(400, f"Webhook error: {str(e)}")
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        meta = session.get("metadata", {})
        user_email = meta.get("user_email")
        plan_name = meta.get("plan_name", "")
        months = int(meta.get("months", 1))
        daily_limit = int(meta.get("daily_limit", 50))
        user = USERS.get(user_email)
        if user:
            user["plan"] = plan_name
            user["daily_limit"] = daily_limit
            user["plan_expires"] = (datetime.utcnow() + timedelta(days=months * 30)).isoformat()
            TRANSACTIONS.append({
                "user_id": user["id"],
                "amount": 0,
                "type": "subscription",
                "description": f"Subscribed to {plan_name}",
                "timestamp": datetime.utcnow().isoformat(),
            })
    return {"received": True}


@app.get("/v1/stats")
async def user_stats(request: Request):
    user = get_current_user(request)
    today = datetime.utcnow().date().isoformat()
    today_analyses = sum(
        1 for a in ANALYSES
        if a["user_id"] == user["id"] and a["timestamp"].startswith(today)
    )
    plan = user.get("plan", "Free")
    expires = user.get("plan_expires")
    if expires:
        try:
            exp_date = datetime.fromisoformat(expires)
            if exp_date < datetime.utcnow():
                plan = "Free"
                expires = "Expired"
                user["plan"] = "Free"
                user["daily_limit"] = 5
            else:
                expires = exp_date.strftime("%b %d, %Y")
        except:
            expires = None
    return {
        "total_analyses": today_analyses,
        "plan": plan,
        "expires": expires or "—",
        "daily_limit": user.get("daily_limit", 5),
    }


@app.get("/v1/user/history")
async def user_history(request: Request):
    user = get_current_user(request)
    user_analyses = [a for a in ANALYSES if a["user_id"] == user["id"]]
    return {"analyses": user_analyses[-50:][::-1]}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "chartpulse-api"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
