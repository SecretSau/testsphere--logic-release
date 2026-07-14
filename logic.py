"""
logic.py  —  Smart automation engine for TestSphere
Handles: calendar pickers, multiple checkboxes, smart button ranking,
         auto-scroll (page + inner containers), custom dropdowns,
         shadow DOM, modals, and Gemini Vision verification.
"""

import os
import re
import time
import json
import tempfile
from pathlib import Path
from datetime import datetime, date

# Selenium
from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    StaleElementReferenceException,
    NoSuchElementException,
    ElementNotInteractableException,
    TimeoutException,
)

# ReportLab
from reportlab.lib.pagesizes import A4
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle
)
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors

import vision


# ══════════════════════════════════════════════════════════════════════════════
#  PDF REPORT
# ══════════════════════════════════════════════════════════════════════════════

def create_pdf_report(
    results: list,
    overall_judgment: dict,
    user_override: dict,
    screenshot: str,
    folder: str
) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fullpath  = os.path.join(folder, f"execution_report_{timestamp}.pdf")
    doc    = SimpleDocTemplate(fullpath, pagesize=A4)
    styles = getSampleStyleSheet()
    story  = []

    story.append(Paragraph(f"Execution Report — {timestamp}", styles["h1"]))
    story.append(Spacer(1, 12))

    summary = (
        f"<b>Steps Executed:</b> {len(results)} of "
        f"{overall_judgment.get('total_steps','N/A')}<br/>"
        f"<b>Final Judgment:</b> {overall_judgment.get('status','N/A')} "
        f"— {overall_judgment.get('reason','')}<br/>"
    )
    if user_override and user_override.get("applied"):
        summary += (
            f"<b>User Override:</b> {user_override['final_status']} "
            f"(Reason: {user_override.get('reason','N/A')})"
        )
    else:
        summary += "<b>User Override:</b> None"

    story.append(Paragraph(summary, styles["Normal"]))
    story.append(Spacer(1, 24))

    normal = styles["Normal"]
    header = ["Step", "Action", "Timestamp", "Expected", "Actual", "Result", "Reason"]
    rows   = [header]
    for r in results:
        rows.append([
            str(r.get("step", "")),
            Paragraph(str(r.get("action", "")), normal),
            Paragraph(str(r.get("timestamp", "")), normal),
            Paragraph(str(r.get("expected", "")), normal),
            Paragraph(str(r.get("actual", "")), normal),
            str(r.get("result", "")),
            Paragraph(str(r.get("reason", "")), normal),
        ])

    tbl = Table(rows, colWidths=[30, 55, 75, 90, 90, 45, 120])
    tbl.setStyle(TableStyle([
        ("BACKGROUND",  (0, 0), (-1,  0), colors.darkgrey),
        ("TEXTCOLOR",   (0, 0), (-1,  0), colors.whitesmoke),
        ("FONTNAME",    (0, 0), (-1,  0), "Helvetica-Bold"),
        ("ALIGN",       (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",      (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND",  (0, 1), (-1, -1), colors.beige),
        ("GRID",        (0, 0), (-1, -1), 0.5, colors.black),
        ("BOTTOMPADDING",(0,0),(-1,  0), 10),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 24))

    story.append(Paragraph("Final Screenshot", styles["h2"]))
    story.append(Spacer(1, 8))
    if screenshot and os.path.exists(screenshot):
        try:
            from reportlab.lib.utils import ImageReader
            max_w  = A4[0] - 100
            ir     = ImageReader(screenshot)
            iw, ih = ir.getSize()
            img    = Image(screenshot, width=max_w, height=max_w * ih / iw)
            story.append(img)
        except Exception as ex:
            story.append(Paragraph(f"[Screenshot error: {ex}]", styles["Normal"]))
    else:
        story.append(Paragraph("[No screenshot available]", styles["Normal"]))

    doc.build(story)
    return fullpath


# ══════════════════════════════════════════════════════════════════════════════
#  SMART ELEMENT FINDER  —  scroll-aware, multi-strategy
# ══════════════════════════════════════════════════════════════════════════════

class ElementFinder:
    """
    Finds ANY interactable element on a page using layered strategies:
    1. Viewport scan
    2. Incremental page scroll + rescan
    3. Inner container scroll (modals, panels)
    4. JavaScript fallback
    """

    SCROLL_PAUSE   = 0.4   # seconds to wait after each scroll
    SCROLL_STEP_PC = 0.30  # scroll 30 % of viewport height per step

    def __init__(self, driver: webdriver.Chrome):
        self.driver = driver

    # ── public helpers ────────────────────────────────────────────────────────

    def scroll_to(self, element) -> None:
        """Scroll element to the center of the viewport."""
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center',inline:'nearest'});",
            element
        )
        time.sleep(0.3)

    def safe_click(self, element) -> bool:
        """Click with fallbacks: normal → JS → ActionChains."""
        self.scroll_to(element)
        for method in (
            lambda: element.click(),
            lambda: self.driver.execute_script("arguments[0].click();", element),
            lambda: ActionChains(self.driver).move_to_element(element).click().perform(),
        ):
            try:
                method()
                return True
            except (ElementClickInterceptedException,
                    ElementNotInteractableException):
                time.sleep(0.3)
            except Exception:
                pass
        return False

    def safe_type(self, element, value: str) -> bool:
        """Type into an element with JS fallback."""
        self.scroll_to(element)
        try:
            element.clear()
            element.send_keys(value)
            return True
        except ElementNotInteractableException:
            pass
        try:
            self.driver.execute_script(
                "arguments[0].value = arguments[1]; "
                "arguments[0].dispatchEvent(new Event('input',{bubbles:true})); "
                "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
                element, value
            )
            return True
        except Exception as e:
            print(f"[Finder] JS type fallback failed: {e}")
            return False

    # ── scoring ───────────────────────────────────────────────────────────────

    def _score_element(self, el, identifier: str) -> int:
        """Score how well an element matches an identifier (higher = better)."""
        score = 0
        ident_lower = identifier.lower()
        try:
            if not el.is_displayed():
                return -999

            for attr in ("text", "aria-label", "title", "placeholder",
                         "name", "id", "value", "data-testid"):
                val = (
                    el.text if attr == "text"
                    else (el.get_attribute(attr) or "")
                ).lower()
                if val == ident_lower:
                    score += 10
                elif ident_lower in val:
                    score += 5

            # Prefer elements in viewport
            rect = self.driver.execute_script(
                "var r=arguments[0].getBoundingClientRect();"
                "return {top:r.top,left:r.left,bottom:r.bottom,right:r.right};",
                el
            )
            vw = self.driver.execute_script("return window.innerWidth;")
            vh = self.driver.execute_script("return window.innerHeight;")
            if 0 <= rect["top"] <= vh and 0 <= rect["left"] <= vw:
                score += 3

            # Prefer elements in modals / dialogs
            try:
                el.find_element(By.XPATH,
                    "./ancestor::*[contains(@class,'modal') or "
                    "contains(@class,'dialog') or contains(@role,'dialog')]"
                )
                score += 2
            except Exception:
                pass

        except StaleElementReferenceException:
            return -999
        except Exception:
            pass
        return score

    # ── page scroll iterator ──────────────────────────────────────────────────

    def _scroll_positions(self):
        """Yield (scrollY) positions to visit across the full page."""
        total_h  = self.driver.execute_script("return document.body.scrollHeight;")
        view_h   = self.driver.execute_script("return window.innerHeight;")
        step     = max(int(view_h * self.SCROLL_STEP_PC), 100)
        pos = 0
        while pos <= total_h:
            yield pos
            pos += step
        yield total_h   # always check the bottom

    def _scroll_page_to(self, y: int) -> None:
        self.driver.execute_script(f"window.scrollTo(0, {y});")
        time.sleep(self.SCROLL_PAUSE)

    def _scroll_containers(self, identifier: str):
        """
        Scroll inner containers (panels, modals, overflow divs)
        and return best candidate found.
        """
        containers = self.driver.find_elements(
            By.XPATH,
            "//*[contains(@class,'modal') or contains(@class,'panel') or "
            "contains(@class,'scroll') or contains(@style,'overflow')]"
        )
        for cont in containers:
            try:
                cont_h = self.driver.execute_script(
                    "return arguments[0].scrollHeight;", cont
                )
                step = max(cont_h // 4, 100)
                for pos in range(0, cont_h + step, step):
                    self.driver.execute_script(
                        "arguments[0].scrollTop = arguments[1];", cont, pos
                    )
                    time.sleep(self.SCROLL_PAUSE)
            except Exception:
                pass
        return None

    # ── strategy runners ─────────────────────────────────────────────────────

    def find_clickable(self, identifier: str) -> object | None:
        """
        Find the best clickable element matching identifier.
        Searches buttons, links, divs, spans, roles.
        Auto-scrolls entire page.
        """
        xpaths = [
            # Exact text matches (buttons, anchors)
            f"//button[normalize-space(.)='{identifier}']",
            f"//a[normalize-space(.)='{identifier}']",
            # aria-label / title / data-testid
            f"//*[@aria-label='{identifier}']",
            f"//*[@title='{identifier}']",
            f"//*[@data-testid='{identifier}']",
            # Partial text (broader)
            f"//button[contains(normalize-space(.),'{identifier}')]",
            f"//a[contains(normalize-space(.),'{identifier}')]",
            f"//*[@role='button' and contains(normalize-space(.),'{identifier}')]",
            f"//*[@role='menuitem' and contains(normalize-space(.),'{identifier}')]",
            f"//*[@role='tab' and contains(normalize-space(.),'{identifier}')]",
            f"//*[@role='option' and contains(normalize-space(.),'{identifier}')]",
            f"//*[@role='link' and contains(normalize-space(.),'{identifier}')]",
            # Generic text match
            f"//*[normalize-space(text())='{identifier}']",
            f"//*[contains(normalize-space(text()),'{identifier}')]",
        ]

        best_el, best_score = None, -1

        # First try current viewport
        candidates = self._collect_by_xpaths(xpaths)
        best_el, best_score = self._best_candidate(candidates, identifier)

        if best_el and best_score >= 5:
            return best_el

        # Scroll page and retry
        self._scroll_page_to(0)
        for y in self._scroll_positions():
            self._scroll_page_to(y)
            candidates = self._collect_by_xpaths(xpaths)
            el, score  = self._best_candidate(candidates, identifier)
            if el and score > best_score:
                best_el, best_score = el, score
            if best_score >= 10:
                break

        # Inner containers
        if not best_el or best_score < 3:
            self._scroll_containers(identifier)
            candidates = self._collect_by_xpaths(xpaths)
            el, score  = self._best_candidate(candidates, identifier)
            if el and score > best_score:
                best_el = el

        return best_el

    # ── login field finder ───────────────────────────────────────────────────────

    # Common field name variations for email / username / password fields
    _EMAIL_VARIANTS = [
        "email", "e-mail", "e_mail", "email address", "emailaddress",
        "username", "user name", "user_name", "login", "log in",
        "signin", "sign in", "account", "id", "user id", "userid",
    ]
    _PASSWORD_VARIANTS = [
        "password", "pass", "passwd", "passcode", "pin",
        "secret", "credential", "pwd",
    ]

    def find_login_field(self, identifier: str) -> object | None:
        """
        Smart login field finder.
        Tries the exact identifier first, then falls back to common
        email/username/password field variations automatically.

        Searches by:
        - label text (exact + partial + case-insensitive)
        - placeholder, name, id, aria-label attributes
        - input type (email, password, text)
        - autocomplete attribute (email, username, current-password)
        - Common class names
        """
        ident_l = identifier.lower().strip()

        # Build candidate list:
        # Start with the exact identifier, then add relevant variants
        if any(v in ident_l for v in ["pass", "pwd", "secret", "pin", "credential"]):
            candidates = [identifier] + self._PASSWORD_VARIANTS
            input_types = ["password", "text"]
            autocomplete_vals = ["current-password", "new-password", "password"]
        else:
            candidates = [identifier] + self._EMAIL_VARIANTS
            input_types = ["email", "text", "tel"]
            autocomplete_vals = ["email", "username", "login"]

        def _scan_all():
            # Strategy 1: Try each candidate through find_input
            for cand in candidates:
                try:
                    el = self._find_input_by_label_or_attr(cand)
                    if el:
                        return el
                except Exception:
                    pass

            # Strategy 2: input[type=email/password]
            for t in input_types:
                try:
                    for el in self.driver.find_elements(
                        By.XPATH, f"//input[@type='{t}']"
                    ):
                        if el.is_displayed():
                            return el
                except Exception:
                    pass

            # Strategy 3: autocomplete attribute
            for ac in autocomplete_vals:
                try:
                    for el in self.driver.find_elements(
                        By.XPATH, f"//input[@autocomplete='{ac}']"
                    ):
                        if el.is_displayed():
                            return el
                except Exception:
                    pass

            # Strategy 4: name/id/placeholder contains any variant
            for cand in candidates:
                cand_l = cand.lower()
                for attr in ("name", "id", "placeholder", "aria-label"):
                    try:
                        xp = (
                            f"//input[contains("
                            f"translate(@{attr},'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                            f"'abcdefghijklmnopqrstuvwxyz'),'{cand_l}')]"
                        )
                        for el in self.driver.find_elements(By.XPATH, xp):
                            if el.is_displayed():
                                return el
                    except Exception:
                        pass

            return None

        # Try current viewport first
        el = _scan_all()
        if el:
            return el

        # Scroll and retry
        self._scroll_page_to(0)
        for y in self._scroll_positions():
            self._scroll_page_to(y)
            el = _scan_all()
            if el:
                return el

        # Last resort: first visible text/email/password input on page
        for t in input_types:
            try:
                for el in self.driver.find_elements(
                    By.XPATH, f"//input[@type='{t}']"
                ):
                    if el.is_displayed():
                        return el
            except Exception:
                pass

        return None

    def _find_input_by_label_or_attr(self, identifier: str) -> object | None:
        """
        Find a single input by label text or common attributes.
        Case-insensitive, partial match.
        """
        ident_l = identifier.lower().strip()

        # 1. Label → associated input
        try:
            lbl_xp = (
                f"//label[contains("
                f"translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                f"'abcdefghijklmnopqrstuvwxyz'),'{ident_l}')]"
            )
            for lbl in self.driver.find_elements(By.XPATH, lbl_xp):
                if not lbl.is_displayed():
                    continue
                for_id = lbl.get_attribute("for")
                if for_id:
                    try:
                        el = self.driver.find_element(By.ID, for_id)
                        if el.is_displayed():
                            return el
                    except Exception:
                        pass
                try:
                    el = lbl.find_element(
                        By.XPATH,
                        ".//following::input[1] | .//following::textarea[1]"
                    )
                    if el.is_displayed():
                        return el
                except Exception:
                    pass
        except Exception:
            pass

        # 2. Attributes: placeholder, name, id, aria-label
        for attr in ("placeholder", "name", "id", "aria-label"):
            try:
                xp = (
                    f"//input[contains("
                    f"translate(@{attr},'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                    f"'abcdefghijklmnopqrstuvwxyz'),'{ident_l}')] | "
                    f"//textarea[contains("
                    f"translate(@{attr},'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                    f"'abcdefghijklmnopqrstuvwxyz'),'{ident_l}')]"
                )
                for el in self.driver.find_elements(By.XPATH, xp):
                    if el.is_displayed():
                        return el
            except Exception:
                pass

        return None

    def find_input(self, identifier: str) -> object | None:
        """
        Find an input / textarea matching identifier.
        Strategies: label → ID/name → aria → placeholder → header → scroll.
        """
        def _scan():
            # 1. label → associated input
            try:
                lbl_xpath = (
                    f"//label[contains(translate(normalize-space(.),"
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                    f"'{identifier.lower()}')]"
                )
                for lbl in self.driver.find_elements(By.XPATH, lbl_xpath):
                    if not lbl.is_displayed():
                        continue
                    for_id = lbl.get_attribute("for")
                    if for_id:
                        try:
                            el = self.driver.find_element(By.ID, for_id)
                            if el.is_displayed():
                                return el
                        except Exception:
                            pass
                    try:
                        el = lbl.find_element(
                            By.XPATH, ".//following::input[1] | .//following::textarea[1]"
                        )
                        if el.is_displayed():
                            return el
                    except Exception:
                        pass
            except Exception:
                pass

            # 2. ID / name exact
            for strat, val in ((By.ID, identifier), (By.NAME, identifier)):
                try:
                    el = self.driver.find_element(strat, val)
                    if el.is_displayed():
                        return el
                except Exception:
                    pass

            # 3. aria-label / placeholder (contains)
            ident_l = identifier.lower()
            tr = (f"translate(normalize-space(.),"
                  f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')")
            for attr in ("aria-label", "placeholder", "name", "id"):
                try:
                    xpath = (
                        f"//input[contains(translate(@{attr},"
                        f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                        f"'{ident_l}')] | "
                        f"//textarea[contains(translate(@{attr},"
                        f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                        f"'{ident_l}')]"
                    )
                    for el in self.driver.find_elements(By.XPATH, xpath):
                        if el.is_displayed():
                            return el
                except Exception:
                    pass

            # 4. Nearby header → form-group input
            try:
                hdr_xpath = " | ".join(
                    f"//h{i}[contains({tr},'{identifier.lower()}')]"
                    for i in range(1, 7)
                )
                for hdr in self.driver.find_elements(By.XPATH, hdr_xpath):
                    try:
                        fg = hdr.find_element(
                            By.XPATH,
                            "./ancestor::div[contains(@class,'form') or "
                            "contains(@class,'field') or contains(@class,'group')][1]"
                        )
                        for inp in fg.find_elements(
                            By.XPATH, ".//input | .//textarea"
                        ):
                            if inp.is_displayed():
                                return inp
                    except Exception:
                        pass
            except Exception:
                pass

            return None

        # Try current view first
        el = _scan()
        if el:
            return el

        # Scroll and retry
        self._scroll_page_to(0)
        for y in self._scroll_positions():
            self._scroll_page_to(y)
            el = _scan()
            if el:
                return el

        # Inner container scroll
        self._scroll_containers(identifier)
        return _scan()

    def find_select(self, identifier: str) -> object | None:
        """Find a native <select> element."""
        def _scan():
            ident_l = identifier.lower()
            # by name/id
            for el in self.driver.find_elements(By.TAG_NAME, "select"):
                if not el.is_displayed():
                    continue
                for attr in ("name", "id", "aria-label"):
                    val = (el.get_attribute(attr) or "").lower()
                    if ident_l in val:
                        return el
            # by label
            try:
                lbl_xpath = (
                    f"//label[contains(translate(normalize-space(.),"
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                    f"'{ident_l}')]"
                )
                for lbl in self.driver.find_elements(By.XPATH, lbl_xpath):
                    if not lbl.is_displayed():
                        continue
                    try:
                        sel = lbl.find_element(
                            By.XPATH, ".//following-sibling::select | .//select"
                        )
                        if sel.is_displayed():
                            return sel
                    except Exception:
                        pass
            except Exception:
                pass
            return None

        el = _scan()
        if el:
            return el
        self._scroll_page_to(0)
        for y in self._scroll_positions():
            self._scroll_page_to(y)
            el = _scan()
            if el:
                return el
        return None

    def find_checkbox(
        self, label_text: str, index: int = 0
    ) -> object | None:
        """
        Find a checkbox by label text with optional index (0-based).
        Handles: standard <input type=checkbox>, toggle-switch divs.
        """
        def _scan():
            matches = []
            ident_l = label_text.lower()

            # Strategy 1: label contains text → checkbox input
            try:
                lbl_xpath = (
                    f"//label[contains(translate(normalize-space(.),"
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                    f"'{ident_l}')]"
                )
                for lbl in self.driver.find_elements(By.XPATH, lbl_xpath):
                    if not lbl.is_displayed():
                        continue
                    for_id = lbl.get_attribute("for")
                    cb = None
                    if for_id:
                        try:
                            cb = self.driver.find_element(By.ID, for_id)
                        except Exception:
                            pass
                    if not cb:
                        try:
                            cb = lbl.find_element(
                                By.XPATH,
                                ".//input[@type='checkbox'] | "
                                ".//preceding::input[@type='checkbox'][1]"
                            )
                        except Exception:
                            pass
                    if cb:
                        matches.append(("checkbox", cb, lbl))
            except Exception:
                pass

            # Strategy 2: input[@type='checkbox'] with aria-label / name / id
            try:
                for cb in self.driver.find_elements(
                    By.XPATH, "//input[@type='checkbox']"
                ):
                    if not cb.is_displayed():
                        continue
                    for attr in ("aria-label", "name", "id", "title"):
                        val = (cb.get_attribute(attr) or "").lower()
                        if ident_l in val:
                            matches.append(("checkbox", cb, None))
                            break
            except Exception:
                pass

            # Strategy 3: toggle/switch divs
            try:
                toggle_xpath = (
                    "//*[contains(@class,'toggle') or contains(@class,'switch') "
                    "or @role='switch' or @role='checkbox']"
                )
                for tog in self.driver.find_elements(By.XPATH, toggle_xpath):
                    if not tog.is_displayed():
                        continue
                    txt = tog.text.strip().lower()
                    aria = (tog.get_attribute("aria-label") or "").lower()
                    if ident_l in txt or ident_l in aria:
                        matches.append(("toggle", tog, None))
            except Exception:
                pass

            return matches

        matches = _scan()
        if not matches:
            self._scroll_page_to(0)
            for y in self._scroll_positions():
                self._scroll_page_to(y)
                matches = _scan()
                if matches:
                    break

        if not matches:
            return None

        # Return by index
        idx = min(index, len(matches) - 1)
        kind, element, label_el = matches[idx]
        return element

    # ── calendar helpers ──────────────────────────────────────────────────────

    def fill_calendar(self, identifier: str, date_str: str) -> bool:
        """
        Fill a date field. Handles:
        1. <input type='date'>  → direct value set
        2. Text inputs that format dates → send keys
        3. Datepicker popups → open, navigate, click day
        Returns True on success.
        """
        target_date = self._parse_date(date_str)
        if not target_date:
            print(f"[Calendar] Cannot parse date: {date_str}")
            return False

        inp = self.find_input(identifier)
        if not inp:
            print(f"[Calendar] Cannot find calendar field: {identifier}")
            return False

        self.scroll_to(inp)
        inp_type = (inp.get_attribute("type") or "").lower()

        # 1. Native date input
        if inp_type == "date":
            iso = target_date.strftime("%Y-%m-%d")
            try:
                self.driver.execute_script(
                    "arguments[0].value = arguments[1]; "
                    "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
                    inp, iso
                )
                print(f"[Calendar] Native date set: {iso}")
                return True
            except Exception as e:
                print(f"[Calendar] Native date set failed: {e}")

        # 2. Click to open popup, then navigate
        try:
            inp.click()
            time.sleep(0.6)

            # Check if a datepicker popup appeared
            popup_xpaths = [
                "//*[contains(@class,'datepicker') or contains(@class,'calendar') "
                "or contains(@class,'picker') or contains(@role,'dialog')]"
                "[not(contains(@style,'display:none')) and not(contains(@style,'display: none'))]",
                "//*[@data-handler='selectDay']",
            ]
            popup = None
            for xp in popup_xpaths:
                els = self.driver.find_elements(By.XPATH, xp)
                if els:
                    popup = els[0]
                    break

            if popup:
                success = self._navigate_datepicker(target_date)
                if success:
                    return True
        except Exception as e:
            print(f"[Calendar] Popup open failed: {e}")

        # 3. Formatted text input fallback
        formatted = self._format_date_for_input(inp, target_date)
        try:
            inp.click()
            time.sleep(0.3)
            inp.clear()
            inp.send_keys(formatted)
            inp.send_keys(Keys.TAB)
            print(f"[Calendar] Text input date: {formatted}")
            return True
        except Exception:
            pass

        # 4. JS force-set
        try:
            iso = target_date.strftime("%Y-%m-%d")
            self.driver.execute_script(
                "arguments[0].value=arguments[1];"
                "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
                "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
                inp, iso
            )
            return True
        except Exception as e:
            print(f"[Calendar] JS fallback failed: {e}")
            return False

    def _parse_date(self, date_str: str) -> date | None:
        """Parse various date string formats."""
        formats = [
            "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y",
            "%B %d, %Y", "%b %d, %Y", "%d %B %Y",
            "%m-%d-%Y", "%Y/%m/%d",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(date_str.strip(), fmt).date()
            except ValueError:
                pass
        return None

    def _format_date_for_input(self, inp, target: date) -> str:
        """Guess the date format the input expects."""
        placeholder = (inp.get_attribute("placeholder") or "").upper()
        if "MM/DD/YYYY" in placeholder:
            return target.strftime("%m/%d/%Y")
        if "DD/MM/YYYY" in placeholder:
            return target.strftime("%d/%m/%Y")
        if "YYYY-MM-DD" in placeholder:
            return target.strftime("%Y-%m-%d")
        return target.strftime("%m/%d/%Y")  # default US format

    def _navigate_datepicker(self, target: date) -> bool:
        """
        Navigate a standard datepicker popup to the target date and click the day.
        Works with jQuery UI, Bootstrap Datepicker, Flatpickr, and similar.
        """
        max_nav = 24  # max months to navigate
        for _ in range(max_nav):
            # Read current month/year displayed
            current = self._get_displayed_month_year()
            if not current:
                break
            cur_year, cur_month = current

            if cur_year == target.year and cur_month == target.month:
                # Click the correct day
                day_xpaths = [
                    f"//*[@data-date='{target.strftime('%Y-%m-%d')}']",
                    f"//*[@data-day='{target.day}']",
                    (
                        f"//td[not(contains(@class,'disabled')) and "
                        f"normalize-space(.)='{target.day}']"
                    ),
                    (
                        f"//td[@data-handler='selectDay']"
                        f"[normalize-space(.)='{target.day}']"
                    ),
                    (
                        f"//*[contains(@class,'day') and not(contains(@class,'disabled')) "
                        f"and normalize-space(.)='{target.day}']"
                    ),
                ]
                for xp in day_xpaths:
                    try:
                        days = self.driver.find_elements(By.XPATH, xp)
                        for d in days:
                            if d.is_displayed():
                                self.safe_click(d)
                                print(f"[Calendar] Clicked day {target.day}")
                                return True
                    except Exception:
                        pass
                break

            # Navigate forward or backward
            if (cur_year, cur_month) < (target.year, target.month):
                self._click_datepicker_nav("next")
            else:
                self._click_datepicker_nav("prev")
            time.sleep(0.4)

        return False

    def _get_displayed_month_year(self):
        """Read the month/year header from an open datepicker."""
        header_xpaths = [
            "//*[contains(@class,'datepicker-switch') or "
            "contains(@class,'month') or contains(@class,'calendar-caption') "
            "or contains(@class,'picker__month')]",
            "//th[@class='datepicker-switch']",
            "//*[@data-handler='selectMonth'] | //*[@data-handler='selectYear']",
        ]
        month_names = {
            "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
            "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
            "jan":1,"feb":2,"mar":3,"apr":4,"jun":6,"jul":7,
            "aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
        }
        for xp in header_xpaths:
            try:
                for el in self.driver.find_elements(By.XPATH, xp):
                    txt = el.text.strip()
                    if not txt:
                        continue
                    # Try "Month YYYY" or "YYYY-MM"
                    parts = txt.replace(",", "").split()
                    for p in parts:
                        if p.isdigit() and len(p) == 4:
                            year = int(p)
                            for mp in parts:
                                mp_l = mp.lower()
                                if mp_l in month_names:
                                    return year, month_names[mp_l]
                            # Try numeric month
                            nums = [x for x in parts if x.isdigit() and len(x) <= 2]
                            if nums:
                                return year, int(nums[0])
            except Exception:
                pass
        return None

    def _click_datepicker_nav(self, direction: str) -> None:
        """Click next/prev arrow on a datepicker."""
        if direction == "next":
            xpaths = [
                "//*[contains(@class,'next') or @data-handler='next' "
                "or contains(@aria-label,'next') or contains(@aria-label,'Next')]",
                "//button[contains(.,'›') or contains(.,'→') or contains(.,'»')]",
            ]
        else:
            xpaths = [
                "//*[contains(@class,'prev') or @data-handler='prev' "
                "or contains(@aria-label,'prev') or contains(@aria-label,'Prev')]",
                "//button[contains(.,'‹') or contains(.,'←') or contains(.,'«')]",
            ]
        for xp in xpaths:
            try:
                els = self.driver.find_elements(By.XPATH, xp)
                for el in els:
                    if el.is_displayed():
                        self.safe_click(el)
                        return
            except Exception:
                pass

    # ── custom dropdown ───────────────────────────────────────────────────────

    def select_custom_dropdown(self, identifier: str, option_text: str) -> bool:
        """
        Handle ALL dropdown types:
        - Native <select>
        - Custom div/ul dropdowns (React Select, Vue Select, etc.)
        - Combobox inputs
        - Options appended to <body> (portal dropdowns)
        """
        ident_l  = identifier.lower().strip()
        option_l = option_text.lower().strip()

        # ── Step 1: Native <select> ───────────────────────────────────────────
        sel_el = self.find_select(identifier)
        if sel_el:
            try:
                self.scroll_to(sel_el)
                Select(sel_el).select_by_visible_text(option_text)
                print(f"[Dropdown] Native select: {identifier} = {option_text}")
                return True
            except Exception:
                try:
                    Select(sel_el).select_by_value(option_text)
                    return True
                except Exception:
                    pass
                # Partial text match on native select
                try:
                    sel_obj = Select(sel_el)
                    for opt in sel_obj.options:
                        if option_l in opt.text.lower():
                            sel_obj.select_by_visible_text(opt.text)
                            print(f"[Dropdown] Native partial: {opt.text}")
                            return True
                except Exception:
                    pass

        # ── Step 2: Find trigger ──────────────────────────────────────────────
        def _find_trigger():
            # 1. label → associated element
            label_xpaths = [
                f"//label[contains(translate(normalize-space(.),"
                f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                f"'{ident_l}')]",
                f"//*[contains(@class,'label') and contains(translate(normalize-space(.),"
                f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                f"'{ident_l}')]",
            ]
            for lbl_xp in label_xpaths:
                try:
                    for lbl in self.driver.find_elements(By.XPATH, lbl_xp):
                        if not lbl.is_displayed():
                            continue
                        for_id = lbl.get_attribute("for")
                        if for_id:
                            try:
                                el = self.driver.find_element(By.ID, for_id)
                                if el.is_displayed():
                                    return el
                            except Exception:
                                pass
                        sibling_xpaths = [
                            "./following-sibling::*[@role='combobox'][1]",
                            "./following-sibling::*[contains(@class,'select')][1]",
                            "./following-sibling::*[contains(@class,'dropdown')][1]",
                            "./following-sibling::div[1]",
                            "./parent::*//*[@role='combobox'][1]",
                            "./parent::*//*[contains(@class,'select')][1]",
                            "./parent::div/following-sibling::div//*[@role='combobox'][1]",
                        ]
                        for sxp in sibling_xpaths:
                            try:
                                el = lbl.find_element(By.XPATH, sxp)
                                if el.is_displayed():
                                    return el
                            except Exception:
                                pass
                except Exception:
                    pass

            # 2. role=combobox with matching attributes
            for attr in ("aria-label", "placeholder", "name", "id"):
                try:
                    xp = (
                        f"//*[@role='combobox'][contains("
                        f"translate(@{attr},'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                        f"'abcdefghijklmnopqrstuvwxyz'),'{ident_l}')]"
                    )
                    for el in self.driver.find_elements(By.XPATH, xp):
                        if el.is_displayed():
                            return el
                except Exception:
                    pass

            # 3. Only one combobox on page — use it
            try:
                combos = [
                    el for el in self.driver.find_elements(By.XPATH, "//*[@role='combobox']")
                    if el.is_displayed()
                ]
                if len(combos) == 1:
                    return combos[0]
            except Exception:
                pass

            # 4. Class-based triggers
            class_xpaths = [
                f"//*[contains(@class,'select') or contains(@class,'dropdown')]"
                f"[contains(translate(normalize-space(.),"
                f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{ident_l}')]",
                "//*[contains(@class,'select__control') or "
                "contains(@class,'dropdown-toggle') or "
                "contains(@class,'multiselect__select')]",
            ]
            for xp in class_xpaths:
                try:
                    for el in self.driver.find_elements(By.XPATH, xp):
                        if el.is_displayed():
                            return el
                except Exception:
                    pass
            return None

        trigger = _find_trigger()
        if not trigger:
            self._scroll_page_to(0)
            for y in self._scroll_positions():
                self._scroll_page_to(y)
                trigger = _find_trigger()
                if trigger:
                    break

        if not trigger:
            print(f"[Dropdown] No trigger found for: '{identifier}'")
            return False

        # ── Step 3: Click trigger ─────────────────────────────────────────────
        self.scroll_to(trigger)
        self.safe_click(trigger)
        time.sleep(1.0)

        # ── Step 4: Find option ───────────────────────────────────────────────
        def _find_option():
            option_xpaths = [
                f"//*[@role='option' and normalize-space(.)='{option_text}']",
                f"//*[@role='option' and contains(normalize-space(.),'{option_text}')]",
                f"//*[@role='option' and contains(translate(normalize-space(.),"
                f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{option_l}')]",
                f"//li[normalize-space(.)='{option_text}']",
                f"//li[contains(normalize-space(.),'{option_text}')]",
                f"//li[contains(translate(normalize-space(.),"
                f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{option_l}')]",
                f"//*[contains(@class,'option') and contains(translate(normalize-space(.),"
                f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{option_l}')]",
                f"//*[contains(@class,'item') and contains(translate(normalize-space(.),"
                f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{option_l}')]",
                f"//*[contains(@class,'select__option') and contains(translate(normalize-space(.),"
                f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{option_l}')]",
                f"//*[contains(@class,'menu-item') and contains(translate(normalize-space(.),"
                f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{option_l}')]",
                f"//*[contains(@class,'multiselect__element') and contains("
                f"translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                f"'abcdefghijklmnopqrstuvwxyz'),'{option_l}')]",
            ]
            for xp in option_xpaths:
                try:
                    for opt in self.driver.find_elements(By.XPATH, xp):
                        if opt.is_displayed():
                            return opt
                except Exception:
                    pass
            return None

        option_el = _find_option()

        # Scroll inside options container if not found
        if not option_el:
            container_xpaths = [
                "//*[@role='listbox']",
                "//*[contains(@class,'select__menu')]",
                "//*[contains(@class,'dropdown-menu')]",
                "//*[contains(@class,'options-container')]",
                "//*[contains(@class,'menu-list')]",
                "//*[contains(@class,'multiselect__content')]",
            ]
            for cxp in container_xpaths:
                try:
                    for container in self.driver.find_elements(By.XPATH, cxp):
                        if not container.is_displayed():
                            continue
                        cont_h = self.driver.execute_script(
                            "return arguments[0].scrollHeight;", container
                        )
                        step = max(cont_h // 5, 50)
                        for pos in range(0, cont_h + step, step):
                            self.driver.execute_script(
                                "arguments[0].scrollTop = arguments[1];", container, pos
                            )
                            time.sleep(0.2)
                            option_el = _find_option()
                            if option_el:
                                break
                        if option_el:
                            break
                except Exception:
                    pass
                if option_el:
                    break

        # Last resort: re-click and wait longer
        if not option_el:
            self.safe_click(trigger)
            time.sleep(2.0)
            option_el = _find_option()

        if option_el:
            self.scroll_to(option_el)
            self.safe_click(option_el)
            print(f"[Dropdown] Selected: '{option_text}'")
            time.sleep(0.5)
            return True

        print(f"[Dropdown] Option '{option_text}' not found for '{identifier}'")
        return False
    # ── rating scale ──────────────────────────────────────────────────────────

    def fill_rating(self, identifier: str, value: int) -> bool:
        """
        Find ANY rating widget and set it to value.
        Supports:
        1. Star buttons (click nth star)
        2. Range/slider inputs (set value via JS)
        3. Radio buttons with numeric labels
        4. Numeric input fields
        5. Custom div/span rating widgets (click nth item)
        Identifier is optional — pass empty string to find first rating on page.
        Auto-scrolls.
        """
        def _scan():
            # 1. Range/slider input
            slider_xpaths = [
                "//input[@type='range']",
                "//input[contains(@class,'rating') or contains(@class,'slider') or contains(@class,'score')]",
            ]
            if identifier:
                slider_xpaths += [
                    f"//label[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{identifier.lower()}')]"
                    f"/following::input[@type='range'][1]",
                ]
            for xp in slider_xpaths:
                try:
                    for el in self.driver.find_elements(By.XPATH, xp):
                        if el.is_displayed():
                            return ("slider", el)
                except Exception:
                    pass

            # 2. Star / icon buttons — look for repeated clickable elements
            star_xpaths = [
                "//*[contains(@class,'star') or contains(@class,'rating-item') "
                "or contains(@class,'rating-star') or contains(@class,'ri-star') "
                "or contains(@aria-label,'star') or contains(@data-rating,'')]",
                "//*[@role='radio' and (contains(@class,'star') or contains(@class,'rating'))]",
            ]
            if identifier:
                star_xpaths.append(
                    f"//*[contains(translate(normalize-space(ancestor::*[@class][1]/@class),"
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                    f"'{identifier.lower()}')]"
                    f"[contains(@class,'star') or contains(@class,'rating')]"
                )
            for xp in star_xpaths:
                try:
                    stars = [e for e in self.driver.find_elements(By.XPATH, xp) if e.is_displayed()]
                    if len(stars) >= 2:
                        return ("stars", stars)
                except Exception:
                    pass

            # 3. Radio buttons with numeric value labels
            try:
                radios = self.driver.find_elements(
                    By.XPATH, "//input[@type='radio']"
                )
                numeric_radios = []
                for r in radios:
                    v = r.get_attribute("value") or ""
                    if v.isdigit():
                        numeric_radios.append(r)
                if len(numeric_radios) >= 2:
                    # Optionally filter by label context
                    if identifier:
                        context = [
                            r for r in numeric_radios
                            if identifier.lower() in (
                                self.driver.execute_script(
                                    "var lbl=document.querySelector('[for=\"'+arguments[0].id+'\"]');"
                                    "return lbl?lbl.innerText:'';", r
                                ) or ""
                            ).lower()
                        ]
                        if context:
                            numeric_radios = context
                    return ("radio", numeric_radios)
            except Exception:
                pass

            # 4. Numeric input (score field)
            if identifier:
                inp = self.find_input(identifier)
                if inp:
                    inp_type = (inp.get_attribute("type") or "").lower()
                    if inp_type in ("number", "text", ""):
                        return ("number", inp)

            # 5. Custom div/span list (e.g. NPS 0-10 buttons)
            custom_xpaths = [
                "//*[@role='listbox' or @role='radiogroup']"
                "//*[@role='option' or @role='radio']",
                "//ul[contains(@class,'rating') or contains(@class,'scale')]//li",
                "//div[contains(@class,'nps') or contains(@class,'rating') or contains(@class,'scale')]"
                "//*[string-length(normalize-space(.))<=2 and string-length(normalize-space(.))>=1]",
            ]
            for xp in custom_xpaths:
                try:
                    items = [e for e in self.driver.find_elements(By.XPATH, xp) if e.is_displayed()]
                    if len(items) >= 2:
                        return ("custom", items)
                except Exception:
                    pass

            return None

        # Try current view
        found = _scan()
        if not found:
            self._scroll_page_to(0)
            for y in self._scroll_positions():
                self._scroll_page_to(y)
                found = _scan()
                if found:
                    break

        if not found:
            print(f"[Rating] No rating widget found for '{identifier}'")
            return False

        kind, widget = found

        try:
            if kind == "slider":
                self.scroll_to(widget)
                min_val = float(widget.get_attribute("min") or 0)
                max_val = float(widget.get_attribute("max") or 5)
                # Clamp value to slider range
                clamped = max(min_val, min(float(value), max_val))
                self.driver.execute_script(
                    "arguments[0].value = arguments[1]; "
                    "arguments[0].dispatchEvent(new Event('input',{bubbles:true})); "
                    "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
                    widget, clamped
                )
                print(f"[Rating] Slider set to {clamped}")
                return True

            elif kind == "stars":
                idx = max(0, min(int(value) - 1, len(widget) - 1))
                self.scroll_to(widget[idx])
                self.safe_click(widget[idx])
                print(f"[Rating] Clicked star {value} of {len(widget)}")
                return True

            elif kind == "radio":
                for r in widget:
                    if r.get_attribute("value") == str(value):
                        self.scroll_to(r)
                        self.safe_click(r)
                        print(f"[Rating] Radio selected: {value}")
                        return True
                # Fallback: click nth radio
                idx = max(0, min(int(value) - 1, len(widget) - 1))
                self.scroll_to(widget[idx])
                self.safe_click(widget[idx])
                return True

            elif kind == "number":
                self.scroll_to(widget)
                self.safe_type(widget, str(value))
                print(f"[Rating] Numeric input set to {value}")
                return True

            elif kind == "custom":
                # Try to match by text first
                for item in widget:
                    if item.text.strip() == str(value):
                        self.scroll_to(item)
                        self.safe_click(item)
                        print(f"[Rating] Custom item clicked: {value}")
                        return True
                # Fallback: click nth item
                idx = max(0, min(int(value) - 1, len(widget) - 1))
                self.scroll_to(widget[idx])
                self.safe_click(widget[idx])
                return True

        except Exception as e:
            print(f"[Rating] Error setting rating: {e}")
            return False

        return False

    # ── file upload ───────────────────────────────────────────────────────────

    def fill_file_upload(self, identifier: str, file_path: str) -> bool:
        """
        Find a file input and upload the specified file.
        Supports:
        1. Native <input type='file'> — send_keys(absolute_path)
        2. Hidden file inputs behind a styled button — JS click + send_keys
        3. Drag-and-drop zones — JS DataTransfer simulation
        Auto-scrolls. Opens OS file dialog fallback if everything else fails.
        """
        abs_path = str(Path(file_path).expanduser().resolve())

        if not Path(abs_path).exists():
            # Try relative to Documents
            docs_path = Path.home() / "Documents" / file_path
            if docs_path.exists():
                abs_path = str(docs_path)
            else:
                print(f"[Upload] File not found: {file_path}")
                return False

        def _scan_file_input():
            # 1. By label/identifier
            if identifier:
                ident_l = identifier.lower()
                # label → input[type=file]
                try:
                    for lbl in self.driver.find_elements(By.XPATH, "//label"):
                        if not lbl.is_displayed() and not lbl.text:
                            continue
                        if ident_l in (lbl.text or "").lower() or ident_l in (lbl.get_attribute("for") or "").lower():
                            for_id = lbl.get_attribute("for")
                            if for_id:
                                try:
                                    inp = self.driver.find_element(By.ID, for_id)
                                    if inp.get_attribute("type") == "file":
                                        return inp
                                except Exception:
                                    pass
                except Exception:
                    pass

                # aria-label / name / id match
                try:
                    for inp in self.driver.find_elements(By.XPATH, "//input[@type='file']"):
                        for attr in ("aria-label", "name", "id", "data-testid"):
                            if ident_l in (inp.get_attribute(attr) or "").lower():
                                return inp
                except Exception:
                    pass

            # 2. Any visible file input
            try:
                for inp in self.driver.find_elements(By.XPATH, "//input[@type='file']"):
                    return inp  # return first one (visible or hidden)
            except Exception:
                pass

            return None

        # Scroll and find
        inp = _scan_file_input()
        if not inp:
            self._scroll_page_to(0)
            for y in self._scroll_positions():
                self._scroll_page_to(y)
                inp = _scan_file_input()
                if inp:
                    break

        if not inp:
            print(f"[Upload] No file input found for '{identifier}'")
            return False

        try:
            # Make hidden file inputs interactable
            self.driver.execute_script(
                "arguments[0].style.display='block';"
                "arguments[0].style.visibility='visible';"
                "arguments[0].style.opacity='1';"
                "arguments[0].style.width='1px';"
                "arguments[0].style.height='1px';",
                inp
            )
            inp.send_keys(abs_path)
            time.sleep(1.0)  # wait for upload to register
            print(f"[Upload] File uploaded: {abs_path}")
            return True

        except Exception as e:
            print(f"[Upload] send_keys failed: {e}, trying JS DataTransfer...")

        # Drag-and-drop zone fallback
        try:
            drop_zone_xpaths = [
                "//*[contains(@class,'drop') or contains(@class,'drag') "
                "or contains(@class,'upload-area') or contains(@class,'dropzone')]",
                "//*[@ondrop or @ondragover]",
            ]
            drop_zone = None
            for xp in drop_zone_xpaths:
                els = self.driver.find_elements(By.XPATH, xp)
                if els:
                    drop_zone = els[0]
                    break

            if drop_zone:
                js = """
                    var dt = new DataTransfer();
                    var file = new File([''], arguments[1].split('/').pop(), {type: 'application/octet-stream'});
                    dt.items.add(file);
                    var event = new DragEvent('drop', {bubbles: true, dataTransfer: dt});
                    arguments[0].dispatchEvent(event);
                """
                self.driver.execute_script(js, drop_zone, abs_path)
                time.sleep(1.0)
                print(f"[Upload] DataTransfer drop simulated on drop zone")
                return True
        except Exception as e:
            print(f"[Upload] DataTransfer fallback failed: {e}")

        return False

    # ── icon finder ───────────────────────────────────────────────────────────

    def find_icon(self, description: str, context: str = "", state: str = "") -> tuple:
        """
        Find any icon, toggle, SVG button, or visual-only element.

        Strategies (in order):
        1. Class name containing icon-{description} (styled-components pattern)
        2. aria-label / title / data-icon / tooltip containing description
        3. <i>, <svg>, <img> near context element
        4. role=button/img with matching class or aria
        5. FontAwesome / Material / custom icon class patterns
        6. Generic class scan for description keyword

        Returns (element, kind) where kind is "toggle" or "icon".
        Auto-scrolls entire page.
        """
        desc_l    = description.lower().strip()
        ctx_l     = context.lower().strip() if context else ""
        icon_slug = desc_l.replace(" ", "-")
        icon_slug2 = desc_l.replace(" ", "_")

        def _score(el):
            score = 0
            try:
                if not el.is_displayed():
                    return -999
                classes = (el.get_attribute("class") or "").lower()
                aria    = (el.get_attribute("aria-label") or "").lower()
                title   = (el.get_attribute("title") or "").lower()
                tip     = (el.get_attribute("data-tooltip") or
                           el.get_attribute("data-tip") or
                           el.get_attribute("data-original-title") or "").lower()
                icon_at = (el.get_attribute("data-icon") or "").lower()
                name_at = (el.get_attribute("name") or "").lower()

                # Exact class match: icon-dashboard
                if f"icon-{icon_slug}" in classes:     score += 15
                if f"icon_{icon_slug2}" in classes:    score += 15
                if f"icon-{icon_slug2}" in classes:    score += 14
                # Generic class contains description
                if icon_slug in classes:               score += 10
                if desc_l in classes:                  score += 10
                # Aria / title / tooltip
                if desc_l in aria:                     score += 12
                if desc_l in title:                    score += 11
                if desc_l in tip:                      score += 10
                if desc_l in icon_at:                  score += 10
                if desc_l in name_at:                  score += 8
                # Text content
                txt = (el.text or "").lower()
                if txt == desc_l:                      score += 8
                if desc_l in txt and len(txt) < 30:    score += 5

                # Context scoring — prefer elements near the context element
                if ctx_l:
                    try:
                        # Check if a nearby label/text matches context
                        nearby = self.driver.execute_script("""
                            var el = arguments[0];
                            var parent = el.parentElement;
                            for(var i=0;i<4;i++){
                                if(!parent) break;
                                if(parent.innerText && parent.innerText.length < 200)
                                    return parent.innerText.toLowerCase();
                                parent = parent.parentElement;
                            }
                            return '';
                        """, el)
                        if ctx_l in (nearby or ""):
                            score += 8
                    except Exception:
                        pass

                # Viewport bonus
                rect = self.driver.execute_script(
                    "var r=arguments[0].getBoundingClientRect();"
                    "return {top:r.top,left:r.left};", el
                )
                vw = self.driver.execute_script("return window.innerWidth;")
                vh = self.driver.execute_script("return window.innerHeight;")
                if 0 <= rect["top"] <= vh and 0 <= rect["left"] <= vw:
                    score += 3

            except Exception:
                return -999
            return score

        def _is_toggle(el):
            """Detect if element is a toggle switch."""
            try:
                classes = (el.get_attribute("class") or "").lower()
                role    = (el.get_attribute("role") or "").lower()
                type_   = (el.get_attribute("type") or "").lower()
                return (
                    role == "switch" or
                    type_ == "checkbox" or
                    "toggle" in classes or
                    "switch" in classes
                )
            except Exception:
                return False

        def _scan():
            candidates = []

            # 1. icon-{slug} class pattern (styled-components / BEM)
            for slug in (icon_slug, icon_slug2, desc_l):
                xpaths = [
                    f"//*[contains(@class,'icon-{slug}')]",
                    f"//*[contains(@class,'icon_{slug}')]",
                    f"//*[contains(@class,'{slug}')]",
                ]
                for xp in xpaths:
                    try:
                        candidates += self.driver.find_elements(By.XPATH, xp)
                    except Exception:
                        pass

            # 2. aria-label / title / data-icon / tooltip
            for attr in ("aria-label", "title", "data-icon",
                         "data-tooltip", "data-tip", "data-original-title", "name"):
                try:
                    candidates += self.driver.find_elements(
                        By.XPATH,
                        f"//*[contains(translate(@{attr},"
                        f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                        f"'{desc_l}')]"
                    )
                except Exception:
                    pass

            # 3. <i>, <svg>, <button>, <a>, <span> containing the slug in any attribute
            for tag in ("i", "svg", "button", "a", "span", "div"):
                try:
                    candidates += self.driver.find_elements(
                        By.XPATH,
                        f"//{tag}[contains(translate(@class,"
                        f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                        f"'{desc_l}')]"
                    )
                except Exception:
                    pass

            # 4. role=button / role=img with any matching content
            try:
                candidates += self.driver.find_elements(
                    By.XPATH,
                    f"//*[@role='button' or @role='img' or @role='switch']"
                    f"[contains(translate(@class,"
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                    f"'{desc_l}') or "
                    f"contains(translate(@aria-label,"
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                    f"'{desc_l}')]"
                )
            except Exception:
                pass

            # 5. Text content match for very short text icons
            try:
                candidates += self.driver.find_elements(
                    By.XPATH,
                    f"//*[normalize-space(text())='{desc_l}' or "
                    f"normalize-space(text())='{description}']"
                )
            except Exception:
                pass

            # Deduplicate and score
            seen = set()
            best_el, best_score = None, -1
            for el in candidates:
                try:
                    eid = self.driver.execute_script(
                        "return arguments[0].outerHTML.substring(0,80);", el
                    )
                    if eid in seen:
                        continue
                    seen.add(eid)
                    s = _score(el)
                    if s > best_score:
                        best_score = s
                        best_el    = el
                except Exception:
                    pass

            return best_el, best_score

        # Try current viewport first
        best_el, best_score = _scan()

        # Scroll and retry if not confident
        if not best_el or best_score < 5:
            self._scroll_page_to(0)
            for y in self._scroll_positions():
                self._scroll_page_to(y)
                el, score = _scan()
                if el and score > best_score:
                    best_el    = el
                    best_score = score
                if best_score >= 10:
                    break

        if not best_el:
            return None, "icon"

        kind = "toggle" if _is_toggle(best_el) else "icon"
        return best_el, kind

    def interact_icon(self, description: str, context: str = "", state: str = "") -> bool:
        """
        Find and interact with an icon/toggle.
        - Icons: just click
        - Toggles: check current state, flip if needed (or force ON/OFF)
        """
        el, kind = self.find_icon(description, context, state)

        if not el:
            print(f"[Icon] Could not find: '{description}'"
                  f"{' near ' + context if context else ''}")
            return False

        self.scroll_to(el)

        if kind == "toggle" and state.upper() in ("ON", "OFF"):
            # Check current state
            try:
                is_on = (
                    el.is_selected() or
                    el.get_attribute("aria-checked") == "true" or
                    "active" in (el.get_attribute("class") or "").lower() or
                    "checked" in (el.get_attribute("class") or "").lower()
                )
                should_be_on = state.upper() == "ON"
                if is_on == should_be_on:
                    print(f"[Icon] Toggle '{description}' already {state}")
                    return True
            except Exception:
                pass

        ok = self.safe_click(el)
        time.sleep(0.5)
        print(f"[Icon] {kind.capitalize()} '{description}' clicked"
              f"{' → ' + state if state else ''}")
        return ok

    # ── internal utils ────────────────────────────────────────────────────────

    def _collect_by_xpaths(self, xpaths: list) -> list:
        elements = []
        for xp in xpaths:
            try:
                elements.extend(self.driver.find_elements(By.XPATH, xp))
            except Exception:
                pass
        return elements

    def _best_candidate(self, candidates: list, identifier: str):
        best_el, best_score = None, -1
        seen = set()
        for el in candidates:
            try:
                eid = id(el)
                if eid in seen:
                    continue
                seen.add(eid)
                score = self._score_element(el, identifier)
                if score > best_score:
                    best_score = score
                    best_el    = el
            except Exception:
                pass
        return best_el, best_score


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN AUTOMATION ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def automate_from_config(config_path) -> tuple:
    """
    Execute a TestSphere config file.
    Returns (executed_results, overall_judgment, screenshot_path).
    """
    ts_fmt = lambda: datetime.now().strftime("%d/%m/%Y, %H:%M")

    # ── Load actions ──────────────────────────────────────────────────────────
    actions = []
    with open(config_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                actions.append(line)

    # Link: is not a counted step
    total_steps = len([a for a in actions if not a.lower().startswith("link:")])

    website_link = next(
        (a.split(":", 1)[1].strip() for a in actions if a.lower().startswith("link:")),
        None
    )
    if not website_link:
        raise ValueError("No 'Link:' found in config. Add 'Link: https://...' as the first line.")

    # ── Start browser ─────────────────────────────────────────────────────────
    opts = webdriver.ChromeOptions()
    opts.add_argument("--start-maximized")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument(f"--user-data-dir={tempfile.mkdtemp()}")

    from selenium.webdriver.chrome.service import Service as ChromeService
    from webdriver_manager.chrome import ChromeDriverManager
    driver = webdriver.Chrome(
        service=ChromeService(ChromeDriverManager().install()), options=opts
    )

    driver.get(website_link)
    WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )

    finder          = ElementFinder(driver)
    executed_results = []
    step_num         = 0

    def parse_args(raw: str) -> list:
        parts = re.split(r',\s*(?=(?:[^"]*"[^"]*")*[^"]*$)', raw)
        return [p.strip().strip('"') for p in parts]

    def log_result(judgment: dict):
        judgment["step"] = step_num
        executed_results.append(judgment)

    def make_pass(action, expected, actual, reason):
        return {
            "action": action, "expected": expected,
            "actual": actual, "result": "PASS",
            "reason": reason,
            "confidence": 1.0,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def make_fail(action, expected, actual, reason):
        # Safe screenshot — works with both old and new vision.py
        try:
            if hasattr(vision, "_capture_screenshot"):
                shot = vision._capture_screenshot(driver, action)
            elif hasattr(vision, "_capture_failure_screenshot"):
                shot = vision._capture_failure_screenshot(driver, action)
            else:
                shot = None
        except Exception:
            shot = None
        return {
            "action": action, "expected": expected,
            "actual": actual, "result": "FAIL",
            "reason": reason,
            "confidence": 0.0,
            "screenshot": shot,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ── Execute each action ───────────────────────────────────────────────────
    for action in actions:
        norm = action.strip().lower()

        # Skip link lines
        if norm.startswith("link:"):
            continue

        step_num += 1
        print(f"\n[Step {step_num}] {action}")
        judgment = {}

        # ── LOGIN ─────────────────────────────────────────────────────────────
        if norm.startswith("login:"):
            try:
                args = parse_args(action.split(":", 1)[1])
                if len(args) != 2:
                    raise ValueError()
                field, value = args

                # Use smart login field finder — tries identifier + common
                # email/username/password variations automatically
                inp = finder.find_login_field(field)
                if inp:
                    finder.scroll_to(inp)
                    ok = finder.safe_type(inp, value)
                    time.sleep(0.5)
                    judgment = vision.judge_step("login", field, value, driver)
                else:
                    judgment = make_fail("Login", value, "NOT FOUND",
                                        f"Could not find login field '{field}' "
                                        f"or any common email/username/password field.")
            except Exception as e:
                judgment = make_fail("Login", "N/A", "ERROR", str(e))

        # ── TEXT ──────────────────────────────────────────────────────────────
        elif norm.startswith("text:"):
            try:
                raw   = action.split(":", 1)[1]
                field, value = raw.split(",", 1)
                field = field.strip()
                value = value.strip().strip('"')

                inp = finder.find_input(field)
                if inp:
                    finder.scroll_to(inp)
                    finder.safe_type(inp, value)
                    time.sleep(0.8)

                    # Handle autocomplete suggestions
                    try:
                        sugg_xp = (
                            "//ul[contains(@class,'autocomplete') or "
                            "contains(@class,'suggestion') or "
                            "contains(@class,'dropdown-menu')]//li"
                        )
                        suggestions = driver.find_elements(By.XPATH, sugg_xp)
                        for s in suggestions:
                            if s.is_displayed():
                                finder.safe_click(s)
                                print(f"[Text] Clicked suggestion: {s.text.strip()}")
                                break
                    except Exception:
                        pass

                    judgment = vision.judge_step("text", field, value, driver)
                else:
                    judgment = make_fail("Text", value, "NOT FOUND",
                                        f"Input field '{field}' not found after full scroll.")
            except Exception as e:
                judgment = make_fail("Text", "N/A", "ERROR", str(e))

        # ── CLICK ─────────────────────────────────────────────────────────────
        elif norm.startswith("click:"):
            label = action.split(":", 1)[1].strip().strip('"')
            el    = finder.find_clickable(label)
            if el:
                ok = finder.safe_click(el)
                time.sleep(0.8)
                judgment = vision.judge_step(
                    "click", label, "Clicked" if ok else "Click failed", driver
                )
            else:
                judgment = make_fail("Click", "Clicked", "NOT FOUND",
                                     f"No element found with text/label '{label}' after full scroll.")

        # ── BUTTON (alias for click with button-specific scoring) ─────────────
        elif norm.startswith("button:"):
            label = action.split(":", 1)[1].strip().strip('"')
            el    = finder.find_clickable(label)
            if el:
                ok = finder.safe_click(el)
                time.sleep(0.8)
                judgment = vision.judge_step("button", label, "Clicked", driver)
            else:
                judgment = make_fail("Button", "Clicked", "NOT FOUND",
                                     f"Button '{label}' not found after full scroll.")

        # ── DROPDOWN ──────────────────────────────────────────────────────────
        elif norm.startswith("dropdown:"):
            try:
                args = parse_args(action.split(":", 1)[1])
                if len(args) != 2:
                    raise ValueError()
                field, value = args
                ok = finder.select_custom_dropdown(field, value)
                time.sleep(0.8)
                if ok:
                    judgment = vision.judge_step("dropdown", field, value, driver)
                else:
                    judgment = make_fail("Dropdown", value, "NOT FOUND",
                                        f"Dropdown '{field}' or option '{value}' not found.")
            except Exception as e:
                judgment = make_fail("Dropdown", "N/A", "ERROR", str(e))

        # ── CALENDAR ─────────────────────────────────────────────────────────
        elif norm.startswith("calendar:"):
            try:
                args      = parse_args(action.split(":", 1)[1])
                field     = args[0]
                date_str  = args[1] if len(args) > 1 else ""
                ok        = finder.fill_calendar(field, date_str)
                time.sleep(0.8)
                if ok:
                    judgment = vision.judge_step("calendar", field, date_str, driver)
                else:
                    judgment = make_fail("Calendar", date_str, "FAILED",
                                        f"Could not fill calendar '{field}' with '{date_str}'.")
            except Exception as e:
                judgment = make_fail("Calendar", "N/A", "ERROR", str(e))

        # ── CHECKBOX ─────────────────────────────────────────────────────────
        elif norm.startswith("checkbox:"):
            try:
                args  = parse_args(action.split(":", 1)[1])
                label = args[0]
                state = args[1].upper() if len(args) > 1 else "ON"
                # Optional 3rd arg = index (0-based) for duplicate labels
                idx   = int(args[2]) - 1 if len(args) > 2 else 0
                idx   = max(0, idx)

                cb = finder.find_checkbox(label, idx)
                if cb:
                    finder.scroll_to(cb)
                    try:
                        is_checked = cb.is_selected()
                    except Exception:
                        is_checked = (
                            cb.get_attribute("aria-checked") == "true"
                            or "checked" in (cb.get_attribute("class") or "")
                        )

                    should_check = state == "ON"
                    if is_checked != should_check:
                        finder.safe_click(cb)
                        time.sleep(0.4)
                        print(f"[Checkbox] '{label}' set to {state}")
                    else:
                        print(f"[Checkbox] '{label}' already {state}")

                    judgment = vision.judge_step("checkbox", label, state, driver)
                else:
                    judgment = make_fail("Checkbox", state, "NOT FOUND",
                                        f"Checkbox '{label}' (index {idx+1}) not found.")
            except Exception as e:
                judgment = make_fail("Checkbox", "N/A", "ERROR", str(e))

        # ── ICON / TOGGLE ────────────────────────────────────────────────────
        elif norm.startswith("icon:"):
            try:
                raw  = action.split(":", 1)[1].strip()
                args = parse_args(raw)
                description = args[0].strip() if len(args) > 0 else ""
                context     = args[1].strip() if len(args) > 1 else ""
                state       = args[2].strip().upper() if len(args) > 2 else ""

                ok = finder.interact_icon(description, context, state)
                time.sleep(0.5)
                if ok:
                    judgment = vision.judge_step(
                        "icon", description,
                        f"Clicked{' → ' + state if state else ''}",
                        driver
                    )
                else:
                    judgment = make_fail(
                        "Icon", description, "NOT FOUND",
                        f"Could not find icon '{description}'"
                        f"{' near ' + context if context else ''} after full scroll."
                    )
            except Exception as e:
                judgment = make_fail("Icon", "N/A", "ERROR", str(e))

        # ── RATING ───────────────────────────────────────────────────────────
        elif norm.startswith("rating:"):
            try:
                raw  = action.split(":", 1)[1].strip()
                args = parse_args(raw)
                if len(args) == 2:
                    field = args[0].strip()
                    val   = int(float(args[1].strip()))
                elif len(args) == 1:
                    field = ""
                    val   = int(float(args[0].strip()))
                else:
                    raise ValueError("rating: requires 1 or 2 arguments")

                ok = finder.fill_rating(field, val)
                time.sleep(0.6)
                if ok:
                    judgment = vision.judge_step("rating", field or "rating widget", str(val), driver)
                else:
                    judgment = make_fail("Rating", str(val), "NOT FOUND",
                                        f"No rating widget found for '{field}' after full scroll.")
            except Exception as e:
                judgment = make_fail("Rating", "N/A", "ERROR", str(e))

        # ── UPLOAD ───────────────────────────────────────────────────────────
        elif norm.startswith("upload:"):
            try:
                raw  = action.split(":", 1)[1].strip()
                args = parse_args(raw)
                if len(args) < 2:
                    raise ValueError("upload: requires 2 arguments — field name and file path")
                field     = args[0].strip()
                file_path = args[1].strip().strip('"')

                ok = finder.fill_file_upload(field, file_path)
                time.sleep(1.0)
                if ok:
                    judgment = vision.judge_step("upload", field, file_path, driver)
                else:
                    judgment = make_fail("Upload", file_path, "NOT FOUND",
                                        f"Could not find file input '{field}' or file '{file_path}'.")
            except Exception as e:
                judgment = make_fail("Upload", "N/A", "ERROR", str(e))

        # ── SLEEP ────────────────────────────────────────────────────────────
        elif norm.startswith("sleep:"):
            try:
                secs = float(action.split(":", 1)[1].strip())
                print(f"[Sleep] {secs}s")
                time.sleep(secs)
                judgment = make_pass("Sleep", f"{secs}s", f"Slept {secs}s",
                                     "Sleep completed.")
            except ValueError:
                judgment = make_pass("Sleep", "?", "Skipped", "Invalid sleep value.")

        # ── UNKNOWN ───────────────────────────────────────────────────────────
        else:
            print(f"[Step {step_num}] Unknown command: {action}")
            judgment = make_pass("Unknown", action, "Skipped",
                                 f"Command '{action}' not recognised — skipped.")

        # Log and check for failure
        if judgment:
            log_result(judgment)
            if judgment.get("result") == "FAIL":
                print(f"[Step {step_num}] FAILED — stopping run.")
                break

    # ── Final screenshot + quit ───────────────────────────────────────────────
    # Safe final screenshot
    try:
        if hasattr(vision, "_capture_screenshot"):
            screenshot_path = vision._capture_screenshot(driver, "final")
        elif hasattr(vision, "_capture_failure_screenshot"):
            screenshot_path = vision._capture_failure_screenshot(driver, "final")
        else:
            screenshot_path = None
    except Exception:
        screenshot_path = None

    overall = vision.judge_run(executed_results, total_steps)
    overall["total_steps"] = total_steps

    try:
        driver.quit()
    except Exception:
        pass

    return executed_results, overall, screenshot_path
