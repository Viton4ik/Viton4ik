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

    # Release-asset downloads. GitHub only counts downloads of files attached
    # to a release, so a repository without releases contributes nothing here
    # — it has no download count at all, which is not the same as zero.
    downloads_by_repo: dict[str, int] = {}
    downloads_by_language: dict[str, int] = {}
    release_assets = 0
    for repo in own:
        releases = api_get(f"/repos/{username}/{repo['name']}/releases?per_page=100")
        if not isinstance(releases, list) or not releases:
            continue
        total = 0
        for release in releases:
            for asset in release.get("assets") or []:
                total += int(asset.get("download_count", 0))
                release_assets += 1
        if total <= 0:
            continue
        downloads_by_repo[repo["name"]] = total
        language = repo.get("language") or "Other"
        downloads_by_language[language] = (
            downloads_by_language.get(language, 0) + total
        )

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
            "downloads": sum(downloads_by_repo.values()),
            "release_assets": release_assets,
        },
        "language_bytes": language_bytes,
        "downloads_by_repo": downloads_by_repo,
        "downloads_by_language": downloads_by_language,
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


# Rough per-character advance widths, as a fraction of the font size, for the
# sans-serif stack below. SVG has no text measurement, so a label that must not
# collide with the bar beside it is trimmed against this estimate.
_NARROW = set("ijlt.,:;'`|!I[](){}/\\ ")
_WIDE = set("mwMW@%")
_UPPER = set("ABCDEFGHJKLNOPQRSTUVXYZ&")


def estimate_width(text: str, size: float) -> float:
    total = 0.0
    for character in text:
        if character in _NARROW:
            total += 0.31
        elif character in _WIDE:
            total += 0.92
        elif character in _UPPER:
            total += 0.70
        elif character.isdigit():
            total += 0.56
        else:
            total += 0.55
    return total * size


def fit_text(text: str, size: float, max_px: float) -> str:
    """Trim text with an ellipsis until it fits the given pixel width."""
    if estimate_width(text, size) <= max_px:
        return text
    ellipsis = "…"
    trimmed = text
    while trimmed and estimate_width(trimmed + ellipsis, size) > max_px:
        trimmed = trimmed[:-1]
    return (trimmed.rstrip() + ellipsis) if trimmed else ellipsis


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


def render_bar_card(mode: str, title: str, subtitle: str,
                    rows: list[tuple[str, float, str]]) -> str:
    """Render a labelled bar list.

    Each row is (label, fraction of the whole in 0..1, right-hand value). The
    track is always the whole, so a bar's length is the quantity it stands
    for; scaling to the largest row instead would make the leader look like
    100% of everything.
    """
    theme = THEMES[mode]

    row_height = 28
    top = 74
    height = top + row_height * len(rows) + 14
    parts = card_open(CARD_W, height, theme, title)

    parts.append(text_node(24, 36, title, size=15,
                           fill=theme["primary"], weight="600"))
    parts.append(text_node(24, 54, subtitle, size=11, fill=theme["secondary"]))

    # Give the label column whatever the longest label needs, within limits,
    # so repository names are not squeezed into the width a language name
    # happens to want.
    label_x = 24
    label_space = max(
        (estimate_width(name, 12) for name, _, _ in rows), default=0.0
    )
    label_right = label_x + min(max(label_space + 12, 112.0), 210.0)

    bar_x = label_right
    bar_width = (CARD_W - 76) - bar_x

    for index, (name, fraction, value_text) in enumerate(rows):
        y = top + index * row_height
        colour = (theme["other"] if name == "Other"
                  else theme["series"][index % len(theme["series"])])

        label = fit_text(name, 12, label_right - label_x - 12)
        parts.append(text_node(label_x, y + 13, label, size=12,
                               fill=theme["primary"]))

        parts.append(
            f'<rect x="{bar_x}" y="{y + 4}" width="{bar_width}" height="9" '
            f'rx="4.5" fill="{theme["track"]}"/>'
        )
        filled = max(5.0, bar_width * max(0.0, min(1.0, fraction)))
        parts.append(
            f'<rect x="{bar_x}" y="{y + 4}" width="{filled:.1f}" height="9" '
            f'rx="4.5" fill="{colour}"/>'
        )

        parts.append(text_node(CARD_W - 24, y + 13, value_text, size=12,
                               fill=theme["secondary"], anchor="end"))

    parts.append("</svg>")
    return "\n".join(parts)


def render_languages(data: dict, mode: str, max_languages: int) -> str:
    rows = [(name, share / 100.0, f"{share:.1f}%")
            for name, share in language_rows(data, max_languages)]
    return render_bar_card(
        mode, "Top languages",
        f'by bytes of code across '
        f'{data["totals"]["repos_counted_for_languages"]} public repositories',
        rows,
    )


def render_downloads(data: dict, mode: str, group_by: str,
                     max_rows: int = 7) -> str | None:
    """Release-asset downloads, grouped by language or by repository.

    Returns None when there is nothing to show, so the profile never carries a
    card full of zeroes.
    """
    source = (data.get("downloads_by_language") if group_by == "language"
              else data.get("downloads_by_repo")) or {}
    total = sum(source.values())
    if total <= 0:
        return None

    ordered = sorted(source.items(), key=lambda kv: kv[1], reverse=True)
    head, tail = ordered[:max_rows], ordered[max_rows:]
    rows = [(name, count / total, compact(count)) for name, count in head]
    if tail:
        rest = sum(count for _, count in tail)
        rows.append(("Other", rest / total, compact(rest)))

    noun = "language" if group_by == "language" else "repository"
    return render_bar_card(
        mode, "Release downloads",
        f"{compact(total)} downloads of release files, by {noun}",
        rows,
    )


# --------------------------------------------------------------------------
# README section
# --------------------------------------------------------------------------

START = "<!-- RECENT:START -->"
END = "<!-- RECENT:END -->"

DL_START = "<!-- DOWNLOADS:START -->"
DL_END = "<!-- DOWNLOADS:END -->"

DOWNLOADS_PICTURE = """<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/downloads-dark.svg">
  <img alt="Release downloads" src="assets/downloads-light.svg" width="430">
</picture>"""


def replace_block(text: str, start: str, end: str, body: str) -> str:
    head, _, rest = text.partition(start)
    _, _, tail = rest.partition(end)
    return f"{head}{start}\n{body}\n{end}{tail}"


def render_recent_markdown(data: dict, count: int) -> str:
    lines = [
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
    lines += ["", f'<sub>Updated automatically on {data["generated_at"]}.</sub>', ""]
    return "\n".join(lines)


def update_readme(readme_path: Path, data: dict, count: int,
                  *, show_downloads: bool) -> bool:
    if not readme_path.exists():
        return False
    original = readme_path.read_text(encoding="utf-8")
    updated = original

    if START in updated and END in updated:
        updated = replace_block(updated, START, END,
                                render_recent_markdown(data, count))
    else:
        print(f"note: {readme_path} has no {START} / {END} markers; skipping")

    # The downloads card only exists when there is something to put on it, so
    # the README reference is added and removed along with the file. That way
    # the profile never points at a card that was not generated.
    if DL_START in updated and DL_END in updated:
        updated = replace_block(
            updated, DL_START, DL_END,
            DOWNLOADS_PICTURE if show_downloads else "",
        )

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

    group_by = config.get("downloads_card", "language")
    show_downloads = False

    for mode in ("light", "dark"):
        write_file(
            out / f"overview-{mode}.svg",
            render_overview(data, mode, config.get("tiles", list(TILE_LABELS))),
        )
        write_file(
            out / f"languages-{mode}.svg",
            render_languages(data, mode, int(config.get("max_languages", 7))),
        )

        card = (None if group_by == "off"
                else render_downloads(data, mode, group_by))
        path = out / f"downloads-{mode}.svg"
        if card is None:
            path.unlink(missing_ok=True)
        else:
            write_file(path, card)
            show_downloads = True

    write_file(out / "data.json", json.dumps(data, indent=2, ensure_ascii=False))

    changed = update_readme(Path(arguments.readme), data,
                            int(config.get("recent_count", 6)),
                            show_downloads=show_downloads)

    downloads = data["totals"].get("downloads", 0)
    print(f'cards written to {out}/ '
          f'({data["totals"]["public_repos"]} repos, '
          f'{len(data["language_bytes"])} languages, '
          f'{downloads} release downloads across '
          f'{len(data.get("downloads_by_repo") or {})} repositories); '
          f'downloads card: {"on" if show_downloads else "omitted (no data)"}; '
          f'README {"updated" if changed else "unchanged"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
