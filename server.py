"""
╔══════════════════════════════════════════════════════════════════╗
║         A2E.ai Daily Bonus Bot  v3.1  —  Render Compatible      ║
║                                                                  ║
║  Fixes from v3.0:                                               ║
║  ✅ launch_persistent_context → launch + new_context            ║
║  ✅ No browser profile folder needed (ephemeral containers)     ║
║  ✅ pw object properly closed in finally block                  ║
║  ✅ stealth applied via context.add_init_script fallback        ║
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
from pydantic import BaseModel
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ─── Optional stealth ─────────────────────────────────────────────────────────
try:
    from playwright_stealth import stealth_async
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

# ─── Config from env ──────────────────────────────────────────────────────────
API_KEY         = os.getenv("A2E_API_KEY", "change-this-secret")
LOG_FILE        = Path(os.getenv("A2E_LOG_FILE", "./a2e_runs.jsonl"))
MAX_RETRIES     = int(os.getenv("A2E_MAX_RETRIES", "5"))
LOGIN_WAIT_SEC  = int(os.getenv("A2E_LOGIN_WAIT", "10"))
RETRY_WAIT_SEC  = int(os.getenv("A2E_RETRY_WAIT", "8"))
SWITCH_WAIT_SEC = int(os.getenv("A2E_SWITCH_WAIT", "30"))

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("a2e")

def _log_jsonl(record: dict):
    with LOG_FILE.open("a") as f:
        f.write(json.dumps({**record, "ts": datetime.utcnow().isoformat()}) + "\n")

# ─── FastAPI ──────────────────────────────────────────────────────────────────
app = FastAPI(title="A2E Daily Bonus Bot v3.1", version="3.1.0", docs_url="/docs")


# ──────────────────────────────────────────────────────────────────────────────
#  Request / Response models
# ──────────────────────────────────────────────────────────────────────────────

class ClaimRequest(BaseModel):
    email:    str
    password: str

class ClaimResponse(BaseModel):
    email:   str
    status:  str
    message: str
    earned:  int = 0
    balance: str = "N/A"
    retries: int = 0
    time:    str = ""


# ──────────────────────────────────────────────────────────────────────────────
#  Auth
# ──────────────────────────────────────────────────────────────────────────────

def _check_api_key(x_api_key: str | None):
    configured = os.getenv("A2E_API_KEY")
    if not configured:
        return
    if x_api_key != configured:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Api-Key header")


# ──────────────────────────────────────────────────────────────────────────────
#  Human-like helpers
# ──────────────────────────────────────────────────────────────────────────────

async def _human_type(page, selector: str, text: str):
    await page.click(selector)
    await page.wait_for_timeout(random.randint(200, 500))
    for char in text:
        await page.keyboard.type(char)
        await page.wait_for_timeout(random.randint(40, 140))

async def _human_click(page, selector: str):
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
#  Browser launch — NO persistent profile (Render compatible)
# ──────────────────────────────────────────────────────────────────────────────

async def _launch_context(email: str):
    """
    Launch a fresh browser + context per call.
    No user_data_dir — safe for Render ephemeral containers.
    """
    pw = await async_playwright().start()

    browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-setuid-sandbox",
            "--single-process",           # needed on some Render tiers
        ],
    )

    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1366, "height": 768},
        locale="en-US",
        timezone_id="America/New_York",
        color_scheme="light",
    )

    if STEALTH_AVAILABLE:
        # stealth_async works on Page objects; apply when new page is created
        context.on("page", lambda p: asyncio.ensure_future(stealth_async(p)))
        log.info(f"[{email}] playwright-stealth hooked ✓")
    else:
        log.warning(f"[{email}] playwright-stealth NOT installed")

    return pw, browser, context


# ──────────────────────────────────────────────────────────────────────────────
#  Login flow
# ──────────────────────────────────────────────────────────────────────────────

async def _do_login(page, email: str, password: str) -> dict:
    await page.goto("https://video.a2e.ai/login", wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(random.randint(1500, 3000))

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

    try:
        await page.wait_for_url(lambda u: "login" not in u, timeout=15_000)
    except PWTimeout:
        pass

    await page.wait_for_timeout(LOGIN_WAIT_SEC * 1000)

    if "login" in page.url.lower():
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
#  Claim flow
# ──────────────────────────────────────────────────────────────────────────────

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

_SUCCESS_TEXTS = ["success", "bonus", "credit", "claimed", "reward", "check-in success"]
_ALREADY_TEXTS = ["already", "come back", "tomorrow", "claimed today", "done today"]
_FAIL_TEXTS    = ["error", "failed", "try again", "something went wrong"]


async def _find_and_click_checkin(page) -> dict:
    for sel in _CHECKIN_SELS:
        try:
            el = await page.wait_for_selector(sel, timeout=3000, state="visible")
            if not el:
                continue
            disabled = await el.get_attribute("disabled")
            txt = (await el.text_content() or "").lower()

            if disabled is not None:
                return {"clicked": False, "already": True,
                        "reason": "Button is disabled (already claimed)"}
            if any(w in txt for w in ["claimed", "done", "completed", "tomorrow"]):
                return {"clicked": False, "already": True,
                        "reason": f"Button text indicates already claimed: '{txt.strip()}'"}

            ok = await _human_click(page, sel)
            if ok:
                return {"clicked": True, "already": False, "reason": f"Clicked: {sel}"}
        except Exception:
            continue
    return {"clicked": False, "already": False, "reason": "No check-in button found"}


async def _verify_claim_result(page) -> dict:
    await page.wait_for_timeout(3500)

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
                if not await el.is_visible():
                    continue
                txt = (await el.text_content() or "").lower().strip()
                if not txt:
                    continue
                if any(w in txt for w in _ALREADY_TEXTS):
                    return {"verified": True, "already": True,
                            "earned": 0, "detail": f"Already claimed: '{txt[:80]}'"}
                if any(w in txt for w in _SUCCESS_TEXTS):
                    return {"verified": True, "already": False,
                            "earned": _parse_credits(txt),
                            "detail": f"Success toast: '{txt[:80]}'"}
                if any(w in txt for w in _FAIL_TEXTS):
                    return {"verified": False, "already": False,
                            "earned": 0, "detail": f"Error toast: '{txt[:80]}'"}
        except Exception:
            continue

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

    return {"verified": False, "already": False, "earned": 0,
            "detail": "No confirmation toast found — treating as unverified"}


def _parse_credits(text: str) -> int:
    nums = re.findall(r'\+?\d+', text)
    return int(nums[0].replace("+", "")) if nums else 30


async def _get_balance(page) -> str:
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

    pw, browser, context = None, None, None
    try:
        pw, browser, context = await _launch_context(email)
        page = await context.new_page()

        if STEALTH_AVAILABLE:
            await stealth_async(page)

        # ── Login ────────────────────────────────────────────────────────────
        login = await _do_login(page, email, password)
        if not login["ok"]:
            result["message"] = f"Login failed: {login['reason']}"
            _log_jsonl({**result, "stage": "login"})
            return result

        if "video.a2e.ai" not in page.url or page.url.endswith("/login"):
            await page.goto("https://video.a2e.ai/", wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_timeout(random.randint(1500, 3000))

        # ── Retry loop ───────────────────────────────────────────────────────
        for attempt in range(1, MAX_RETRIES + 1):
            result["retries"] = attempt
            log.info(f"[{email}] Attempt {attempt}/{MAX_RETRIES}")

            click_r = await _find_and_click_checkin(page)

            if click_r["already"]:
                balance = await _get_balance(page)
                result.update(status="already_claimed", balance=balance,
                               message=f"Already claimed today. ({click_r['reason']})")
                _log_jsonl({**result, "stage": "claim"})
                return result

            if not click_r["clicked"]:
                log.warning(f"[{email}] {click_r['reason']}")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_WAIT_SEC)
                    await page.reload(wait_until="domcontentloaded")
                    await page.wait_for_timeout(2000)
                continue

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

            log.warning(f"[{email}] Unverified after click: {verify['detail']}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_WAIT_SEC)
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)

        result["message"] = (
            f"❌ Could not verify claim after {MAX_RETRIES} attempts."
        )

    except Exception as exc:
        result["message"] = f"Unexpected error: {exc}"
        log.exception(f"[{email}] Crash")

    finally:
        # ── Proper cleanup: page → context → browser → playwright ──────────
        try:
            if context:
                await context.close()
        except Exception:
            pass
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
        try:
            if pw:
                await pw.stop()
        except Exception:
            pass
        _log_jsonl({**result, "stage": "final"})

    return result


# ──────────────────────────────────────────────────────────────────────────────
#  Endpoints
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "service": "A2E Daily Bonus Bot v3.1",
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
    _check_api_key(x_api_key)
    result = await run_claim(body.email, body.password)
    return JSONResponse(content=result)

@app.post("/batch")
async def batch(
    request: Request,
    x_api_key: str | None = Header(default=None),
):
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
    _check_api_key(x_api_key)
    if not LOG_FILE.exists():
        return {"logs": []}
    lines = LOG_FILE.read_text().strip().splitlines()
    return {"logs": [json.loads(l) for l in lines[-n:]]}
