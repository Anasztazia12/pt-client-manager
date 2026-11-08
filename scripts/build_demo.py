"""Rebuild the static GitHub Pages demo in docs/ from the real templates.

Run from the project root:  python scripts/build_demo.py

It starts the app with sample data on a throwaway database, follows the links from the overview
and saves each page as a flat HTML file. Replaces the HTML files in docs/. Forms show a
"nothing is saved" message, and links to pages left out (far-off dates, deep library pages) are off.
"""
import html
import os
import re
import shutil
import sys
import tempfile
from collections import deque
from datetime import date, timedelta
from urllib.parse import parse_qsl, urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
sys.path.insert(0, ROOT)

work_dir = tempfile.mkdtemp()
os.environ.update(
    DATABASE_URL="sqlite:///" + os.path.join(work_dir, "demo.db").replace("\\", "/"),
    DEMO_MODE="1",
    STRIPE_SECRET_KEY="sk_test_static_demo",  # never used: no form is submitted
    STRIPE_CURRENCY="gbp",
)

from app import app  # noqa: E402

BANNER = '<div class="demo-bar">Demo with sample data. Click around freely; changes are not saved.</div>'

DEMO_CSS = """
/* static demo */
.demo-toast { position: fixed; left: 50%; bottom: 24px; transform: translate(-50%, 20px); padding: 9px 14px; border-radius: 7px; background: var(--ink); color: #fff; opacity: 0; transition: opacity .2s, transform .2s; z-index: 50; pointer-events: none; }
.demo-toast.show { opacity: 1; transform: translate(-50%, 0); }
a[data-demo-off] { cursor: default; }
"""

DEMO_SCRIPT = """<script>
(function () {
  function toast(text) {
    var t = document.querySelector('.demo-toast');
    if (!t) { t = document.createElement('div'); t.className = 'demo-toast'; t.setAttribute('role', 'status'); document.body.appendChild(t); }
    t.textContent = text;
    t.classList.add('show');
    clearTimeout(t._h); t._h = setTimeout(function () { t.classList.remove('show'); }, 2600);
  }
  document.addEventListener('submit', function (e) {
    if (e.target.id === 'calcForm') return;
    e.preventDefault();
    e.stopImmediatePropagation();
    toast('This is a demo, so nothing is saved.');
  }, true);
  document.addEventListener('click', function (e) {
    if (e.target.closest('a[data-demo-off]')) { e.preventDefault(); toast('This page is not part of the demo.'); }
  });
})();
</script>"""


def is_included(url):
    """Which pages make the demo: everything except far-off dates and deep library pages."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    today = date.today()
    if parts.path.startswith("/exercises") and parts.path != "/exercises/new":
        if "q" in query or ("page" in query and (len(query) > 1 or int(query["page"]) > 3)):
            return False
        return not ("type" in query and "muscle" in query)
    day_match = re.fullmatch(r"/day/(\d{4}-\d\d-\d\d)", parts.path)
    day = day_match.group(1) if day_match else query.get("day")
    if day:
        return today - timedelta(days=14) <= date.fromisoformat(day) <= today + timedelta(days=7)
    return True


def file_name(url):
    parts = urlsplit(url)
    if parts.path == "/overview":
        return "index.html"
    if parts.path == "/":
        return "clients.html"
    pieces = [parts.path.strip("/")] + [f"{k}-{v}" for k, v in parse_qsl(parts.query)]
    return re.sub(r"[^a-z0-9]+", "-", "-".join(pieces).lower()).strip("-") + ".html"


def main():
    app.config["WTF_CSRF_ENABLED"] = False
    http = app.test_client()
    pages, queue, seen = {}, deque(["/overview", "/"]), {"/overview", "/"}
    link = re.compile(r'href="(/[^"#]*)')
    while queue:
        url = queue.popleft()
        response = http.get(url)
        if response.status_code != 200:
            raise SystemExit(f"{url} returned {response.status_code}")
        page = response.get_data(as_text=True)
        pages[url] = page
        for match in link.findall(page):
            target = html.unescape(match)
            if target.startswith("/static/") or target in seen or not is_included(target):
                continue
            seen.add(target)
            queue.append(target)

    def rewrite(match):
        target = html.unescape(match.group(1))
        fragment = match.group(2) or ""
        if target.startswith("/static/"):
            return f'href="{target[1:]}{fragment}"'
        if target in pages:
            return f'href="{file_name(target)}{fragment}"'
        return 'href="#" data-demo-off'

    for name in os.listdir(DOCS):
        if name.endswith(".html"):
            os.remove(os.path.join(DOCS, name))
    for url, page in pages.items():
        page = re.sub(r'href="(/[^"#]*)(#[^"]*)?"', rewrite, page)
        page = page.replace('src="/static/', 'src="static/')
        page = re.sub(r'<div class="demo-bar">.*?</div>', BANNER, page, count=1, flags=re.S)
        page = page.replace("</body>", DEMO_SCRIPT + "</body>")
        with open(os.path.join(DOCS, file_name(url)), "w", encoding="utf-8", newline="\n") as out:
            out.write(page)

    os.makedirs(os.path.join(DOCS, "static"), exist_ok=True)
    with open(os.path.join(ROOT, "static", "style.css"), encoding="utf-8") as source:
        css = source.read()
    with open(os.path.join(DOCS, "static", "style.css"), "w", encoding="utf-8", newline="\n") as out:
        out.write(css + DEMO_CSS)
    shutil.copytree(os.path.join(ROOT, "static", "avatars"), os.path.join(DOCS, "static", "avatars"), dirs_exist_ok=True)
    print(f"Wrote {len(pages)} pages to docs/")


if __name__ == "__main__":
    try:
        main()
    finally:
        with app.app_context():
            from models import db
            db.engine.dispose()
        shutil.rmtree(work_dir, ignore_errors=True)
