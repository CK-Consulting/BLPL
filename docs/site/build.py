"""Build the GitHub Pages site from docs/user-guide.md.

The guide stays a Markdown file in docs/, reviewed and diffed like every other
page; this script only renders it. Run from the repository root:

    python docs/site/build.py [--out _site]

Links to other docs/ pages point at the repository on GitHub rather than being
rendered too, so the published site is one page and cannot drift from a second
copy of anything.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import re
import subprocess
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "docs" / "user-guide.md"
TEMPLATE = Path(__file__).with_name("template.html")
REPO = os.environ.get("GITHUB_REPOSITORY", "CK-Consulting/BLPL")
BLOB = f"https://github.com/{REPO}/blob/main"


def _commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _rewrite_links(body: str) -> str:
    """Point relative links at the file on GitHub; leave in-page anchors alone."""

    def repl(m: re.Match[str]) -> str:
        href = m.group(1)
        if href.startswith(("#", "http://", "https://", "mailto:")):
            return m.group(0)
        path, _, frag = href.partition("#")
        target = (SOURCE.parent / path).resolve().relative_to(ROOT).as_posix()
        return f'href="{BLOB}/{target}{"#" + frag if frag else ""}"'

    return re.sub(r'href="([^"]+)"', repl, body)


def _mermaid(body: str) -> str:
    # fenced_code writes <pre><code class="language-mermaid">; mermaid.js wants
    # <pre class="mermaid"> holding the source as text.
    return re.sub(
        r'<pre><code class="language-mermaid">(.*?)</code></pre>',
        lambda m: f'<pre class="mermaid">{m.group(1)}</pre>',
        body,
        flags=re.S,
    )


def _nav(tokens: list[dict]) -> str:
    items = []
    for t in tokens:
        for h2 in t.get("children", []):
            if h2["id"] == "contents":  # the sidebar is the contents
                continue
            items.append(
                f'<li><a href="#{h2["id"]}">{html.escape(h2["name"], quote=False)}</a></li>'
            )
    return "\n".join(items)


def build(out: Path) -> Path:
    md = markdown.Markdown(
        extensions=["tables", "fenced_code", "toc", "sane_lists"],
        extension_configs={"toc": {"permalink": False}},
    )
    body = md.convert(SOURCE.read_text(encoding="utf-8"))
    body = _mermaid(_rewrite_links(body))
    page = (
        TEMPLATE.read_text(encoding="utf-8")
        .replace("{{nav}}", _nav(md.toc_tokens))
        .replace("{{body}}", body)
        .replace("{{commit}}", _commit())
        .replace("{{built}}", dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"))
        .replace("{{source}}", f"{BLOB}/docs/user-guide.md")
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    # Serve the files as written; no Jekyll pass over them.
    (out / ".nojekyll").write_text("", encoding="utf-8")
    return out / "index.html"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "_site")
    print(build(ap.parse_args().out))
