import json
import math
import os
import random
import re
import signal
import string
import time

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from playwright_recaptcha import recaptchav2
from steel import Steel
from dotenv import load_dotenv

# NOTE: Steel hobby plan does NOT support residential proxies or the built-in
# CAPTCHA solver, so we rely purely on (a) playwright-stealth, (b) careful,
# human-like interaction, and (c) a live Google cookie jar to keep the
# reCAPTCHA risk score out of the "silent-block" zone.

# Load .env locally if it exists (Render will ignore this and use its own environment variables)
load_dotenv()

# ── Globals for Ctrl+C cleanup ──────────────────────────────────────
_browser = None
_steel_client = None
_steel_session_id = None


def _handle_sigint(*args):
    print("\nBrowser and script stopped.")
    os._exit(130)


signal.signal(signal.SIGINT, _handle_sigint)


def generate_password(length=12):
    """Random password with upper, lower, digit, and symbol."""
    chars = string.ascii_letters + string.digits + "!@#$%&*"
    pw = [
        random.choice(string.ascii_uppercase),
        random.choice(string.ascii_lowercase),
        random.choice(string.digits),
        random.choice("!@#$%&*"),
    ]
    pw += [random.choice(chars) for _ in range(length - 4)]
    random.shuffle(pw)
    return "".join(pw)


# ── Proxy-list API response interceptor ─────────────────────────────
_captured_proxy_data = None


def _intercept_proxy_response(response):
    """Playwright response listener — grabs the proxy list JSON."""
    global _captured_proxy_data
    url = response.url
    if ("proxy/list" in url or "proxy_list" in url) and response.ok:
        try:
            data = response.json()
            if data.get("results"):
                _captured_proxy_data = data
        except Exception:
            pass


# Track the "last known" cursor position ourselves — Playwright does not expose it.
_LAST_POS = {"x": None, "y": None}


def _human_move(page, target_x, target_y, steps=None):
    """Move the cursor to (target_x, target_y) along a quadratic Bezier curve,
    with small gaussian jitter and variable per-step delays. This looks much
    more like a human than a straight interpolated line.
    """
    sx, sy = _LAST_POS["x"], _LAST_POS["y"]
    if sx is None or sy is None:
        # Pick a random-ish starting point the first time so trajectories
        # do not always begin at (0,0).
        vw = page.evaluate("window.innerWidth")
        vh = page.evaluate("window.innerHeight")
        sx = random.uniform(vw * 0.3, vw * 0.7)
        sy = random.uniform(vh * 0.3, vh * 0.7)

    dist = math.hypot(target_x - sx, target_y - sy)
    if steps is None:
        # More steps for longer distances — roughly 1 step per 12–18px.
        steps = max(10, int(dist / random.uniform(12, 18)))

    # Pick a control point offset perpendicular to the direct line so the
    # curve arcs naturally rather than going straight.
    mx = (sx + target_x) / 2 + random.uniform(-dist * 0.15, dist * 0.15)
    my = (sy + target_y) / 2 + random.uniform(-dist * 0.15, dist * 0.15)

    for i in range(1, steps + 1):
        t = i / steps
        # Quadratic bezier
        x = (1 - t) ** 2 * sx + 2 * (1 - t) * t * mx + t ** 2 * target_x
        y = (1 - t) ** 2 * sy + 2 * (1 - t) * t * my + t ** 2 * target_y
        # Tiny jitter
        x += random.gauss(0, 0.7)
        y += random.gauss(0, 0.7)
        page.mouse.move(x, y)
        # Humans slow down near the target.
        if t > 0.8:
            page.wait_for_timeout(random.randint(14, 32))
        else:
            page.wait_for_timeout(random.randint(6, 18))

    _LAST_POS["x"] = target_x
    _LAST_POS["y"] = target_y


def _human_click(page, locator, settle_ms=None):
    """Move to the element with a curved path, pause briefly, then press/release
    the mouse with a realistic (60–140ms) hold time. Uses `mouse.down()` + `mouse.up()`
    rather than `mouse.click()` so the dwell time is always present.
    """
    box = locator.bounding_box()
    if not box:
        locator.click()
        return
    # Random point within the element — avoid dead center.
    tx = box["x"] + box["width"] * random.uniform(0.25, 0.75)
    ty = box["y"] + box["height"] * random.uniform(0.3, 0.7)
    _human_move(page, tx, ty)
    page.wait_for_timeout(settle_ms if settle_ms is not None else random.randint(120, 280))
    page.mouse.down()
    page.wait_for_timeout(random.randint(60, 140))
    page.mouse.up()


def _human_type(page, text, base_delay=(55, 110)):
    """Type text one key at a time with PER-CHARACTER variable delays and
    occasional longer pauses (like a human who briefly looked away).
    `page.type(text, delay=N)` uses a FIXED delay for every keystroke, which
    is easy to fingerprint as a bot.
    """
    for i, ch in enumerate(text):
        page.keyboard.type(ch)
        # Most keys: quick variable delay.
        d = random.randint(*base_delay)
        # ~8% of the time, a longer "thinking" pause.
        if random.random() < 0.08:
            d += random.randint(180, 420)
        page.wait_for_timeout(d)


def _has_recaptcha_challenge(page):
    """Return True only when the reCAPTCHA CHALLENGE (bframe) popup is visible
    — not just the always-present anchor iframe. The anchor is always loaded
    whenever reCAPTCHA is on the page, so matching it tells us nothing.
    The bframe only appears when Google decides we need to solve a challenge.
    """
    # 1) Look for a bframe frame (URL contains /bframe).
    for frame in page.frames:
        if re.search(r"/recaptcha/(api2|enterprise)/bframe", frame.url):
            return True
    # 2) Fallback: the challenge iframe element is usually <iframe title="recaptcha challenge ..."
    #    and its parent <div> has style set to a non-zero opacity/visibility.
    try:
        visible = page.evaluate(
            """() => {
                const iframes = document.querySelectorAll('iframe[src*="/recaptcha/"][src*="bframe"], iframe[title*="recaptcha challenge"]');
                for (const f of iframes) {
                    const r = f.getBoundingClientRect();
                    const style = window.getComputedStyle(f.parentElement || f);
                    if (r.width > 0 && r.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none'
                        && parseFloat(style.opacity || '1') > 0.1) {
                        return true;
                    }
                }
                return false;
            }"""
        )
        if visible:
            return True
    except Exception:
        pass
    return False


def _dismiss_cookie_banner(page):
    """Reject cookies on MailTMP using the 'Only necessary' button.
    Only clicks the consent button — does NOT remove DOM elements."""
    try:
        btn = page.locator("xpath=//button[@data-role='necessary']")
        if btn.is_visible(timeout=3000):
            btn.click()
            print("    Cookie banner rejected.")
            page.wait_for_timeout(500)
            return True
    except Exception:
        pass
    # Fallback: try other common consent button selectors
    try:
        for selector in [
            "button[data-role='necessary']",
            "button.cm-btn-accept",
            "button#cm-acceptNone",
            "a.cc-btn.cc-dismiss",
        ]:
            btn = page.locator(selector)
            if btn.is_visible(timeout=1000):
                btn.click()
                print(f"    Cookie banner dismissed via: {selector}")
                page.wait_for_timeout(500)
                return True
    except Exception:
        pass
    print("    No cookie banner found (OK).")
    return False


def run_automation():
    global _browser, _steel_client, _steel_session_id, _captured_proxy_data

    steel_api_key = os.environ.get("STEEL_API_KEY", "").strip()
    if not steel_api_key:
        print("[ERROR] STEEL_API_KEY not found in environment — cannot proceed.")
        yield {"status": "error", "message": "STEEL_API_KEY missing in environment"}
        return

    steel_client = None
    steel_session = None

    stealth = Stealth()

    with stealth.use_sync(sync_playwright()) as pw:
        # ── 0. Launch Steel cloud browser ────────────────────────────
        try:
            print("[0] Starting Steel cloud browser session...")
            yield {"status": "step", "step_num": 1, "message": "Initializing Cloud Browser"}

            steel_client = Steel(steel_api_key=steel_api_key)
            steel_session = steel_client.sessions.create(
                timeout=900000,  # 15 minutes (hobby plan max)
            )
            _steel_client = steel_client
            _steel_session_id = str(steel_session.id)
            print(f"    Session ID : {steel_session.id}")
            debug_url = getattr(steel_session, 'debug_url', None)
            if debug_url:
                live_url = f"{debug_url}?apiKey={steel_api_key}"
            else:
                live_url = "N/A"
            print(f"    Live view  : {live_url}")
            print(f"    Dashboard  : {getattr(steel_session, 'session_viewer_url', 'N/A')}")
            
            if live_url != "N/A":
                yield {"status": "info", "live_url": live_url}

            cdp_url = f"wss://connect.steel.dev?apiKey={steel_api_key}&sessionId={steel_session.id}"
            browser = pw.chromium.connect_over_cdp(cdp_url)
            _browser = browser
            # We are using the default isolated session provided by Steel
            # It provides a clean IP and fingerprint natively.
            context = browser.contexts[0]
            context.clear_cookies()
            page = context.new_page()
            stealth.apply_stealth_sync(page)
            print("    Connected to Steel cloud browser (stealth ON)!")

        except Exception as e:
            print(f"[ERROR] Steel cloud browser failed: {e}")
            yield {"status": "error", "message": f"Browser init failed: {str(e)}"}
            return

        # Attach the proxy-list response listener early
        page.on("response", _intercept_proxy_response)

        try:
            # ── 1. Open MailTMP ──────────────────────────────────────
            print("\n[1] Opening MailTMP...")
            yield {"status": "step", "step_num": 2, "message": "Getting temporary email"}
            email_pattern = re.compile(
                r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}$"
            )
            temp_email = ""

            for page_attempt in range(5):
                try:
                    page.goto("https://mailtmp.com/en/", timeout=40000)
                    page.wait_for_load_state("domcontentloaded")
                    page.wait_for_timeout(3000)
                except Exception as e:
                    print(f"    MailTMP load failed: {e}")
                    if page_attempt < 4:
                        print(f"    Retrying page load... ({page_attempt + 1}/5)")
                        page.wait_for_timeout(3000)
                        continue
                    else:
                        print("    ERROR: MailTMP is down!")
                        yield {"status": "error", "message": "MailTMP is down"}
                        return

                # Check for 502 / error page
                title = page.title().lower()
                if "bad gateway" in title or "502" in title or "error" in title:
                    print(f"    MailTMP returned error page, retrying... ({page_attempt + 1}/5)")
                    page.wait_for_timeout(5000)
                    continue

                # Dismiss cookie consent banner
                print("    Checking for cookie banner...")
                _dismiss_cookie_banner(page)
                page.wait_for_timeout(1000)

                # ── 2. Read temporary email ───────────────────────
                print("[2] Reading temporary email...")
                for email_attempt in range(6):
                    # Method A: input value
                    result = page.evaluate("""
                        (() => {
                            for (const inp of document.querySelectorAll('input')) {
                                if (inp.value && inp.value.includes('@')) return inp.value;
                            }
                            return '';
                        })()
                    """)
                    if result and email_pattern.match(result.strip()):
                        temp_email = result.strip()
                        break

                    # Method B: visible text scan
                    result = page.evaluate("""
                        (() => {
                            for (const el of document.querySelectorAll('*')) {
                                const t = el.textContent.trim();
                                if (t && t.includes('@') && !t.includes(' ') && t.includes('.')
                                    && /^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\\.[a-zA-Z]{2,}$/.test(t))
                                    return t;
                            }
                            return '';
                        })()
                    """)
                    if result and email_pattern.match(result.strip()):
                        temp_email = result.strip()
                        break

                    # Method C: click copy button
                    try:
                        page.locator("i.fa.fa-copy").click(timeout=3000)
                        page.wait_for_timeout(500)
                        result = page.evaluate("""
                            (() => {
                                const inp = document.createElement('input');
                                inp.id = '__paste_helper';
                                document.body.appendChild(inp);
                                inp.focus();
                                document.execCommand('paste');
                                return inp.value;
                            })()
                        """)
                        if result and email_pattern.match(result.strip()):
                            temp_email = result.strip()
                            break
                    except Exception:
                        pass

                    print(f"    Waiting for email to load... ({email_attempt + 1}/6)")
                    page.wait_for_timeout(2000)

                if temp_email:
                    break
                else:
                    print(f"    Email not found, refreshing page... ({page_attempt + 1}/5)")
                    page.wait_for_timeout(2000)

            if not temp_email:
                print("    ERROR: Could not read a valid email from MailTMP!")
                yield {"status": "error", "message": "Could not extract temp email"}
                return

            print(f"    Temp email: {temp_email}")
            mailtmp_page = page  # keep reference

            # ── 3. Initialize Webshare page ──────────────────────────
            ws_page = context.new_page()
            stealth.apply_stealth_sync(ws_page)
            ws_page.on("response", _intercept_proxy_response)

            # ── 3a. Google warm-up ─────────────────────────────────
            # reCAPTCHA risk-scores a session largely based on Google cookies
            # (NID, 1P_JAR, CONSENT, SOCS). A brand-new Steel session on a
            # datacenter IP with ZERO Google history is a near-guaranteed
            # "silent block" — Google hands back a low-score token and Webshare
            # bounces you to /register? without ever showing a challenge.
            # So: visit google.com first, accept consent, and let the cookies
            # settle before we ever touch webshare.io.
            print("    Warming up Google session (seeding trust cookies)...")
            try:
                ws_page.goto("https://www.google.com/", timeout=30000)
                ws_page.wait_for_load_state("domcontentloaded")
                ws_page.wait_for_timeout(random.randint(1500, 2800))

                # Accept the Google consent banner if it appears (EU datacenter IPs
                # will usually show it). We prefer the "Accept all" path so the
                # cookies written look like a real consenting user.
                for accept_sel in [
                    "button:has-text('Accept all')",
                    "button:has-text('I agree')",
                    "button#L2AGLb",
                    "form[action*='consent'] button",
                ]:
                    try:
                        btn = ws_page.locator(accept_sel).first
                        if btn.is_visible(timeout=1500):
                            _human_click(ws_page, btn)
                            ws_page.wait_for_timeout(random.randint(800, 1500))
                            print(f"    Google consent accepted via: {accept_sel}")
                            break
                    except Exception:
                        pass

                # A little organic scrolling on the Google homepage itself.
                ws_page.mouse.wheel(0, random.randint(80, 220))
                ws_page.wait_for_timeout(random.randint(400, 900))
                ws_page.mouse.wheel(0, -random.randint(40, 120))
                ws_page.wait_for_timeout(random.randint(600, 1200))
            except Exception as warm_err:
                print(f"    [Warning] Google warm-up failed (non-fatal): {warm_err}")

            # ── 3b. Navigate to Webshare & gently clear ONLY its own state ──
            # IMPORTANT: Do NOT wipe google.com / gstatic.com here — those
            # cookies are our trust score. We only clear Webshare's own origin.
            ws_page.goto("https://webshare.io", timeout=60000)
            ws_page.wait_for_load_state("domcontentloaded")
            try:
                cdp = context.new_cdp_session(ws_page)
                cdp.send('Storage.clearDataForOrigin', {
                    'origin': 'https://webshare.io',
                    'storageTypes': 'cookies,local_storage,session_storage,indexeddb,service_workers,cache_storage',
                })
                print("    CDP Webshare-only origin wipe completed.")
            except Exception as e:
                print(f"    [Warning] CDP Webshare wipe failed: {e}")

            # Organic warm-up on the Webshare homepage: a couple of lazy mouse
            # arcs and a scroll, like a human deciding whether to sign up.
            print("    Organic warm-up on webshare.io...")
            vw = ws_page.evaluate("window.innerWidth")
            vh = ws_page.evaluate("window.innerHeight")
            for _ in range(random.randint(3, 5)):
                _human_move(ws_page,
                            random.randint(int(vw * 0.2), int(vw * 0.8)),
                            random.randint(int(vh * 0.2), int(vh * 0.8)))
                ws_page.wait_for_timeout(random.randint(300, 700))
            ws_page.mouse.wheel(0, random.randint(150, 400))
            ws_page.wait_for_timeout(random.randint(600, 1100))
            ws_page.mouse.wheel(0, -random.randint(80, 200))
            ws_page.wait_for_timeout(random.randint(400, 900))

            for registration_attempt in range(3):
                print(f"\n[3] Opening Webshare homepage (attempt {registration_attempt+1})...")
                yield {"status": "step", "step_num": 3, "message": f"Sign-up Attempt {registration_attempt+1}/3"}
                
                ws_page.goto("https://webshare.io", timeout=60000)
                ws_page.wait_for_load_state("domcontentloaded")
                
                # Handle possible overlays
                ws_page.evaluate("() => document.querySelectorAll('.cm-wrapper, .cookie-banner, #onetrust-banner-sdk').forEach(el => el.remove())")

                # Organic click logic — find the VISIBLE Sign Up link only
                print("    Detecting 'Sign Up' button...")
                # Use :visible to skip the hidden mobile duplicate
                signup_nav = ws_page.locator("a.nav-register_button:visible, a:has-text('Sign Up'):visible").first
                
                try:
                    signup_nav.wait_for(state="visible", timeout=10000)
                    _human_click(ws_page, signup_nav)
                except Exception as e:
                    print(f"    Visible click failed ({type(e).__name__}). Trying JS click...")
                    # Use JS to click the exact desktop button as last resort
                    ws_page.evaluate("""
                        () => {
                            const btn = document.querySelector('a.nav-register_button');
                            if (btn) { btn.click(); return; }
                            const links = [...document.querySelectorAll('a')];
                            const signup = links.find(a => a.textContent.trim() === 'Sign Up' && a.offsetParent !== null);
                            if (signup) signup.click();
                        }
                    """)
                
                # Wait for the registration page to load — don't force any URL
                # Whatever page the button takes us to, just wait for the email input
                ws_page.wait_for_load_state("domcontentloaded")
                ws_page.wait_for_timeout(3000)
                print(f"    Landed on: {ws_page.url}")
                
                # ── 4. Type email (human-like) ──────────────────────────────
                print("[4] Typing email...")
                email_input = ws_page.locator("#email-input").first
                email_input.wait_for(state="visible", timeout=20000)
                _human_click(ws_page, email_input)
                # Clear any existing value first (selection + delete, like a human)
                ws_page.keyboard.press("Control+a")
                ws_page.wait_for_timeout(random.randint(80, 180))
                ws_page.keyboard.press("Backspace")
                ws_page.wait_for_timeout(random.randint(150, 320))
                # Type character by character with variable per-key delays
                _human_type(ws_page, temp_email, base_delay=(50, 110))
                print(f"    Email typed: {temp_email}")

                # Tab from email → password instead of clicking (real humans do this).
                ws_page.wait_for_timeout(random.randint(320, 700))
                ws_page.keyboard.press("Tab")
                ws_page.wait_for_timeout(random.randint(200, 450))

                # ── 5. Type password (human-like) ─────────────────────────
                password = generate_password(12)
                print(f"[5] Typing password: {password}")
                # If Tab didn't land us on the password field (e.g. CAPTCHA widget was focused),
                # fall back to an explicit click.
                pw_field = ws_page.locator("input[type='password']").first
                try:
                    focused_is_pw = ws_page.evaluate(
                        "() => document.activeElement && document.activeElement.type === 'password'"
                    )
                except Exception:
                    focused_is_pw = False
                if not focused_is_pw:
                    pw_field.wait_for(state="visible", timeout=10000)
                    _human_click(ws_page, pw_field)
                _human_type(ws_page, password, base_delay=(55, 115))
                print(f"    Password typed.")

                ws_page.wait_for_timeout(random.randint(400, 800))

                # ── 6. Terms checkbox ───────────────────────────────
                print("[6] Accepting Terms & Conditions...")
                checkbox_el = ws_page.locator("input[type='checkbox']").first
                checkbox_el.scroll_into_view_if_needed()
                ws_page.wait_for_timeout(random.randint(250, 500))

                # Prefer clicking the associated <label> with a real mouse event
                # — force=True bypasses actionability checks and looks synthetic.
                # Webshare's checkbox has a label whose `for` matches the input id,
                # OR the input is wrapped in a label. Try the label first.
                clicked_naturally = False
                try:
                    cb_id = checkbox_el.get_attribute("id") or ""
                    label_loc = None
                    if cb_id:
                        label_loc = ws_page.locator(f"label[for='{cb_id}']").first
                    if label_loc is None or not label_loc.is_visible(timeout=500):
                        label_loc = ws_page.locator("label:has(input[type='checkbox'])").first
                    if label_loc.is_visible(timeout=1500):
                        _human_click(ws_page, label_loc)
                        clicked_naturally = True
                except Exception:
                    pass

                if not clicked_naturally:
                    # Fall back to clicking the input directly (still a real mouse event).
                    try:
                        _human_click(ws_page, checkbox_el)
                    except Exception:
                        # Last resort: focus + space. Still a real keyboard event.
                        checkbox_el.focus()
                        ws_page.wait_for_timeout(random.randint(120, 260))
                        ws_page.keyboard.press("Space")

                ws_page.wait_for_timeout(random.randint(350, 700))
                is_checked = checkbox_el.is_checked()
                print(f"    Checkbox checked: {is_checked}")
                if not is_checked:
                    print("    Retry: focus + Space...")
                    checkbox_el.focus()
                    ws_page.wait_for_timeout(random.randint(120, 260))
                    ws_page.keyboard.press("Space")
                    ws_page.wait_for_timeout(random.randint(300, 600))
                    is_checked = checkbox_el.is_checked()
                    print(f"    Checkbox checked: {is_checked}")

                # Natural "read the page" pause before clicking Sign Up.
                ws_page.wait_for_timeout(random.randint(900, 1800))

                print(f"\n=== Sign-up details ===")
                print(f"Email used   : {temp_email}")
                print(f"Password used: {password}")

                # ── 7. Click Sign Up (ONCE) + long wait for captcha/redirect ──
                # We used to click up to 5 times if nothing happened, but every
                # extra click increases the bot score, making the NEXT click even
                # more likely to silent-fail. Click ONCE, wait generously, and
                # only retry the entire flow (fresh page) if nothing happened.
                yield {"status": "step", "step_num": 4, "message": "Solving reCAPTCHA & verifying"}
                signup_el = ws_page.locator("button[type='submit'], button:has-text('Sign Up With Email')").first
                signup_el.scroll_into_view_if_needed()
                ws_page.wait_for_timeout(random.randint(250, 500))

                MAX_CAPTCHA_ATTEMPTS = 3
                has_recaptcha = False

                print("\n[7] Clicking 'Sign Up' with human-like motion (single click)...")
                _human_click(ws_page, signup_el)

                # Up to 30s for something to happen: URL change, bframe, or
                # an inline error message. NO extra mouse jitter here — real
                # users wait for the form to respond.
                print("    Waiting up to 30s for captcha challenge or redirect...")
                outcome = None   # "redirect" | "challenge" | "error" | None
                for tick in range(30):
                    # Signup went through (URL left /register)?
                    if "/register" not in ws_page.url:
                        outcome = "redirect"
                        break
                    # reCAPTCHA challenge popup appeared?
                    if _has_recaptcha_challenge(ws_page):
                        has_recaptcha = True
                        outcome = "challenge"
                        break
                    # Any visible inline error?
                    try:
                        err = ws_page.evaluate(
                            """() => {
                                const nodes = document.querySelectorAll('[class*="error" i], [role="alert"]');
                                for (const n of nodes) {
                                    const t = (n.innerText || '').trim();
                                    const r = n.getBoundingClientRect();
                                    if (t && r.width > 0 && r.height > 0) return t.slice(0, 200);
                                }
                                return '';
                            }"""
                        )
                    except Exception:
                        err = ""
                    if err:
                        print(f"    Inline error detected: {err!r}")
                        outcome = "error"
                        break
                    ws_page.wait_for_timeout(1000)

                if outcome == "redirect":
                    print("    ✓ Sign-up went through without a visible challenge!")
                elif outcome == "challenge":
                    print("    ✓ reCAPTCHA challenge appeared!")
                elif outcome == "error":
                    print("    [WARN] Form returned an inline error — will retry the whole flow.")
                else:
                    print("    [WARN] Neither redirect nor challenge after 30s — likely a silent low-score block.")

                # ── 8. Solve captcha if it appeared ──────────────────────
                if outcome == "challenge" and "/register" in ws_page.url:
                    print("\n[8] reCAPTCHA detected — solving via audio challenge...")
                    for f in ws_page.frames:
                        if "recaptcha" in f.url:
                            print(f"      frame: {f.name} -> {f.url[:80]}")
                    for attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
                        print(f"    Attempt {attempt}/{MAX_CAPTCHA_ATTEMPTS}...")
                        try:
                            with recaptchav2.SyncSolver(ws_page) as solver:
                                token = solver.solve_recaptcha(
                                    wait=True, wait_timeout=90
                                )
                                print(f"    CAPTCHA SOLVED! Token length: {len(token)}")
                                break
                        except Exception as e:
                            print(f"    Attempt {attempt} failed: {type(e).__name__}: {e}")
                            if attempt < MAX_CAPTCHA_ATTEMPTS:
                                print("    Retrying after short delay...")
                                ws_page.wait_for_timeout(3000)
                            else:
                                print("    All auto-solve attempts exhausted.")
                                print("    >>> Please solve the CAPTCHA manually in the live viewer <<<")

                    # Wait for URL to leave /register after solving (up to 60s).
                    print("    Waiting for sign-up redirect post-solve...")
                    for _ in range(60):
                        if "/register" not in ws_page.url or ws_page.url.endswith("register?"):
                            break
                        ws_page.wait_for_timeout(1000)

                elif outcome == "redirect":
                    print("    No CAPTCHA needed — sign-up went through!")
                elif outcome == "error":
                    print("    Inline form error — retrying the whole flow.")
                else:
                    # Neither redirect nor challenge after 30s — the invisible
                    # captcha almost certainly returned a low-score token that
                    # Webshare's server silently rejected. Retry the whole flow
                    # rather than waste 3 more minutes waiting for a redirect
                    # that will never come.
                    print("    No redirect and no challenge — will retry the whole flow.")

                print("    Sign-up step finished!")

                # Decide whether to retry the whole registration flow.
                # - Successful: URL is outside /register (dashboard/activation pending etc).
                # - Silent fail: URL ends in /register? OR still on /register with no challenge.
                on_register = "/register" in ws_page.url
                silent_fail = on_register and outcome in (None, "error")

                if silent_fail or ws_page.url.endswith("/register?"):
                    print(f"    [WARNING] Silent bounce back to /register (attempt {registration_attempt+1}). Retrying...")
                    yield {"status": "info", "message": "Applying stealth form retry..."}
                    # Give the session a short cool-down + some mouse/scroll activity
                    # before the next attempt, so Google can see continued human-like
                    # behaviour before we try again.
                    try:
                        ws_page.mouse.wheel(0, random.randint(100, 300))
                        ws_page.wait_for_timeout(random.randint(1500, 3000))
                        ws_page.mouse.wheel(0, -random.randint(60, 180))
                    except Exception:
                        pass
                    ws_page.wait_for_timeout(random.randint(2500, 4500))
                    continue
                else:
                    break
            # ── 9. Switch to MailTMP, get verification email ────────
            print("\n[9] Switching to MailTMP for verification email...")
            mailtmp_page.bring_to_front()
            mailtmp_page.wait_for_timeout(2000)

            # ── 10. Wait for and click the Webshare email ───────────
            print("[10] Waiting for Webshare verification email...")
            email_locator = mailtmp_page.locator(
                "text=/Activate Your Webshare Account|Webshare Support|webshare/i"
            ).first
            email_found = False

            for mail_attempt in range(12):  # up to ~2 minutes of retries
                # Click the internal Refresh button (do NOT reload the page!)
                try:
                    refresh_btn = mailtmp_page.locator(
                        "xpath=//small[normalize-space()='Refresh']"
                    )
                    if refresh_btn.is_visible(timeout=2000):
                        refresh_btn.click()
                        mailtmp_page.wait_for_timeout(3000)
                except Exception:
                    # Fallback: try by button ID
                    try:
                        mailtmp_page.locator("#actualizarEmail").click(timeout=2000)
                        mailtmp_page.wait_for_timeout(3000)
                    except Exception:
                        pass

                # Check if the email appeared
                try:
                    email_locator.wait_for(state="visible", timeout=5000)
                    email_locator.click(timeout=5000)
                    email_found = True
                    print(f"    Verification email found and clicked! (attempt {mail_attempt + 1})")
                    break
                except Exception:
                    print(f"    Email not yet arrived... refreshing ({mail_attempt + 1}/12)")
                    mailtmp_page.wait_for_timeout(5000)

            if not email_found:
                print("    ERROR: Verification email never arrived!")
                yield {"status": "error", "message": "Verification email never arrived"}
                return

            mailtmp_page.wait_for_timeout(3000)

            # ── 11. Find the verification link ──────────────────────
            print("[11] Looking for verification link...")
            verify_url = None

            # Method 1: search iframes
            for frame in mailtmp_page.frames:
                if frame == mailtmp_page.main_frame:
                    continue
                for link in frame.locator("a").all():
                    href = link.get_attribute("href") or ""
                    if "activation" in href or "verify" in href.lower():
                        verify_url = href
                        break
                if verify_url:
                    break

            # Method 2: main page links
            if not verify_url:
                for link in mailtmp_page.locator("a").all():
                    href = link.get_attribute("href") or ""
                    if "activation" in href or "verify" in href.lower():
                        verify_url = href
                        break

            # Method 3: regex scan page source
            if not verify_url:
                source = mailtmp_page.content()
                match = re.search(
                    r"https://dashboard\.webshare\.io/activation/[A-Za-z0-9/]+",
                    source,
                )
                if match:
                    verify_url = match.group(0)

            if not verify_url:
                print("    ERROR: Could not find verification link!")
                yield {"status": "error", "message": "Could not find verification link"}
                return

            print(f"    Opening: {verify_url}")
            ws_page.bring_to_front()
            ws_page.goto(verify_url, timeout=40000)
            ws_page.wait_for_timeout(3000)
            print("\n=== Email verified! Account should now be active. ===")

            # ── 12. Navigate to proxy list ──────────────────────────
            print("[12] Navigating to proxy list...")
            yield {"status": "step", "step_num": 5, "message": "Extracting proxies"}
            ws_page.wait_for_url("**/dashboard**", timeout=40000)
            ws_page.wait_for_timeout(3000)

            # Find the proxy list URL (may be relative like /12345/proxy/list)
            proxy_url = None
            source = ws_page.content()
            # Try absolute URL in page source
            proxy_match = re.search(
                r"https://dashboard\.webshare\.io/\d+/proxy/list", source
            )
            if proxy_match:
                proxy_url = proxy_match.group(0)
            else:
                # Try href attributes (often relative)
                for link in ws_page.locator("a").all():
                    href = link.get_attribute("href") or ""
                    if "/proxy/list" in href:
                        proxy_url = href
                        break
                # Also try extracting account ID from current URL
                if not proxy_url:
                    id_match = re.search(r"/(\d{5,})/", ws_page.url)
                    if id_match:
                        proxy_url = f"/{id_match.group(1)}/proxy/list"
            if not proxy_url:
                proxy_url = "/proxy/list"

            # Ensure absolute URL
            if proxy_url.startswith("/"):
                proxy_url = f"https://dashboard.webshare.io{proxy_url}"

            print(f"    Proxy page: {proxy_url}")
            _captured_proxy_data = None  # reset before navigation
            ws_page.goto(proxy_url, timeout=40000)
            ws_page.wait_for_load_state("domcontentloaded")
            ws_page.wait_for_timeout(5000)

            # ── 13. Extract proxies ─────────────────────────────────
            print("[13] Extracting proxies...")
            proxies = []

            # Method 1: intercepted API response
            if _captured_proxy_data and _captured_proxy_data.get("results"):
                for p in _captured_proxy_data["results"]:
                    ip = p.get("proxy_address", "")
                    port = str(p.get("port", ""))
                    user = p.get("username", "")
                    pw = p.get("password", "")
                    if ip and port:
                        proxies.append(f"{ip}:{port}:{user}:{pw}")
                print(f"    Found {len(proxies)} proxies from API interception.")

            # Method 2: direct fetch from page context
            if not proxies:
                print("    Trying direct API call from page context...")
                api_data = ws_page.evaluate("""
                    async () => {
                        try {
                            const r = await fetch(
                                'https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page=1&page_size=10',
                                { credentials: 'include' }
                            );
                            return await r.json();
                        } catch { return null; }
                    }
                """)
                if api_data and isinstance(api_data, dict) and api_data.get("results"):
                    for p in api_data["results"]:
                        ip = p.get("proxy_address", "")
                        port = str(p.get("port", ""))
                        user = p.get("username", "")
                        pw = p.get("password", "")
                        if ip and port:
                            proxies.append(f"{ip}:{port}:{user}:{pw}")
                    print(f"    Found {len(proxies)} proxies from direct API call.")

            # Method 3: DOM table scrape
            if not proxies:
                print("    Trying table extraction...")
                for attempt in range(5):
                    ws_page.wait_for_timeout(2000)
                    js_proxies = ws_page.evaluate("""
                        (() => {
                            const results = [];
                            const rows = document.querySelectorAll(
                                'table tbody tr, [role="row"]'
                            );
                            for (const row of rows) {
                                const text = row.innerText || '';
                                const m = text.match(
                                    /(\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3})\\s+(\\d{2,5})/
                                );
                                if (!m) continue;
                                const ip = m[1], port = m[2];
                                const cells = row.querySelectorAll(
                                    'td, [role="cell"]'
                                );
                                const parts = [];
                                for (const c of cells) {
                                    const t = c.innerText.trim();
                                    if (t && t !== ip && t !== port && t.length < 60)
                                        parts.push(t);
                                }
                                results.push(
                                    ip + ':' + port + ':' +
                                    (parts[0]||'') + ':' + (parts[1]||'')
                                );
                            }
                            return results;
                        })()
                    """)
                    if js_proxies:
                        proxies = js_proxies
                        print(f"    Found {len(proxies)} proxies from table.")
                        break
                    print(f"    Table attempt {attempt + 1}/5...")

            # ── Print results ───────────────────────────────────────
            print("\n" + "=" * 50)
            print("    EXTRACTED PROXIES")
            print("=" * 50)
            if proxies:
                for proxy in proxies:
                    print(proxy)
                    print()
                print(f"Total: {len(proxies)} proxies")
                yield {"status": "success", "proxies": proxies}
            else:
                print("    No proxies found! Check the browser manually.")
                yield {"status": "error", "message": "No proxies found! Captcha blocked it?"}
            print("=" * 50)
            return

        except Exception as e:
            print(f"\n[ERROR] {e}")
            import traceback
            traceback.print_exc()
            yield {"status": "error", "message": f"Script failed: {str(e)}"}
            return

        finally:
            try:
                browser.close()
            except Exception:
                pass
            if steel_client and steel_session:
                try:
                    steel_client.sessions.release(steel_session.id)
                    print("    Steel session released.")
                except Exception:
                    pass


if __name__ == "__main__":
    for partial_result in run_automation():
        print("\nYielded:")
        print(json.dumps(partial_result, indent=2))
