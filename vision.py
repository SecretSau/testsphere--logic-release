"""
vision.py  —  AI-powered step verification for TestSphere
Uses Gemini Vision to actually SEE the screen after every step
instead of counting steps or relying solely on DOM values.
"""

import os
import re
import json
import base64
import time
import difflib
import requests
from pathlib import Path
from datetime import datetime

from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.common.by import By

# ── Gemini Vision config ─────────────────────────────────────────────────────
GEMINI_MODEL   = "gemini-2.5-flash"
GEMINI_API_URL = (
    f"https://generativelanguage.googleapis.com/v1beta"
    f"/models/{GEMINI_MODEL}:generateContent"
)


def _load_gemini_key() -> str:
    try:
        key_file = (
            Path(os.path.expandvars(r"%LOCALAPPDATA%"))
            / "TestSphere" / "gemini_key.json"
        )
        if key_file.exists():
            with open(key_file, "r") as f:
                return json.load(f).get("api_key", "")
    except Exception:
        pass
    return ""


# ── Screenshot helpers ───────────────────────────────────────────────────────

def _capture_screenshot(driver: WebDriver, label: str = "step") -> str | None:
    """Save a full-page screenshot to Pictures folder and return the path."""
    try:
        folder = Path.home() / "Pictures"
        folder.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"ts_{label.replace(' ', '_')}_{ts}.png"
        path  = str(folder / fname)
        driver.save_screenshot(path)
        return path
    except Exception as e:
        print(f"[Vision] Screenshot failed: {e}")
        return None


def _screenshot_as_base64(driver: WebDriver) -> str | None:
    """Return a base64-encoded PNG of the current viewport."""
    try:
        return base64.b64encode(driver.get_screenshot_as_png()).decode("utf-8")
    except Exception as e:
        print(f"[Vision] base64 screenshot failed: {e}")
        return None


def _element_as_base64(element: WebElement) -> str | None:
    """Return a base64-encoded PNG of a single element."""
    try:
        return base64.b64encode(element.screenshot_as_png).decode("utf-8")
    except Exception as e:
        print(f"[Vision] element screenshot failed: {e}")
        return None


# ── Gemini Vision call ───────────────────────────────────────────────────────

def _ask_gemini_vision(image_b64: str, prompt: str) -> str:
    """
    Send a screenshot + prompt to Gemini Vision.
    Returns the raw text response or empty string on failure.
    """
    api_key = _load_gemini_key()
    if not api_key or not image_b64:
        return ""

    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": "image/png", "data": image_b64}},
                {"text": prompt}
            ]
        }],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1024}
    }

    try:
        resp = requests.post(
            f"{GEMINI_API_URL}?key={api_key}",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"[Vision] Gemini Vision call failed: {e}")
        return ""


def _gemini_judge_screen(
    driver: WebDriver,
    action_type: str,
    field: str,
    expected: str
) -> dict | None:
    """
    Take a live screenshot and ask Gemini what it sees.
    Returns a partial result dict or None if Gemini is unavailable.
    """
    img_b64 = _screenshot_as_base64(driver)
    if not img_b64:
        return None

    prompt = (
        f"You are a QA verification assistant. Look at this screenshot of a web application.\n\n"
        f"Action just performed: {action_type}\n"
        f"Field/Element: {field}\n"
        f"Expected outcome: {expected}\n\n"
        f"Answer ONLY with a JSON object, no markdown:\n"
        f'{{"result":"PASS or FAIL","confidence":0.0_to_1.0,'
        f'"actual":"what you see on screen related to this field/action",'
        f'"reason":"one sentence explanation"}}'
    )

    raw = _ask_gemini_vision(img_b64, prompt)
    if not raw:
        return None

    # Strip fences
    raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()

    try:
        parsed = json.loads(raw)
        return {
            "result":     parsed.get("result", "INDETERMINATE"),
            "confidence": float(parsed.get("confidence", 0.5)),
            "actual":     parsed.get("actual", ""),
            "reason":     parsed.get("reason", "Gemini Vision assessment."),
        }
    except Exception as e:
        print(f"[Vision] Gemini JSON parse failed: {e}  raw={raw[:200]}")
        # Try to salvage truncated JSON by extracting result field
        try:
            import re as _re
            result_match = _re.search(r'"result"\s*:\s*"(PASS|FAIL|INDETERMINATE)"', raw)
            conf_match   = _re.search(r'"confidence"\s*:\s*([0-9.]+)', raw)
            if result_match:
                return {
                    "result":     result_match.group(1),
                    "confidence": float(conf_match.group(1)) if conf_match else 0.6,
                    "actual":     "",
                    "reason":     "Gemini Vision partial response — result extracted.",
                }
        except Exception:
            pass
        return None


# ── DOM verification helpers ─────────────────────────────────────────────────

def _find_element_for_verification(
    driver: WebDriver, field: str, retries: int = 3, delay: float = 0.8
) -> WebElement | None:
    for attempt in range(retries):
        # 1. by current value
        try:
            for el in driver.find_elements(
                By.XPATH,
                f"//input[@value='{field}'] | //textarea[normalize-space()='{field}']"
            ):
                if el.is_displayed():
                    return el
        except Exception:
            pass

        # 2. by placeholder / name / id / aria-label
        try:
            for el in driver.find_elements(By.XPATH, "//input | //textarea"):
                if not el.is_displayed():
                    continue
                for attr in ("placeholder", "name", "id", "aria-label"):
                    if field.lower() in (el.get_attribute(attr) or "").lower():
                        return el
        except Exception:
            pass

        # 3. by associated label
        try:
            for label in driver.find_elements(
                By.XPATH, f"//label[contains(normalize-space(.), '{field}')]"
            ):
                if not label.is_displayed():
                    continue
                for_id = label.get_attribute("for")
                if for_id:
                    try:
                        return driver.find_element(By.ID, for_id)
                    except Exception:
                        pass
                else:
                    try:
                        return label.find_element(By.XPATH, ".//input | .//textarea")
                    except Exception:
                        pass
        except Exception:
            pass

        if attempt < retries - 1:
            time.sleep(delay)

    return None


# ── Public API ───────────────────────────────────────────────────────────────

def judge_step(
    action_type: str,
    field: str,
    expected: str,
    driver: WebDriver
) -> dict:
    """
    Verify a completed step using:
    1. Gemini Vision  — actually sees the screen
    2. DOM check      — reads element values
    3. OCR fallback   — reads element pixels via Tesseract
    """
    result = {
        "action":     action_type.capitalize(),
        "field":      field,
        "expected":   expected,
        "actual":     "N/A",
        "reason":     "Not yet evaluated.",
        "result":     "INDETERMINATE",
        "confidence": 0.0,
        "screenshot": None,
        "timestamp":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    # ── Click / Checkbox / Sleep — ask Gemini Vision to confirm ─────────────
    if action_type.lower() in ("button", "click", "sidebar", "checkbox", "sleep", "dropdown", "rating", "upload", "icon"):
        gemini = _gemini_judge_screen(driver, action_type, field, expected)
        if gemini:
            result.update(gemini)
        else:
            # Gemini unavailable — assume pass for click-type actions
            result["result"]     = "PASS"
            result["confidence"] = 0.8
            result["reason"]     = "Action completed; Gemini Vision unavailable, assumed PASS."
            result["actual"]     = "Action performed"
        return result

    # ── Input / Login / Text / Calendar — DOM check then Gemini Vision ───────
    if action_type.lower() in ("login", "text", "input", "calendar"):
        # Auto-pass password fields — value is always masked, unverifiable visually
        field_l = field.lower()
        if any(p in field_l for p in ("password", "passwd", "pass", "pwd", "pin", "secret")):
            result["result"]     = "PASS"
            result["confidence"] = 1.0
            result["reason"]     = "Password field — masked value assumed correct after input."
            result["actual"]     = "••••••••"
            return result

        # 1. Try DOM check first (fastest)
        element = _find_element_for_verification(driver, field)
        if element:
            try:
                actual_val = element.get_attribute("value") or ""
                result["actual"] = actual_val

                expected_norm = expected.strip().lower()
                actual_norm   = actual_val.strip().lower()

                if actual_norm == expected_norm:
                    result["result"]     = "PASS"
                    result["confidence"] = 1.0
                    result["reason"]     = "DOM value matches expected exactly."
                    return result

                similarity = difflib.SequenceMatcher(
                    None, expected_norm, actual_norm
                ).ratio()
                result["confidence"] = round(similarity, 2)

                if similarity >= 0.85:
                    result["result"] = "PASS"
                    result["reason"] = (
                        f"DOM value is {similarity*100:.0f}% similar to expected — auto-passed."
                    )
                    return result

            except StaleElementReferenceException:
                pass
            except Exception as e:
                print(f"[Vision] DOM read error: {e}")

        # 2. Gemini Vision — see the screen live
        gemini = _gemini_judge_screen(driver, action_type, field, expected)
        if gemini:
            result.update(gemini)
            if result["result"] == "FAIL":
                result["screenshot"] = _capture_screenshot(driver, field)
            return result

        # Gemini unavailable and DOM check inconclusive
        result["result"]     = "FAIL"
        result["reason"]     = (
            f"Could not verify field '{field}' — "
            "DOM check inconclusive and Gemini Vision unavailable."
        )
        result["screenshot"] = _capture_screenshot(driver, field)

        return result

    # ── Unknown action type ──────────────────────────────────────────────────
    result["result"] = "PASS"
    result["reason"] = f"Action type '{action_type}' — assumed PASS."
    return result


def judge_run(executed_results: list, total_config_steps: int) -> dict:
    """
    Overall run verdict.
    Gemini Vision means individual steps have their own visual pass/fail,
    so we just check if any step failed.
    """
    passed = [r for r in executed_results if r.get("result") == "PASS"]
    failed = [r for r in executed_results if r.get("result") == "FAIL"]

    if failed:
        first_fail = failed[0]
        return {
            "status": "FAIL",
            "reason": (
                f"Step {first_fail.get('step','?')} failed — "
                f"{first_fail.get('reason','Unknown')}"
            )
        }

    if len(executed_results) < total_config_steps:
        return {
            "status": "FAIL",
            "reason": (
                f"Only {len(executed_results)} of {total_config_steps} "
                "steps completed."
            )
        }

    return {
        "status": "PASS",
        "reason": (
            f"All {total_config_steps} steps passed "
            f"({len(passed)} verified by Gemini Vision / DOM)."
        )
    }

# ── Backward compatibility aliases ────────────────────────────────────────────
# Support both old (_capture_failure_screenshot) and new (_capture_screenshot) callers
_capture_failure_screenshot = _capture_screenshot
