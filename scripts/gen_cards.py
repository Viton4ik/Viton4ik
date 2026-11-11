#!/usr/bin/env python3
"""
Generate the profile's statistics cards as static SVG files.

Everything this script needs comes from the public GitHub REST API and the
Python standard library. No third-party packages, no external rendering
service: the cards are plain SVG files committed into this repository, so the
profile page can never show a broken image because somebody else's free server
went down.

Usage:
    python3 scripts/gen_cards.py [--config scripts/config.json] [--out assets]

A GitHub token in GH_TOKEN or GITHUB_TOKEN is optional but strongly
recommended: without one the API allows only 60 requests per hour.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API = "https://api.github.com"
UA = "Viton4ik-profile-cards/1.0"

# --------------------------------------------------------------------------
# Theme
#
# Categorical hues are used in a fixed order and never cycled; the dark column
# is the same hues re-stepped for a dark surface rather than a separate
# palette. Every bar is directly labelled with its language name and share, so
# colour is never the only way to tell two rows apart.
# --------------------------------------------------------------------------

THEMES = {
    "light": {
        "surface": "#fcfcfb",
        "border": "#e3e2df",
        "track": "#ebeae7",
        "primary": "#0b0b0b",
        "secondary": "#52514e",
        "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                   "#e87ba4", "#008300", "#6250d6", "#e34948"],
        "other": "#8a8982",
    },
    "dark": {
        "surface": "#1a1a19",
        "border": "#2f2f2d",
        "track": "#2a2a28",
        "primary": "#f0efec",
        "secondary": "#c3c2b7",
        "series": ["#3987e5", "#d95926", "#199e70", "#c98500",
                   "#d55181", "#008300", "#9085e9", "#e66767"],
        "other": "#7c7b74",
    },
}

FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,"
        "Arial,sans-serif")

CARD_W = 460


# --------------------------------------------------------------------------
# API access
# --------------------------------------------------------------------------

def _token() -> str | None:
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value
    return None


def api_get(path: str, *, retries: int = 3) -> Any:
    """GET a GitHub API path, following nothing and retrying transient errors."""
    url = path if path.startswith("http") else f"{API}{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": UA,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    last_error: Exception | None = None
    for attempt in range(retries):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            # 403 here is almost always the rate limit, which a retry after a
            # short pause will not fix, so fail loudly rather than silently
            # producing a card full of zeroes.
            if error.code in (403, 429):
                remaining = error.headers.get("X-RateLimit-Remaining")
                raise SystemExit(
                    f"GitHub API refused {url} with HTTP {error.code} "
                    f"(rate limit remaining: {remaining}). "
                    "Set GH_TOKEN to raise the limit."
                ) from error
            if error.code == 404:
                raise SystemExit(f"GitHub API returned 404 for {url}") from error
            last_error = error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))

    raise SystemExit(f"GitHub API request failed for {url}: {last_error}")


def api_paginate(path: str, *, per_page: int = 100, max_pages: int = 10) -> list:
    """Collect every page of a list endpoint."""
    joiner = "&" if "?" in path else "?"
    items: list = []
    for page in range(1, max_pages + 1):
        batch = api_get(f"{path}{joiner}per_page={per_page}&page={page}")
        if not isinstance(batch, list) or not batch:
            break
        items.extend(batch)
        if len(batch) < per_page:
            break
    return items


# --------------------------------------------------------------------------
# Data collection
# --------------------------------------------------------------------------

def collect(config: dict) -> dict:
    username = config["username"]
    excluded_repos = {name.lower() for name in config.get("exclude_repos", [])}
    excluded_langs = {name.lower() for name in config.get("exclude_languages", [])}

    user = api_get(f"/users/{username}")
    repos = api_paginate(f"/users/{username}/repos?type=owner&sort=pushed")

    own = [r for r in repos if not r.get("fork") and not r.get("private")]

    language_bytes: dict[str, int] = {}
    counted_repos = 0
    for repo in own:
        if repo["name"].lower() in excluded_repos:
            continue
        counted_repos += 1
        languages = api_get(repo["languages_url"])
        if not isinstance(languages, dict):
            continue
        for language, size in languages.items():
            if language.lower() in excluded_langs:
                continue
            language_bytes[language] = language_bytes.get(language, 0) + int(size)

    created = dt.datetime.strptime(
        user["created_at"], "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=dt.timezone.utc)
    years = (dt.datetime.now(dt.timezone.utc) - created).days / 365.25

    skip_recent = {name.lower() for name in config.get("recent_skip", [])}
    recent = [
        {
            "name": r["name"],
            "url": r["html_url"],
            "description": (r.get("description") or "").strip(),
            "language": r.get("language"),
            "stars": r.get("stargazers_count", 0),
            "pushed_at": r["pushed_at"][:10],
            "homepage": (r.get("homepage") or "").strip(),
        }
        for r in own
        if r["name"].lower() not in skip_recent
    ]
    recent.sort(key=lambda r: r["pushed_at"], reverse=True)

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
        "user": {
            "login": user["login"],
            "name": user.get("name") or user["login"],
            "followers": user.get("followers", 0),
        },
        "totals": {
            "public_repos": len(own),
            "stars": sum(r.get("stargazers_count", 0) for r in own),
            "forks": sum(r.get("forks_count", 0) for r in own),
            "followers": user.get("followers", 0),
            "languages": len(language_bytes),
            "years": years,
            "repos_counted_for_languages": counted_repos,
        },
        "language_bytes": language_bytes,
        "recent": recent,
    }


# --------------------------------------------------------------------------
# SVG helpers
# --------------------------------------------------------------------------

def xml_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def text_node(x: float, y: float, content: str, *, size: float, fill: str,
              weight: str = "400", anchor: str = "start") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" '
        f'font-size="{size}" font-weight="{weight}" fill="{fill}" '
        f'text-anchor="{anchor}">{xml_escape(content)}</text>'
    )


def card_open(width: int, height: int, theme: dict, title: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{xml_escape(title)}">',
        f'<title>{xml_escape(title)}</title>',
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" '
        f'rx="10" fill="{theme["surface"]}" stroke="{theme["border"]}"/>',
    ]


def compact(number: int) -> str:
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M".replace(".0M", "M")
    if number >= 1_000:
        return f"{number / 1_000:.1f}k".replace(".0k", "k")
    return str(number)


# --------------------------------------------------------------------------
# Card: overview
# --------------------------------------------------------------------------

TILE_LABELS = {
    "public_repos": "public repos",
    "stars": "stars earned",
    "forks": "forks",
    "followers": "followers",
    "languages": "languages",
    "years": "years on GitHub",
}


def render_overview(data: dict, mode: str, tile_keys: list[str]) -> str:
    theme = THEMES[mode]
    totals = data["totals"]

    tiles = []
    for key in tile_keys:
        if key not in TILE_LABELS or key not in totals:
            continue
        value = (f'{totals[key]:.1f}' if key == "years"
                 else compact(int(totals[key])))
        tiles.append((value, TILE_LABELS[key]))
    tiles = tiles[:6]

    height = 196 if len(tiles) > 3 else 130
    parts = card_open(CARD_W, height, theme, "GitHub overview")

    parts.append(text_node(24, 36, "GitHub overview", size=15,
                           fill=theme["primary"], weight="600"))
    parts.append(text_node(CARD_W - 24, 36, data["generated_at"], size=11,
                           fill=theme["secondary"], anchor="end"))
    parts.append(
        f'<line x1="24" y1="52" x2="{CARD_W - 24}" y2="52" '
        f'stroke="{theme["border"]}" stroke-width="1"/>'
    )

    columns = [24, 170, 316]
    rows = [98, 164]
    for index, (value, label) in enumerate(tiles):
        x = columns[index % 3]
        y = rows[index // 3]
        parts.append(text_node(x, y, value, size=26,
                               fill=theme["primary"], weight="650"))
        parts.append(text_node(x, y + 19, label, size=11,
                               fill=theme["secondary"]))

    parts.append("</svg>")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Card: languages
# --------------------------------------------------------------------------

def language_rows(data: dict, max_languages: int) -> list[tuple[str, float]]:
    totals = data["language_bytes"]
    grand_total = sum(totals.values())
    if grand_total <= 0:
        return []

    ordered = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    head = ordered[:max_languages]
    tail = ordered[max_languages:]

    rows = [(name, size / grand_total * 100) for name, size in head]
    if tail:
        rows.append(("Other", sum(size for _, size in tail) / grand_total * 100))
    return rows


def render_languages(data: dict, mode: str, max_languages: int) -> str:
    theme = THEMES[mode]
    rows = language_rows(data, max_languages)

    row_height = 28
    top = 74
    height = top + row_height * len(rows) + 14
    parts = card_open(CARD_W, height, theme, "Top languages")

    parts.append(text_node(24, 36, "Top languages", size=15,
                           fill=theme["primary"], weight="600"))
    parts.append(text_node(
        24, 54,
        f'by bytes of code across {data["totals"]["repos_counted_for_languages"]} '
        f'public repositories',
        size=11, fill=theme["secondary"],
    ))

    bar_x = 148
    bar_end = CARD_W - 76
    bar_width = bar_end - bar_x

    for index, (name, share) in enumerate(rows):
        y = top + index * row_height
        colour = (theme["other"] if name == "Other"
                  else theme["series"][index % len(theme["series"])])

        label = name if len(name) <= 16 else name[:15] + "…"
        parts.append(text_node(24, y + 13, label, size=12,
                               fill=theme["primary"]))

        parts.append(
            f'<rect x="{bar_x}" y="{y + 4}" width="{bar_width}" height="9" '
            f'rx="4.5" fill="{theme["track"]}"/>'
        )
        # The track is the full 100%, so a bar's length is its actual share.
        # Scaling to the largest value instead would make a 55% language look
        # like the whole chart.
        filled = max(5.0, bar_width * (share / 100.0))
        parts.append(
            f'<rect x="{bar_x}" y="{y + 4}" width="{filled:.1f}" height="9" '
            f'rx="4.5" fill="{colour}"/>'
        )

        parts.append(text_node(CARD_W - 24, y + 13, f"{share:.1f}%", size=12,
                               fill=theme["secondary"], anchor="end"))

    parts.append("</svg>")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# README section
# --------------------------------------------------------------------------

START = "<!-- RECENT:START -->"
END = "<!-- RECENT:END -->"


def render_recent_markdown(data: dict, count: int) -> str:
    lines = [
        START,
        "",
        "| Project | What it is | Language | Last push |",
        "| --- | --- | --- | --- |",
    ]
    for repo in data["recent"][:count]:
        description = repo["description"] or "—"
        if len(description) > 90:
            description = description[:89].rstrip() + "…"
        description = description.replace("|", "\\|")
        language = repo["language"] or "—"
        lines.append(
            f'| [{repo["name"]}]({repo["url"]}) | {description} | '
            f'{language} | {repo["pushed_at"]} |'
        )
    lines += ["", f'<sub>Updated automatically on {data["generated_at"]}.</sub>',
              "", END]
    return "\n".join(lines)


def update_readme(readme_path: Path, data: dict, count: int) -> bool:
    if not readme_path.exists():
        return False
    original = readme_path.read_text(encoding="utf-8")
    if START not in original or END not in original:
        print(f"note: {readme_path} has no {START} / {END} markers; skipping")
        return False

    head, _, rest = original.partition(START)
    _, _, tail = rest.partition(END)
    updated = head + render_recent_markdown(data, count) + tail
    if updated == original:
        return False
    readme_path.write_text(updated, encoding="utf-8")
    return True


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def write_file(path: Path, content: str) -> None:
    """Write via a temporary file so a crash never leaves a half-written card."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="scripts/config.json")
    parser.add_argument("--out", default="assets")
    parser.add_argument("--readme", default="README.md")
    parser.add_argument("--data", default=None,
                        help="read collected data from this JSON file instead "
                             "of calling the API (for offline testing)")
    arguments = parser.parse_args()

    config = json.loads(Path(arguments.config).read_text(encoding="utf-8"))

    if arguments.data:
        data = json.loads(Path(arguments.data).read_text(encoding="utf-8"))
    else:
        data = collect(config)

    if not data["language_bytes"]:
        print("error: no language data was returned; refusing to overwrite "
              "the existing cards with an empty one", file=sys.stderr)
        return 1

    out = Path(arguments.out)
    out.mkdir(parents=True, exist_ok=True)

    for mode in ("light", "dark"):
        write_file(
            out / f"overview-{mode}.svg",
            render_overview(data, mode, config.get("tiles", list(TILE_LABELS))),
        )
        write_file(
            out / f"languages-{mode}.svg",
            render_languages(data, mode, int(config.get("max_languages", 7))),
        )

    write_file(out / "data.json", json.dumps(data, indent=2, ensure_ascii=False))

    changed = update_readme(Path(arguments.readme), data,
                            int(config.get("recent_count", 6)))

    print(f'cards written to {out}/ '
          f'({data["totals"]["public_repos"]} repos, '
          f'{len(data["language_bytes"])} languages); '
          f'README {"updated" if changed else "unchanged"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
