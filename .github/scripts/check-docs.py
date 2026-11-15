"""Select the documentation-only CI path and check its static links."""
from html.parser import HTMLParser
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "docs"
MARKDOWN = {
    "ATTRIBUTION.md", "CHANGELOG.md", "INSTALL.md", "PROVIDERS.md",
    "README.md", "REPRODUCE.md", "START.md",
}
PAGES_HOST = "dalto-ai-public.github.io"
PAGES_PATH = "/openweb-benchmark-news/"


def docs_only(base):
    if not re.fullmatch(r"[0-9a-f]{40}", base) or base == "0" * 40:
        return False
    diff = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", base, "HEAD", "--"],
        cwd=ROOT, capture_output=True, check=False,
    )
    if diff.returncode:
        return False
    paths = diff.stdout.rstrip(b"\0").split(b"\0")
    markdown_paths = {name.encode() for name in MARKDOWN}
    return all(path in markdown_paths or path.startswith(b"docs/") for path in paths)


class Page(HTMLParser):
    def __init__(self, path):
        super().__init__()
        self.tags = set()
        self.ids = set()
        self.urls = []
        self.feed(path.read_text(encoding="utf-8"))
        self.close()
        if not {"html", "head", "title", "body"} <= self.tags:
            raise ValueError(f"{path.relative_to(ROOT)} has an incomplete HTML document")

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        attrs = dict(attrs)
        if fragment := attrs.get("id"):
            if fragment in self.ids:
                raise ValueError(f"Duplicate HTML id {fragment}")
            self.ids.add(fragment)
        self.urls.extend(attrs[key] for key in ("href", "src") if attrs.get(key))


def check_link(source, value, pages):
    url = urlsplit(value)
    site_link = source in pages
    if url.hostname == PAGES_HOST and url.path.startswith(PAGES_PATH):
        target = SITE / unquote(url.path[len(PAGES_PATH):])
        site_link = True
    elif url.scheme or url.netloc:
        if url.scheme not in {"https", "http", "mailto"}:
            raise ValueError(f"{source.relative_to(ROOT)} has an unsupported link: {value}")
        return
    elif url.path:
        target = source.parent / unquote(url.path)
    else:
        target = source
    target = target.resolve()
    root = SITE if site_link else ROOT
    if not target.is_relative_to(root) or not target.exists():
        raise ValueError(f"{source.relative_to(ROOT)} links to a missing or invalid target: {value}")
    if site_link and target.is_dir():
        target = target / "index.html"
        if target not in pages:
            raise ValueError(f"{source.relative_to(ROOT)} links to a directory without an index: {value}")
    if (target in pages and url.fragment and unquote(url.fragment) not in pages[target].ids):
        raise ValueError(f"{source.relative_to(ROOT)} links to a missing HTML anchor: {value}")


def check_docs():
    pages = {path: Page(path) for path in sorted(SITE.rglob("*.html"))}
    if SITE / "index.html" not in pages:
        raise ValueError("Missing GitHub Pages index")
    for source, page in pages.items():
        for link in page.urls:
            check_link(source, link, pages)
    for name in sorted(MARKDOWN):
        source = ROOT / name
        for link in re.findall(r"\]\(([^\s()]+)\)", source.read_text(encoding="utf-8")):
            check_link(source, link, pages)
    print(f"Checked HTML and local links in {len(pages)} pages and {len(MARKDOWN)} Markdown files")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(f"docs_only={str(docs_only(sys.argv[1])).lower()}")
    else:
        check_docs()
