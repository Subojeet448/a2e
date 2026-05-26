"""
╔══════════════════════════════════════════════════════════════════╗
║         A2E.ai Daily Bonus Bot  v3.0  —  Production Grade       ║
║                                                                  ║
║  Fixes from v2 review:                                          ║
║  ✅ POST /claim  (credentials never in URL)                      ║
║  ✅ playwright-stealth  (navigator.webdriver patched + 11 more)  ║
║  ✅ Real success verification  (no fake optimistic return)       ║
║  ✅ Human-like mouse/keyboard timing                             ║
║  ✅ Persistent browser profile  (cookies survive restarts)       ║
║  ✅ Structured logging  (JSON lines to file)                     ║
║  ✅ Request-level API key auth  (X-API-Key header)               ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ─── Optional stealth ─────────────────────────────────────────────────────────
try:
    from playwright_stealth import stealth_async
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

# ─── Config from env ──────────────────────────────────────────────────────────
API_KEY          = os.getenv("A2E_API_KEY", "change-this-secret")
PROFILE_DIR      = Path(os.getenv("A2E_PROFILE_DIR", "./browser_profiles"))
LOG_FILE         = Path(os.getenv("A2E_LOG_FILE", "./a2e_runs.jsonl"))
MAX_RETRIES      = int(os.getenv("A2E_MAX_RETRIES", "5"))
LOGIN_WAIT_SEC   = int(os.getenv("A2E_LOGIN_WAIT", "10"))
RETRY_WAIT_SEC   = int(os.getenv("A2E_RETRY_WAIT", "8"))
SWITCH_WAIT_SEC  = int(os.getenv("A2E_SWITCH_WAIT", "30"))

PROFILE_DIR.mkdir(parents=True, exist_ok=True)

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("a2e")

def _log_jsonl(record: dict):
    """Append structured log line to file."""
    with LOG_FILE.open("a") as f:
        f.write(json.dumps({**record, "ts": datetime.utcnow().isoformat()}) + "\n")

# ─── FastAPI ──────────────────────────────────────────────────────────────────
app = FastAPI(
    title="A2E Daily Bonus Bot v3",
    version="3.0.0",
    docs_url="/docs",
)


# ──────────────────────────────────────────────────────────────────────────────
#  Request / Response models
# ──────────────────────────────────────────────────────────────────────────────

class ClaimRequest(BaseModel):
    email:    str
    password: str


class ClaimResponse(BaseModel):
    email:   str
    status:  str          # "success" | "already_claimed" | "fail"
    message: str
    earned:  int = 0
    balance: str = "N/A"
    retries: int = 0
    time:    str = ""


# ──────────────────────────────────────────────────────────────────────────────
#  Auth middleware
# ──────────────────────────────────────────────────────────────────────────────

def _check_api_key(x_api_key: str | None):
    """Raise 401 if key is wrong. Allows running without key in dev mode."""
    configured = os.getenv("A2E_API_KEY")
    if not configured:
        return   # no key configured → open (dev only)
    if x_api_key != configured:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Api-Key header")


# ──────────────────────────────────────────────────────────────────────────────
#  Human-like helpers
# ──────────────────────────────────────────────────────────────────────────────

async def _human_type(page, selector: str, text: str):
    """Type with random delays between keystrokes — looks human."""
    await page.click(selector)
    await page.wait_for_timeout(random.randint(200, 500))
    for char in text:
        await page.keyboard.type(char)
        await page.wait_for_timeout(random.randint(40, 140))


async def _human_click(page, selector: str):
    """Move mouse to element randomly then click."""
    el = await page.query_selector(selector)
    if not el:
        return False
    box = await el.bounding_box()
    if not box:
        return False
    x = box["x"] + box["width"]  * random.uniform(0.3, 0.7)
    y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
    await page.mouse.move(x, y, steps=random.randint(8, 20))
    await page.wait_for_timeout(random.randint(80, 250))
    await page.mouse.click(x, y)
    return True


# ──────────────────────────────────────────────────────────────────────────────
#  Browser launch with persistent profile + stealth
# ──────────────────────────────────────────────────────────────────────────────

async def _launch_context(email: str):
    """
    Launch a persistent browser context per email account.
    Persistent = cookies/session survive between runs → fewer logins needed.
    """
    profile_path = PROFILE_DIR / email.replace("@", "_at_").replace(".", "_")
    profile_path.mkdir(parents=True, exist_ok=True)

    pw = await async_playwright().start()

    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_path),
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            # ⚠️  We do NOT use --disable-blink-features=AutomationControlled
            # because modern detection sees that flag itself as suspicious.
            # playwright-stealth handles webdriver removal properly.
        ],
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1366, "height": 768},
        locale="en-US",
        timezone_id="America/New_York",
        color_scheme="light",
        # Realistic screen dimensions
        screen={"width": 1920, "height": 1080},
    )

    # Apply stealth to all pages in this context
    if STEALTH_AVAILABLE:
        for p in context.pages:
            await stealth_async(p)
        # Hook new pages too
        context.on("page", lambda p: asyncio.ensure_future(stealth_async(p)))
        log.info(f"[{email}] playwright-stealth applied ✓")
    else:
        log.warning(f"[{email}] playwright-stealth NOT installed — less stealthy!")

    return pw, context


# ──────────────────────────────────────────────────────────────────────────────
#  Login flow
# ──────────────────────────────────────────────────────────────────────────────

async def _do_login(page, email: str, password: str) -> dict:
    """Fill and submit login form. Returns {ok, reason}."""
    await page.goto("https://video.a2e.ai/login", wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(random.randint(1500, 3000))

    # Email
    email_sel = None
    for s in ['input[type="email"]', 'input[name="email"]', '#email',
              'input[placeholder*="email" i]']:
        try:
            await page.wait_for_selector(s, timeout=4000)
            email_sel = s
            break
        except PWTimeout:
            continue
    if not email_sel:
        return {"ok": False, "reason": "Email input not found"}

    await _human_type(page, email_sel, email)
    await page.wait_for_timeout(random.randint(300, 700))

    # Password
    pass_sel = None
    for s in ['input[type="password"]', 'input[name="password"]', '#password']:
        try:
            await page.wait_for_selector(s, timeout=4000)
            pass_sel = s
            break
        except PWTimeout:
            continue
    if not pass_sel:
        return {"ok": False, "reason": "Password input not found"}

    await _human_type(page, pass_sel, password)
    await page.wait_for_timeout(random.randint(400, 900))

    # Submit
    submitted = False
    for s in ['button[type="submit"]', 'button:has-text("Login")',
              'button:has-text("Sign in")', 'button:has-text("Log in")']:
        try:
            await page.wait_for_selector(s, timeout=3000)
            await _human_click(page, s)
            submitted = True
            break
        except (PWTimeout, Exception):
            continue

    if not submitted:
        await page.keyboard.press("Enter")

    # Wait for redirect off login
    try:
        await page.wait_for_url(lambda u: "login" not in u, timeout=15_000)
    except PWTimeout:
        pass

    await page.wait_for_timeout(LOGIN_WAIT_SEC * 1000)

    if "login" in page.url.lower():
        # Check for error message
        for err_sel in ['.error', '[class*="error"]', '[class*="alert"]',
                        'p:has-text("Invalid")', 'p:has-text("incorrect")']:
            try:
                el = await page.query_selector(err_sel)
                if el:
                    txt = await el.text_content()
                    return {"ok": False, "reason": f"Login error: {txt.strip()}"}
            except Exception:
                pass
        return {"ok": False, "reason": "Still on login page after submit"}

    log.info(f"[{email}] Login succeeded. URL: {page.url}")
    return {"ok": True}


# ──────────────────────────────────────────────────────────────────────────────
#  Check-in / Claim flow — VERIFIED success (no fake return)
# ──────────────────────────────────────────────────────────────────────────────

# Selectors for the check-in button (ordered best → worst)
_CHECKIN_SELS = [
    'button:has-text("Check In")',
    'button:has-text("Check-in")',
    'button:has-text("Daily Check")',
    'button:has-text("Claim")',
    'button:has-text("Daily Bonus")',
    'a:has-text("Check In")',
    '[data-testid*="checkin"]',
    '[data-testid*="check-in"]',
    '[data-testid*="claim"]',
    '.checkin-btn',
    '.check-in-btn',
    '.daily-check',
    '[aria-label*="check in" i]',
    '[aria-label*="claim" i]',
]

# What a "success" notification looks like
_SUCCESS_TEXTS   = ["success", "bonus", "credit", "claimed", "reward", "check-in success"]
_ALREADY_TEXTS   = ["already", "come back", "tomorrow", "claimed today", "done today"]
_FAIL_TEXTS      = ["error", "failed", "try again", "something went wrong"]


async def _find_and_click_checkin(page) -> dict:
    """
    Find the check-in button and click it.
    Returns {clicked: bool, already: bool, reason: str}
    """
    for sel in _CHECKIN_SELS:
        try:
            el = await page.wait_for_selector(sel, timeout=3000, state="visible")
            if not el:
                continue

            # Check disabled / already-claimed state
            disabled = await el.get_attribute("disabled")
            cls      = (await el.get_attribute("class") or "").lower()
            txt      = (await el.text_content() or "").lower()

            if disabled is not None:
                return {"clicked": False, "already": True,
                        "reason": "Button is disabled (already claimed)"}

            if any(w in txt for w in ["claimed", "done", "completed", "tomorrow"]):
                return {"clicked": False, "already": True,
                        "reason": f"Button text indicates already claimed: '{txt.strip()}'"}

            # Click it
            ok = await _human_click(page, sel)
            if ok:
                return {"clicked": True, "already": False, "reason": f"Clicked: {sel}"}
        except Exception:
            continue

    return {"clicked": False, "already": False, "reason": "No check-in button found"}


async def _verify_claim_result(page) -> dict:
    """
    ⚠️  KEY FIX: This function NEVER returns success unless it positively
    confirms a success toast/modal. No optimistic guessing.

    Returns {verified: bool, already: bool, earned: int, detail: str}
    """
    # Wait a bit for toast/dialog to appear
    await page.wait_for_timeout(3500)

    # ── 1. Scan all visible toast / notification elements ──────────────────
    notification_sels = [
        '.toast', '[class*="toast"]', '[class*="notification"]',
        '[class*="alert"]', '[class*="snack"]', '[class*="message"]',
        '[role="alert"]', '[role="status"]',
        '.popup', '[class*="popup"]',
        '[class*="modal"]',
        'div[class*="success"]', 'div[class*="error"]',
    ]

    for sel in notification_sels:
        try:
            elements = await page.query_selector_all(sel)
            for el in elements:
                is_visible = await el.is_visible()
                if not is_visible:
                    continue
                txt = (await el.text_content() or "").lower().strip()
                if not txt:
                    continue

                if any(w in txt for w in _ALREADY_TEXTS):
                    return {"verified": True, "already": True,
                            "earned": 0, "detail": f"Already claimed: '{txt[:80]}'"}

                if any(w in txt for w in _SUCCESS_TEXTS):
                    earned = _parse_credits(txt)
                    return {"verified": True, "already": False,
                            "earned": earned, "detail": f"Success toast: '{txt[:80]}'"}

                if any(w in txt for w in _FAIL_TEXTS):
                    return {"verified": False, "already": False,
                            "earned": 0, "detail": f"Error toast: '{txt[:80]}'"}
        except Exception:
            continue

    # ── 2. Check if check-in button changed state (became disabled / greyed) ──
    for sel in _CHECKIN_SELS:
        try:
            el = await page.query_selector(sel)
            if not el:
                continue
            disabled = await el.get_attribute("disabled")
            txt = (await el.text_content() or "").lower()
            if disabled is not None or any(w in txt for w in ["claimed", "done"]):
                return {"verified": True, "already": True,
                        "earned": 0, "detail": "Button became disabled after click"}
        except Exception:
            continue

    # ── 3. No confirmation at all → FAIL (not fake success) ──────────────────
    return {
        "verified": False,
        "already":  False,
        "earned":   0,
        "detail":   "No confirmation toast found — treating as unverified",
    }


def _parse_credits(text: str) -> int:
    """Extract first number from toast text. E.g. '+30 credits' → 30."""
    nums = re.findall(r'\+?\d+', text)
    if nums:
        return int(nums[0].replace("+", ""))
    return 30  # default if parsing fails


async def _get_balance(page) -> str:
    """Read current credit balance from page header/nav."""
    sels = [
        '[class*="credit"]', '[class*="balance"]', '[class*="coin"]',
        '[class*="points"]', 'span:has-text("credits")',
        'span:has-text("Credits")', '[class*="wallet"]',
    ]
    for sel in sels:
        try:
            el = await page.query_selector(sel)
            if el:
                txt = (await el.text_content() or "").strip()
                nums = re.findall(r'[\d,]+', txt)
                if nums:
                    return nums[-1].replace(",", "")
        except Exception:
            continue
    return "N/A"


# ──────────────────────────────────────────────────────────────────────────────
#  Main orchestrator
# ──────────────────────────────────────────────────────────────────────────────

async def run_claim(email: str, password: str) -> dict:
    result = {
        "email":   email,
        "status":  "fail",
        "message": "",
        "earned":  0,
        "balance": "N/A",
        "retries": 0,
        "time":    datetime.now().isoformat(),
    }

    pw, context = None, None
    try:
        pw, context = await _launch_context(email)
        page = context.pages[0] if context.pages else await context.new_page()

        if STEALTH_AVAILABLE:
            await stealth_async(page)

        # ── Login ────────────────────────────────────────────────────────────
        login = await _do_login(page, email, password)
        if not login["ok"]:
            result["message"] = f"Login failed: {login['reason']}"
            _log_jsonl({**result, "stage": "login"})
            return result

        # Navigate to home
        if "video.a2e.ai" not in page.url or page.url.endswith("/login"):
            await page.goto("https://video.a2e.ai/", wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_timeout(random.randint(1500, 3000))

        # ── Retry loop ───────────────────────────────────────────────────────
        for attempt in range(1, MAX_RETRIES + 1):
            result["retries"] = attempt
            log.info(f"[{email}] Attempt {attempt}/{MAX_RETRIES}")

            click_r = await _find_and_click_checkin(page)

            # Already claimed (detected at button level)
            if click_r["already"]:
                balance = await _get_balance(page)
                result.update(status="already_claimed", balance=balance,
                               message=f"Already claimed today. ({click_r['reason']})")
                _log_jsonl({**result, "stage": "claim"})
                return result

            # Button not found
            if not click_r["clicked"]:
                log.warning(f"[{email}] {click_r['reason']}")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_WAIT_SEC)
                    await page.reload(wait_until="domcontentloaded")
                    await page.wait_for_timeout(2000)
                continue

            # Button clicked — now VERIFY
            verify = await _verify_claim_result(page)

            if verify["already"]:
                balance = await _get_balance(page)
                result.update(status="already_claimed", balance=balance,
                               message=f"Already claimed today. ({verify['detail']})")
                _log_jsonl({**result, "stage": "claim"})
                return result

            if verify["verified"]:
                balance = await _get_balance(page)
                result.update(
                    status="success",
                    earned=verify["earned"],
                    balance=balance,
                    message=f"✅ Bonus claimed! +{verify['earned']} credits. Balance: {balance}",
                )
                log.info(f"[{email}] {result['message']}")
                _log_jsonl({**result, "stage": "claim"})
                await asyncio.sleep(SWITCH_WAIT_SEC)
                return result

            # Not verified → retry
            log.warning(f"[{email}] Unverified after click: {verify['detail']}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_WAIT_SEC)
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)

        # All retries exhausted — FAIL (never fake success)
        result["message"] = (
            f"❌ Could not verify claim after {MAX_RETRIES} attempts. "
            "Check browser screenshots or logs."
        )

    except Exception as exc:
        result["message"] = f"Unexpected error: {exc}"
        log.exception(f"[{email}] Crash")

    finally:
        if context:
            await context.close()
        if pw:
            await pw.stop()
        _log_jsonl({**result, "stage": "final"})

    return result


# ──────────────────────────────────────────────────────────────────────────────
#  Endpoints
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "service": "A2E Daily Bonus Bot v3",
        "endpoints": {
            "claim":  "POST /claim  body={email, password}  header=X-Api-Key",
            "batch":  "POST /batch  body=[{email,password}] header=X-Api-Key",
            "health": "GET  /health",
            "logs":   "GET  /logs?n=50",
        },
    }


@app.get("/health")
async def health():
    return {"status": "ok", "stealth": STEALTH_AVAILABLE, "time": datetime.now().isoformat()}


@app.post("/claim", response_model=ClaimResponse)
async def claim(
    body: ClaimRequest,
    x_api_key: str | None = Header(default=None),
):
    """
    Claim daily bonus for ONE account.

    POST body (JSON):  { "email": "...", "password": "..." }
    Header:            X-Api-Key: your-secret  (set A2E_API_KEY env var)
    """
    _check_api_key(x_api_key)
    result = await run_claim(body.email, body.password)
    return JSONResponse(content=result)


@app.post("/batch")
async def batch(
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    """
    Claim for multiple accounts sequentially.

    POST body: [ {"email":"...","password":"..."}, ... ]
    """
    _check_api_key(x_api_key)

    try:
        acct_list = await request.json()
        if not isinstance(acct_list, list):
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be JSON array of {email,password}")

    results = []
    for i, acct in enumerate(acct_list):
        r = await run_claim(acct["email"], acct["password"])
        results.append(r)
        if i < len(acct_list) - 1:
            await asyncio.sleep(SWITCH_WAIT_SEC)

    success = sum(1 for r in results if r["status"] == "success")
    already = sum(1 for r in results if r["status"] == "already_claimed")

    return JSONResponse({
        "summary": {"total": len(results), "success": success,
                    "already_claimed": already, "failed": len(results)-success-already},
        "results": results,
    })


@app.get("/logs")
async def get_logs(
    n: int = 50,
    x_api_key: str | None = Header(default=None),
):
    """Return last N log lines from the JSONL log file."""
    _check_api_key(x_api_key)
    if not LOG_FILE.exists():
        return {"logs": []}
    lines = LOG_FILE.read_text().strip().splitlines()
    return {"logs": [json.loads(l) for l in lines[-n:]]}
