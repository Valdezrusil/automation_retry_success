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


def _human_move(page, target_x, target_y, steps=10):
    """Move mouse to (target_x, target_y) in interpolated steps with jitter."""
    # Get current mouse position (default to center-ish if unknown)
    start = page.evaluate("() => ({x: window.innerWidth/2, y: window.innerHeight/2})")
    sx, sy = start["x"], start["y"]
    for i in range(1, steps + 1):
        t = i / steps
        # Ease-in-out curve
        t = t * t * (3 - 2 * t)
        x = sx + (target_x - sx) * t + random.gauss(0, 2)
        y = sy + (target_y - sy) * t + random.gauss(0, 2)
        page.mouse.move(x, y)
        page.wait_for_timeout(random.randint(10, 30))


def _human_click(page, locator):
    """Move mouse to element with human-like motion, then click at random offset."""
    box = locator.bounding_box()
    if not box:
        locator.click()
        return
    # Random point within the element (not dead center)
    tx = box["x"] + box["width"] * random.uniform(0.25, 0.75)
    ty = box["y"] + box["height"] * random.uniform(0.3, 0.7)
    _human_move(page, tx, ty)
    page.wait_for_timeout(random.randint(100, 300))
    page.mouse.click(tx, ty)


def _dismiss_cookie_banner(page):
    """Reject cookies on MailTMP using the 'Only necessary' button."""
    try:
        btn = page.locator("xpath=//button[@data-role='necessary']")
        if btn.is_visible(timeout=3000):
            btn.click()
            print("    Cookie banner rejected.")
            page.wait_for_timeout(500)
            return True
    except Exception:
        pass
    # Fallback: remove the overlay via JS
    try:
        page.evaluate("""
            (() => {
                for (const el of document.querySelectorAll('.cm-wrapper, [class*="cc--"]')) {
                    el.remove();
                }
            })()
        """)
        print("    Cookie banner removed via JS.")
        return True
    except Exception:
        pass
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

            cdp_url = f"wss://connect.steel.dev?sessionId={steel_session.id}"
            browser = pw.chromium.connect_over_cdp(
                cdp_url,
                headers={"x-api-key": steel_api_key}
            )
            _browser = browser
            context = browser.contexts[0]
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
                    page.goto("https://mailtmp.com/en/", timeout=30000)
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

            # ── 3. Open Webshare register page directly ──────────────
            print("\n[3] Opening Webshare register page...")
            yield {"status": "step", "step_num": 3, "message": "Submitting registration form"}
            ws_page = context.new_page()
            stealth.apply_stealth_sync(ws_page)
            ws_page.on("response", _intercept_proxy_response)
            ws_page.goto("https://dashboard.webshare.io/register", timeout=30000)
            ws_page.wait_for_load_state("domcontentloaded")
            print("    Webshare register page loaded.")

            # ── 4. Fill email (instant) ──────────────────────────────
            print("[4] Entering email...")
            # Try multiple selectors in case the page uses different IDs
            email_input = ws_page.locator(
                "#email-input, input[type='email'], input[name='email'], "
                "input[placeholder*='mail' i], input[placeholder*='Email']"
            ).first
            email_input.wait_for(state="visible", timeout=20000)
            email_input.click()
            email_input.fill(temp_email)
            print(f"    Email entered: {temp_email}")

            ws_page.wait_for_timeout(300)

            # ── 5. Password (instant) ────────────────────────────────
            password = generate_password(12)
            print(f"[5] Entering password: {password}")
            pw_field = ws_page.locator("input[type='password']").first
            pw_field.wait_for(state="visible", timeout=10000)
            pw_field.click()
            pw_field.fill(password)
            print(f"    Password entered.")

            ws_page.wait_for_timeout(300)

            # ── 6. Terms checkbox (it's BELOW the signup button) ─────
            print("[6] Accepting Terms & Conditions...")
            checkbox_el = ws_page.locator("input[type='checkbox']").first
            checkbox_el.scroll_into_view_if_needed()
            ws_page.wait_for_timeout(random.randint(300, 500))
            _human_click(ws_page, checkbox_el)
            ws_page.wait_for_timeout(500)
            is_checked = checkbox_el.is_checked()
            print(f"    Checkbox checked: {is_checked}")
            if not is_checked:
                print("    Retry: focus + Space...")
                checkbox_el.focus()
                ws_page.wait_for_timeout(200)
                ws_page.keyboard.press("Space")
                ws_page.wait_for_timeout(500)
                is_checked = checkbox_el.is_checked()
                print(f"    Checkbox checked: {is_checked}")

            # Pause before clicking signup
            ws_page.wait_for_timeout(random.randint(500, 1000))

            # ── 7. Click Sign Up ────────────────────────────────────
            print("[7] Clicking Sign Up button...")
            signup_el = ws_page.get_by_text("Sign Up With Email", exact=True)
            signup_el.scroll_into_view_if_needed()
            ws_page.wait_for_timeout(300)
            _human_click(ws_page, signup_el)

            print(f"\n=== Sign-up initiated! ===")
            print(f"Email used   : {temp_email}")
            print(f"Password used: {password}")

            # ── 8. Auto-solve reCAPTCHA if it appears ───────────────
            print("\n[8] Checking for reCAPTCHA...")
            yield {"status": "step", "step_num": 4, "message": "Solving reCAPTCHA & verifying"}
            ws_page.wait_for_timeout(3000)
            # Build up human-like mouse history for reCAPTCHA scoring
            print("    Adding mouse activity...")
            vw = ws_page.evaluate("window.innerWidth")
            vh = ws_page.evaluate("window.innerHeight")
            for _ in range(3):
                _human_move(ws_page,
                            random.randint(int(vw * 0.2), int(vw * 0.8)),
                            random.randint(int(vh * 0.2), int(vh * 0.8)))
                ws_page.wait_for_timeout(random.randint(200, 500))
            # Small scroll to mimic reading
            ws_page.mouse.wheel(0, random.randint(-50, 50))
            ws_page.wait_for_timeout(2000)

            MAX_CAPTCHA_ATTEMPTS = 3

            if "/register" not in ws_page.url:
                print("    No CAPTCHA — sign-up went through directly!")
            elif "/register" in ws_page.url:
                # Wait up to 10s for recaptcha anchor iframe to appear
                has_recaptcha = False
                for _ in range(20):
                    for frame in ws_page.frames:
                        if re.search(r"/recaptcha/(api2|enterprise)/anchor", frame.url):
                            has_recaptcha = True
                            break
                    if has_recaptcha or "/register" not in ws_page.url:
                        break
                    ws_page.wait_for_timeout(500)

                if has_recaptcha and "/register" in ws_page.url:
                    print("    reCAPTCHA detected — solving via audio challenge...")
                    # Debug: show what frames are available
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
                elif "/register" not in ws_page.url:
                    print("    Page already redirected — no CAPTCHA needed!")
                else:
                    print("    No reCAPTCHA iframe found — waiting for redirect...")

            # Wait for URL to leave /register (auto-solved or manual)
            print("    Waiting for sign-up redirect...")
            ws_page.wait_for_url(
                lambda url: "/register" not in url, timeout=180000
            )
            print("    Sign-up successful!")

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
            ws_page.goto(verify_url, timeout=30000)
            ws_page.wait_for_timeout(3000)
            print("\n=== Email verified! Account should now be active. ===")

            # ── 12. Navigate to proxy list ──────────────────────────
            print("[12] Navigating to proxy list...")
            yield {"status": "step", "step_num": 5, "message": "Extracting proxies"}
            ws_page.wait_for_url("**/dashboard**", timeout=30000)
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
            ws_page.goto(proxy_url, timeout=30000)
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
    result = run_automation()
    print("\nResult:")
    print(json.dumps(result, indent=2))
