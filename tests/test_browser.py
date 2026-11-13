"""Tests de l'interface : cohérence statique HTML/JS, puis parcours réel dans un navigateur (Playwright)."""
from __future__ import annotations

import re
import threading
from html.parser import HTMLParser
from pathlib import Path

import pytest

from upscale import settings as st
from upscale.web import app

STATIC = Path(__file__).resolve().parent.parent / "static"
VOID = {"input", "br", "img", "meta", "link", "hr", "source", "use", "path", "circle", "rect", "line", "polyline"}


def test_every_element_used_by_the_script_exists():
    """Chaque $("id") du script désigne un élément de la page (ou créé par le script lui-même)."""
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    used = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', script))
    present = set(re.findall(r'id="([A-Za-z0-9_-]+)"', page)) | set(re.findall(r'id=\\?"([A-Za-z0-9_-]+)\\?"', script))
    assert not used - present, f"éléments absents de index.html : {sorted(used - present)}"
    duplicated = [i for i in present if len(re.findall(rf'id="{re.escape(i)}"', page)) > 1]
    assert not duplicated, f"identifiants en double : {duplicated}"


def test_page_markup_is_balanced():
    class Checker(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.stack: list[str] = []
            self.errors: list[tuple] = []

        def handle_starttag(self, tag, attrs):
            if tag not in VOID:
                self.stack.append(tag)

        def handle_startendtag(self, tag, attrs):
            pass

        def handle_endtag(self, tag):
            if tag in VOID:
                return
            if self.stack and self.stack[-1] == tag:
                self.stack.pop()
            else:
                self.errors.append((tag, self.getpos(), self.stack[-3:]))

    checker = Checker()
    checker.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    assert not checker.errors and not checker.stack, (checker.errors[:3], checker.stack)


@pytest.fixture()
def live(env):
    from werkzeug.serving import make_server

    st.save_settings({"preset": "medium"})  # « ultrafast » (tests) n'est pas proposé dans l'interface
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture()
def page(live):
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except sync_api.Error as exc:  # navigateur non installé (« playwright install chromium »)
            pytest.skip(f"Chromium indisponible : {str(exc).splitlines()[0]}")
        tab = browser.new_page(viewport={"width": 1400, "height": 900})
        problems: list[str] = []
        tab.on("pageerror", lambda error: problems.append(f"erreur : {error}"))
        tab.on("console", lambda message: problems.append(f"console : {message.text}")
               if message.type in {"error", "warning"} and "Error" in message.text else None)
        tab.goto(live)
        tab.wait_for_function("document.querySelectorAll('#model option').length >= 3")
        tab.problems = problems
        yield tab
        browser.close()


def test_interface_loads_and_saves_settings(page):
    """Chargement sans erreur JavaScript, chaque vue s'affiche, les réglages s'enregistrent."""
    for view in ("library", "videos", "queue", "preview", "models", "settings", "automation", "dashboard"):
        page.click(f'a[data-view="{view}"]')
        page.wait_for_timeout(150)
    page.click('a[data-view="models"]')
    assert page.locator("#modelsBody tr").count() >= 1          # tableau des modèles rendu
    assert page.locator("#modelSuggestions .suggestion").count() >= 1
    page.click('a[data-view="settings"]')
    page.select_option("#deblock", "strong")
    page.select_option("#codec", "av1_vaapi")
    page.click("#addProfile")
    headers = page.evaluate("document.querySelector('#profilesBody').closest('table').querySelectorAll('thead th').length")
    assert page.locator("#profilesBody tr").first.locator("td").count() == headers
    page.locator("#profilesBody .p-deblock").first.select_option("light")
    with page.expect_response(lambda r: r.url.endswith("/api/config") and r.request.method == "POST") as saved:
        page.click("#saveSettings")
    assert saved.value.status == 200, saved.value.text()
    stored = st.get_settings()
    assert stored["deblock"] == "strong" and stored["codec"] == "av1_vaapi"
    assert stored["profiles"][0]["deblock"] == "light"
    page.reload()
    page.wait_for_function("document.querySelector('#deblock').value === 'strong'")
    assert not page.problems, page.problems
