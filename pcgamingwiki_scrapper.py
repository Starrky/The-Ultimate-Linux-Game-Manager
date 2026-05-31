from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import mwparserfromhell
import requests
from bs4 import BeautifulSoup, Tag


PCGW_API_URL = "https://www.pcgamingwiki.com/w/api.php"
USER_AGENT = "PCGWGameDataScraper/0.3"

CACHE_DIR = Path(".pcgw_cache")
HTML_CACHE_DIR = CACHE_DIR / "html"
WIKITEXT_CACHE_DIR = CACHE_DIR / "wikitext"
PAGE_LIST_CACHE = CACHE_DIR / "games_pages.json"
PROCESSED_CACHE = CACHE_DIR / "processed_pageids.json"
FAILED_CACHE = CACHE_DIR / "failed_pages.json"

DEFAULT_REQUESTS_PER_MINUTE = 29
DEFAULT_FLUSH_EVERY = 100
BATCH_SIZE = 50


@dataclass
class GameDataPath:
    platform: str
    path: str
    kind: str
    raw: str


@dataclass
class GameRecord:
    pageid: int
    title: str
    url: str
    steam_appid: int | None
    save_paths: list[GameDataPath]
    config_paths: list[GameDataPath]


class RateLimiter:
    def __init__(self, requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE):
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be greater than 0")

        self.min_interval = 60.0 / requests_per_minute
        self.last_request_at = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_request_at

        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

        self.last_request_at = time.monotonic()


PCGW_TEMPLATE_PATH_VARIABLES = {
    "game": "<gameDir>",
    "uid": "<userId>",
    "steam": "<steamDir>",
    "steam-installation-folder": "<steamDir>",
    "uplay": "<ubisoftDir>",
    "ubisoftconnect": "<ubisoftDir>",
    "ubisoft connect": "<ubisoftDir>",
    "username": "<osUserName>",
    "userprofile": "<home>",
    r"userprofile\documents": "<documents>",
    "documents": "<documents>",
    "appdata": "<appData>",
    "localappdata": "<localAppData>",
    "public": "<public>",
    "allusersprofile": "<programData>",
    "programdata": "<programData>",
    "programfiles": "C:/Program Files",
    "programfilesx86": "C:/Program Files (x86)",
    "windir": "<windowsDir>",
    "machome": "<home>",
    "osxhome": "<home>",
    "linuxhome": "<home>",
    "xdgdatahome": "<xdgData>",
    "xdgconfighome": "<xdgConfig>",
    "hkcu": "HKEY_CURRENT_USER",
    "hkey_current_user": "HKEY_CURRENT_USER",
    "hklm": "HKEY_LOCAL_MACHINE",
    "hkey_local_machine": "HKEY_LOCAL_MACHINE",
    "wow64": "WOW6432Node",
}

IGNORED_WIKI_TEMPLATES = {
    "note",
    "cn",
    "citation needed",
    "ref",
    "refcheck",
    "refurl",
    "key",
    "n/a",
    "unknown",
    "abbr",
    "small",
    "sic",
}

RENDERED_PLACEHOLDERS = {
    "<path-to-game>": "<gameDir>",
    "<Steam-folder>": "<steamDir>",
    "<SteamLibrary-folder>": "<steamLibraryDir>",
    "<user-id>": "<userId>",
    "<Steam-user-id>": "<userId>",
    "<Steam3-user-id>": "<userId>",
    "<Ubisoft-Connect-folder>": "<ubisoftDir>",
    "<Epic-Games-folder>": "<epicDir>",
    "<GOG-Galaxy-folder>": "<gogDir>",
    "<EA-Desktop-folder>": "<eaAppDir>",
    "<Origin-folder>": "<originDir>",
    "<Battle.net-folder>": "<battleNetDir>",
}

ENV_PLACEHOLDERS = {
    "%APPDATA%": "<appData>",
    "%LOCALAPPDATA%": "<localAppData>",
    "%USERPROFILE%": "<home>",
    "%PROGRAMDATA%": "<programData>",
    "%PUBLIC%": "<public>",
    "%WINDIR%": "<windowsDir>",
    "%PROGRAMFILES%": "C:/Program Files",
    "%PROGRAMFILES(X86)%": "C:/Program Files (x86)",
}


def ensure_cache_dirs() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    HTML_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    WIKITEXT_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def load_json_file(path: Path) -> Any | None:
    if not path.exists():
        return None

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2, sort_keys=True)


def replace_file_with_retries(
    source_path: Path,
    destination_path: Path,
    attempts: int = 20,
    delay_seconds: float = 0.25,
) -> None:
    for attempt in range(attempts):
        try:
            source_path.replace(destination_path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise

            time.sleep(delay_seconds)
            delay_seconds = min(delay_seconds * 1.5, 5.0)


def api_get(
    params: dict[str, Any],
    session: requests.Session,
    rate_limiter: RateLimiter,
    max_retries: int = 6,
) -> dict[str, Any]:
    for attempt in range(max_retries):
        rate_limiter.wait()

        response = session.get(
            PCGW_API_URL,
            params={
                **params,
                "format": "json",
                "formatversion": "2",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=60,
        )

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")

            if retry_after and retry_after.isdigit():
                sleep_for = int(retry_after)
            else:
                sleep_for = 60

            print(f"Rate limited. Sleeping for {sleep_for} seconds.")
            time.sleep(sleep_for)
            continue

        response.raise_for_status()
        data = response.json()

        if "error" in data:
            raise RuntimeError(data["error"])

        return data

    raise RuntimeError("Too many 429 responses from PCGamingWiki API")


def page_url(title: str) -> str:
    return "https://www.pcgamingwiki.com/wiki/" + title.replace(" ", "_")


def chunked(items: list[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def list_game_pages(
    session: requests.Session,
    rate_limiter: RateLimiter,
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    cached = load_json_file(PAGE_LIST_CACHE) if use_cache else None

    if cached:
        return cached

    pages: list[dict[str, Any]] = []
    continuation: dict[str, Any] = {}

    while True:
        data = api_get(
            {
                "action": "query",
                "list": "categorymembers",
                "cmtitle": "Category:Games",
                "cmnamespace": "0",
                "cmtype": "page",
                "cmlimit": "500",
                "cmprop": "ids|title",
                **continuation,
            },
            session=session,
            rate_limiter=rate_limiter,
        )

        batch = data.get("query", {}).get("categorymembers", [])

        for page in batch:
            if page.get("ns") == 0 and page.get("pageid") is not None:
                pages.append(
                    {
                        "pageid": int(page["pageid"]),
                        "ns": int(page["ns"]),
                        "title": page["title"],
                    }
                )

        print(f"Listed {len(pages)} game pages...")

        if "continue" not in data:
            break

        continuation = data["continue"]

    write_json_file(PAGE_LIST_CACHE, pages)
    return pages


def wikitext_cache_path(pageid: int) -> Path:
    return WIKITEXT_CACHE_DIR / f"{pageid}.wiki"


def html_cache_path(pageid: int) -> Path:
    return HTML_CACHE_DIR / f"{pageid}.html"


def fetch_wikitext_batch_by_pageids(
    pageids: list[int],
    session: requests.Session,
    rate_limiter: RateLimiter,
    use_cache: bool = True,
) -> dict[int, dict[str, str]]:
    result: dict[int, dict[str, str]] = {}
    missing_pageids: list[int] = []

    for pageid in pageids:
        cache_path = wikitext_cache_path(pageid)

        if use_cache and cache_path.exists():
            result[pageid] = {
                "title": "",
                "wikitext": cache_path.read_text(encoding="utf-8"),
            }
        else:
            missing_pageids.append(pageid)

    if not missing_pageids:
        return result

    data = api_get(
        {
            "action": "query",
            "prop": "revisions",
            "pageids": "|".join(str(pageid) for pageid in missing_pageids),
            "rvprop": "content",
            "rvslots": "main",
            "redirects": "1",
        },
        session=session,
        rate_limiter=rate_limiter,
    )

    for page in data.get("query", {}).get("pages", []):
        if page.get("missing"):
            continue

        pageid = int(page["pageid"])
        title = page.get("title", "")

        revisions = page.get("revisions", [])

        if not revisions:
            continue

        revision = revisions[0]
        slots = revision.get("slots", {})
        main_slot = slots.get("main", {})

        content = (
            main_slot.get("content")
            or main_slot.get("*")
            or revision.get("content")
            or revision.get("*")
            or ""
        )

        if not content:
            continue

        result[pageid] = {
            "title": title,
            "wikitext": content,
        }

        wikitext_cache_path(pageid).write_text(content, encoding="utf-8")

    return result


def fetch_rendered_html_by_pageid(
    pageid: int,
    session: requests.Session,
    rate_limiter: RateLimiter,
    use_cache: bool = True,
) -> str:
    cache_path = html_cache_path(pageid)

    if use_cache and cache_path.exists():
        return cache_path.read_text(encoding="utf-8")

    data = api_get(
        {
            "action": "parse",
            "pageid": str(pageid),
            "prop": "text",
            "redirects": "1",
        },
        session=session,
        rate_limiter=rate_limiter,
    )

    html = data["parse"]["text"]
    cache_path.write_text(html, encoding="utf-8")

    return html


def preprocess_wikitext(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<ref\b[^/]*/>", "", text, flags=re.I)
    text = re.sub(r"<ref\b[^>]*>.*?</ref>", "", text, flags=re.I | re.S)
    return text


def extract_steam_appid_from_wikitext(wikitext: str) -> int | None:
    code = mwparserfromhell.parse(preprocess_wikitext(wikitext))

    for template in code.filter_templates(recursive=True):
        template_name = str(template.name).strip().lower()

        if template_name != "infobox game":
            continue

        for param in template.params:
            param_name = str(param.name).strip().lower()

            if param_name != "steam appid":
                continue

            value = str(param.value).strip()

            if not value:
                return None

            match = re.fullmatch(r"\d+", value)

            if match is None:
                return None

            return int(value)

    return None


def normalize_template_variable_key(key: str) -> str:
    key = key.strip().lower()
    key = key.replace("/", "\\")
    key = re.sub(r"\\+", r"\\", key)
    return key


def flatten_wiki_path_value(value: Any) -> tuple[str, bool]:
    result: list[str] = []
    irregular = False

    code = mwparserfromhell.parse(str(value))

    for node in code.nodes:
        if isinstance(node, mwparserfromhell.nodes.Text):
            result.append(str(node))
            continue

        if isinstance(node, mwparserfromhell.nodes.Template):
            name = str(node.name).strip().lower()

            if name in {"p", "path"}:
                if not node.params:
                    continue

                key, child_irregular = flatten_wiki_path_value(node.params[0].value)
                key = normalize_template_variable_key(key)

                mapped = PCGW_TEMPLATE_PATH_VARIABLES.get(key)

                if mapped is None:
                    irregular = True
                else:
                    result.append(mapped)

                irregular = irregular or child_irregular
                continue

            if name in {"localizedpath", "localisedpath"}:
                for param in node.params:
                    text, child_irregular = flatten_wiki_path_value(param.value)
                    result.append(text)
                    irregular = irregular or child_irregular
                continue

            if name in {"code", "file"}:
                if node.params:
                    text, child_irregular = flatten_wiki_path_value(
                        node.params[0].value
                    )
                    result.append(text)
                    irregular = irregular or child_irregular
                else:
                    irregular = True
                continue

            if name in IGNORED_WIKI_TEMPLATES:
                continue

            irregular = True
            continue

        if isinstance(node, mwparserfromhell.nodes.Wikilink):
            result.append(str(node.title))
            continue

        text = str(node).strip()

        if text:
            irregular = True

    return "".join(result), irregular


def normalize_path(path: str) -> str:
    path = path.strip()
    path = path.rstrip("/\\")
    path = path.replace("\\", "/")

    for source, target in RENDERED_PLACEHOLDERS.items():
        path = path.replace(source, target)

    for source, target in ENV_PLACEHOLDERS.items():
        path = re.sub(re.escape(source), target, path, flags=re.I)

    replacements = [
        (r"(?i)%appdata%", "<appData>"),
        (r"(?i)%userprofile%/AppData/Roaming", "<appData>"),
        (r"(?i)%localappdata%", "<localAppData>"),
        (r"(?i)%userprofile%/AppData/Local/?", "<localAppData>/"),
        (r"(?i)%userprofile%/Documents", "<documents>"),
        (r"(?i)%userprofile%", "<home>"),
        (r"(?i)%programfiles\(x86\)%", "C:/Program Files (x86)"),
        (r"(?i)%programfiles%", "C:/Program Files"),
    ]

    for pattern, replacement in replacements:
        path = re.sub(pattern, replacement, path)

    path = path.replace("{64BitSteamID}", "<userId>")
    path = path.replace("{Steam3AccountID}", "<userId>")

    if path == "~" or path.startswith("~/"):
        path = path.replace("~", "<home>", 1)

    if "#SharedObjects" not in path:
        path = re.sub(r"#+", "*", path)

    path = re.sub(r"\[\d+\]", "", path)
    path = re.sub(r"/{2,}", "/", path)
    path = re.sub(r"(/\*)+$", "", path)
    path = re.sub(r"(/\.)$", "", path)
    path = re.sub(r"/\./", "/", path)
    path = re.sub(r"\s+", " ", path)
    path = path.strip().rstrip("/")

    path = re.sub(r"(?i)<home>/Documents", "<documents>", path)
    path = re.sub(r"(?i)<home>/AppData/Roaming", "<appData>", path)
    path = re.sub(r"(?i)<home>/AppData/Local", "<localAppData>", path)

    return path


def is_usable_path(path: str) -> bool:
    if not path:
        return False

    if path.lower() in {"unknown", "n/a", "none", "not applicable"}:
        return False

    if "{{" in path or "}}" in path:
        return False

    if path.startswith("./") or path.startswith("../"):
        return False

    if re.search(r"[\x00-\x1f\x7f]", path):
        return False

    too_broad = {
        "<gameDir>",
        "<home>",
        "<userId>",
        "<steamDir>",
        "<steamLibraryDir>",
        "<appData>",
        "<localAppData>",
        "<documents>",
        "<programData>",
        "<public>",
        "<windowsDir>",
        "C:/Program Files",
        "C:/Program Files (x86)",
        "/",
    }

    if path in too_broad:
        return False

    if re.fullmatch(r"[A-Za-z]:", path):
        return False

    return True


def dedupe_paths(paths: list[GameDataPath]) -> list[GameDataPath]:
    seen: set[tuple[str, str, str]] = set()
    out: list[GameDataPath] = []

    for item in paths:
        key = (item.kind, item.platform.lower(), item.path)

        if key in seen:
            continue

        seen.add(key)
        out.append(item)

    return out


def extract_paths_from_wikitext(
    wikitext: str,
    debug: bool = False,
) -> tuple[list[GameDataPath], list[GameDataPath]]:
    wikitext = preprocess_wikitext(wikitext)
    code = mwparserfromhell.parse(wikitext)

    save_paths: list[GameDataPath] = []
    config_paths: list[GameDataPath] = []

    for template in code.filter_templates(recursive=True):
        template_name = str(template.name).strip().lower()

        if template_name not in {"game data/saves", "game data/config"}:
            continue

        kind = "save" if template_name == "game data/saves" else "config"
        params = list(template.params)

        if len(params) < 2:
            continue

        platform = str(params[0].value).strip()

        for param in params[1:]:
            raw_path = str(param.value).strip()

            if not raw_path:
                continue

            flattened, irregular = flatten_wiki_path_value(param.value)
            normalized = normalize_path(flattened)
            usable = is_usable_path(normalized)

            if debug:
                print("---- wikitext candidate ----")
                print("kind:      ", kind)
                print("platform:  ", platform)
                print("raw:       ", raw_path)
                print("flattened: ", flattened)
                print("normalized:", normalized)
                print("irregular: ", irregular)
                print("usable:    ", usable)

            if irregular:
                continue

            if not usable:
                continue

            item = GameDataPath(
                platform=platform,
                path=normalized,
                kind=kind,
                raw=raw_path,
            )

            if kind == "save":
                save_paths.append(item)
            else:
                config_paths.append(item)

    return dedupe_paths(save_paths), dedupe_paths(config_paths)


# HTML fallback code.
# This is intentionally optional because it is slow: one extra API request per page.


def normalize_heading_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def heading_level(tag: Tag) -> int | None:
    if not tag.name:
        return None

    if not re.fullmatch(r"h[1-6]", tag.name):
        return None

    return int(tag.name[1])


def clean_heading_text(heading: Tag) -> str:
    headline = heading.select_one(".mw-headline")

    if headline is not None:
        return normalize_heading_text(headline.get_text(" ", strip=True))

    clone = BeautifulSoup(str(heading), "html.parser")

    for unwanted in clone.select(".mw-editsection"):
        unwanted.decompose()

    return normalize_heading_text(clone.get_text(" ", strip=True))


def find_heading(soup: BeautifulSoup, wanted_heading: str) -> Tag | None:
    wanted = normalize_heading_text(wanted_heading)

    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        current = clean_heading_text(heading)

        if current == wanted:
            return heading

        if wanted in current:
            return heading

    return None


def iter_section_nodes(heading: Tag) -> Iterable[Tag]:
    start_level = heading_level(heading)

    if start_level is None:
        return

    for sibling in heading.next_siblings:
        if not isinstance(sibling, Tag):
            continue

        level = heading_level(sibling)

        if level is not None and level <= start_level:
            break

        yield sibling


def remove_noise(cell: Tag) -> None:
    for unwanted in cell.select(
        "sup.reference, span.mw-editsection, style, script, img, .metadata, .noprint"
    ):
        unwanted.decompose()

    for br in cell.find_all("br"):
        br.replace_with("\n")


def extract_cell_text(cell: Tag) -> str:
    clone = BeautifulSoup(str(cell), "html.parser")
    root = clone.find(["td", "th"])

    if root is None:
        root = clone

    remove_noise(root)

    text = root.get_text("\n", strip=True)
    text = re.sub(r"\[\d+\]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)

    return text.strip()


def split_possible_paths(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    out: list[str] = []

    for line in lines:
        lower = line.lower()

        skip_prefixes = (
            "this game follows",
            "the game follows",
            "save files are",
            "configuration files are",
            "cloud saves",
            "steam cloud",
            "see",
            "note:",
            "notes:",
        )

        if lower.startswith(skip_prefixes):
            continue

        if lower in {"unknown", "n/a", "none", "not applicable"}:
            continue

        likely_path = (
            "\\" in line
            or "/" in line
            or "%" in line
            or "<" in line
            or "HKEY_" in line
            or line.startswith("~")
        )

        if not likely_path:
            continue

        out.append(line)

    return out


def parse_data_table(
    table: Tag,
    kind: str,
    debug: bool = False,
) -> list[GameDataPath]:
    results: list[GameDataPath] = []

    for row in table.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)

        if len(cells) < 2:
            continue

        platform = extract_cell_text(cells[0])
        value = extract_cell_text(cells[1])

        if not platform or not value:
            continue

        if platform.lower() in {"system", "os", "platform"}:
            continue

        for raw_path in split_possible_paths(value):
            normalized = normalize_path(raw_path)
            usable = is_usable_path(normalized)

            if debug:
                print("---- html candidate ----")
                print("kind:      ", kind)
                print("platform:  ", platform)
                print("raw:       ", raw_path)
                print("normalized:", normalized)
                print("usable:    ", usable)

            if not usable:
                continue

            results.append(
                GameDataPath(
                    platform=platform,
                    path=normalized,
                    kind=kind,
                    raw=raw_path,
                )
            )

    return results


def find_first_table_in_section(heading: Tag) -> Tag | None:
    for node in iter_section_nodes(heading):
        if node.name == "table":
            return node

        nested = node.find("table")

        if nested is not None:
            return nested

    return None


def extract_paths_from_html(
    html: str,
    debug: bool = False,
) -> tuple[list[GameDataPath], list[GameDataPath]]:
    soup = BeautifulSoup(html, "html.parser")

    save_paths: list[GameDataPath] = []
    config_paths: list[GameDataPath] = []

    section_specs = [
        ("Save game data location", "save"),
        ("Configuration file(s) location", "config"),
        ("Configuration file location", "config"),
    ]

    for heading_text, kind in section_specs:
        heading = find_heading(soup, heading_text)

        if heading is None:
            if debug:
                print(f"DEBUG: heading not found: {heading_text!r}")
            continue

        table = find_first_table_in_section(heading)

        if table is None:
            if debug:
                print(f"DEBUG: table not found under: {heading_text!r}")
            continue

        paths = parse_data_table(table, kind=kind, debug=debug)

        if kind == "save":
            save_paths.extend(paths)
        else:
            config_paths.extend(paths)

    return dedupe_paths(save_paths), dedupe_paths(config_paths)


def record_to_jsonable(
    record: GameRecord,
    include_pageid: bool = True,
) -> dict[str, Any]:
    item = {
        "title": record.title,
        "url": record.url,
        "steamappid": record.steam_appid,
        "save_paths": [asdict(item) for item in record.save_paths],
        "config_paths": [asdict(item) for item in record.config_paths],
    }

    if include_pageid:
        item = {"pageid": record.pageid, **item}

    return item


def records_to_jsonable_by_pageid(
    records: list[GameRecord],
) -> dict[str, dict[str, Any]]:
    return {
        str(record.pageid): record_to_jsonable(record, include_pageid=False)
        for record in records
    }


def load_processed_pageids(path: Path = PROCESSED_CACHE) -> set[int]:
    data = load_json_file(path)

    if not data:
        return set()

    return {int(pageid) for pageid in data}


def save_processed_pageids(pageids: set[int], path: Path = PROCESSED_CACHE) -> None:
    write_json_file(path, sorted(pageids))


def load_failed_pages(path: Path = FAILED_CACHE) -> dict[str, dict[str, Any]]:
    data = load_json_file(path)

    if not data:
        return {}

    return data


def save_failed_pages(
    failed: dict[str, dict[str, Any]], path: Path = FAILED_CACHE
) -> None:
    write_json_file(path, failed)


def load_existing_records(output_path: Path) -> list[GameRecord]:
    data = load_json_file(output_path)

    if not data:
        return []

    records: list[GameRecord] = []

    games = data.get("games", {})
    iterable: Iterable[tuple[int | None, dict[str, Any]]]

    if isinstance(games, dict):
        iterable = ((int(pageid), item) for pageid, item in games.items())
    else:
        iterable = ((None, item) for item in games)

    for pageid, item in iterable:
        save_paths = [GameDataPath(**path) for path in item.get("save_paths", [])]

        config_paths = [GameDataPath(**path) for path in item.get("config_paths", [])]

        records.append(
            GameRecord(
                pageid=pageid if pageid is not None else int(item["pageid"]),
                title=item["title"],
                url=item["url"],
                steam_appid=item.get("steamappid"),
                save_paths=save_paths,
                config_paths=config_paths,
            )
        )

    return records


def write_database_snapshot(output_path: Path, records: list[GameRecord]) -> None:
    payload = {
        "source": "PCGamingWiki",
        "generated_at_unix": int(time.time()),
        "count": len(records),
        "games": records_to_jsonable_by_pageid(records),
    }

    temp_path = output_path.with_name(
        f"{output_path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    )
    write_json_file(temp_path, payload)

    try:
        replace_file_with_retries(temp_path, output_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def write_toml(output_path: Path, records: list[GameRecord]) -> None:
    try:
        import tomli_w
    except ImportError as exc:
        raise RuntimeError("Install TOML writer with: pip install tomli-w") from exc

    payload = {
        "source": "PCGamingWiki",
        "generated_at_unix": int(time.time()),
        "count": len(records),
        "games": records_to_jsonable_by_pageid(records),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("wb") as file:
        tomli_w.dump(payload, file)


def build_database(
    output_path: Path,
    limit: int | None = None,
    use_cache: bool = True,
    requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
    debug: bool = False,
    fallback_to_html: bool = False,
    resume: bool = True,
    retry_failed: bool = False,
    flush_every: int = DEFAULT_FLUSH_EVERY,
) -> list[GameRecord]:
    ensure_cache_dirs()

    session = requests.Session()
    rate_limiter = RateLimiter(requests_per_minute=requests_per_minute)

    pages = list_game_pages(
        session=session,
        rate_limiter=rate_limiter,
        use_cache=use_cache,
    )

    if limit is not None:
        pages = pages[:limit]

    processed_pageids: set[int] = set()
    failed_pages: dict[str, dict[str, Any]] = {}

    if resume:
        processed_pageids = load_processed_pageids()
        failed_pages = load_failed_pages()

    if retry_failed:
        failed_pageids = {int(pageid) for pageid in failed_pages.keys()}
        processed_pageids -= failed_pageids

        for pageid in failed_pageids:
            failed_pages.pop(str(pageid), None)

    records = load_existing_records(output_path) if resume else []
    existing_record_pageids = {record.pageid for record in records}

    total = len(pages)

    wikitext_hits = 0
    html_fallbacks = 0
    empty_pages = 0
    records_added = 0
    pages_done_since_flush = 0

    try:
        for batch_index, original_page_batch in enumerate(
            chunked(pages, BATCH_SIZE), start=1
        ):
            page_batch = [
                page
                for page in original_page_batch
                if int(page["pageid"]) not in processed_pageids
            ]

            if not page_batch:
                continue

            batch_start = (batch_index - 1) * BATCH_SIZE + 1
            batch_end = min(batch_start + len(original_page_batch) - 1, total)

            print(f"[{batch_start}-{batch_end}/{total}] Fetching wikitext batch")

            pageids = [int(page["pageid"]) for page in page_batch]

            try:
                wikitext_by_pageid = fetch_wikitext_batch_by_pageids(
                    pageids=pageids,
                    session=session,
                    rate_limiter=rate_limiter,
                    use_cache=use_cache,
                )

            except Exception as exc:
                print(f"ERROR: batch fetch failed for {pageids}: {exc}")

                for page in page_batch:
                    pageid = int(page["pageid"])
                    failed_pages[str(pageid)] = {
                        "pageid": pageid,
                        "title": page["title"],
                        "stage": "wikitext_batch_fetch",
                        "error": repr(exc),
                        "time": int(time.time()),
                    }

                save_failed_pages(failed_pages)
                continue

            for page in page_batch:
                pageid = int(page["pageid"])
                title = page["title"]

                save_paths: list[GameDataPath] = []
                config_paths: list[GameDataPath] = []
                steam_appid: int | None = None

                try:
                    page_data = wikitext_by_pageid.get(pageid)

                    if page_data:
                        steam_appid = extract_steam_appid_from_wikitext(
                            page_data["wikitext"]
                        )
                        save_paths, config_paths = extract_paths_from_wikitext(
                            page_data["wikitext"],
                            debug=debug,
                        )

                    if save_paths or config_paths:
                        wikitext_hits += 1

                    if fallback_to_html and not save_paths and not config_paths:
                        html_fallbacks += 1

                        if debug:
                            print(f"DEBUG: HTML fallback for {pageid} {title!r}")

                        html = fetch_rendered_html_by_pageid(
                            pageid=pageid,
                            session=session,
                            rate_limiter=rate_limiter,
                            use_cache=use_cache,
                        )

                        save_paths, config_paths = extract_paths_from_html(
                            html,
                            debug=debug,
                        )

                    if save_paths or config_paths:
                        if pageid not in existing_record_pageids:
                            records.append(
                                GameRecord(
                                    pageid=pageid,
                                    title=title,
                                    url=page_url(title),
                                    steam_appid=steam_appid,
                                    save_paths=save_paths,
                                    config_paths=config_paths,
                                )
                            )
                            existing_record_pageids.add(pageid)
                            records_added += 1
                    else:
                        empty_pages += 1

                    processed_pageids.add(pageid)
                    failed_pages.pop(str(pageid), None)

                except Exception as exc:
                    print(f"ERROR: failed pageid={pageid} title={title!r}: {exc}")

                    failed_pages[str(pageid)] = {
                        "pageid": pageid,
                        "title": title,
                        "stage": "page_parse",
                        "error": repr(exc),
                        "time": int(time.time()),
                    }

                    processed_pageids.add(pageid)

                pages_done_since_flush += 1

                if pages_done_since_flush >= flush_every:
                    print("Saving checkpoint...")
                    write_database_snapshot(output_path, records)
                    save_processed_pageids(processed_pageids)
                    save_failed_pages(failed_pages)
                    pages_done_since_flush = 0

            print(
                f"Stats: records={len(records)}, "
                f"added={records_added}, "
                f"wikitext_hits={wikitext_hits}, "
                f"html_fallbacks={html_fallbacks}, "
                f"empty={empty_pages}, "
                f"failed={len(failed_pages)}, "
                f"processed={len(processed_pageids)}/{total}"
            )

    except KeyboardInterrupt:
        print("\nInterrupted. Saving checkpoint before exit...")
        write_database_snapshot(output_path, records)
        save_processed_pageids(processed_pageids)
        save_failed_pages(failed_pages)
        raise

    write_database_snapshot(output_path, records)
    save_processed_pageids(processed_pageids)
    save_failed_pages(failed_pages)

    print(f"Processed pages: {len(processed_pageids)}")
    print(f"Failed pages: {len(failed_pages)}")
    print(f"Records: {len(records)}")

    return records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="pcgw_game_data.json")
    parser.add_argument("--toml-output", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--rpm", type=int, default=DEFAULT_REQUESTS_PER_MINUTE)
    parser.add_argument("--debug", action="store_true")

    # Fast by default. HTML fallback is slow and must be explicitly enabled.
    parser.add_argument("--html-fallback", action="store_true")

    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--flush-every", type=int, default=DEFAULT_FLUSH_EVERY)

    args = parser.parse_args()

    records = build_database(
        output_path=Path(args.output),
        limit=args.limit,
        use_cache=not args.no_cache,
        requests_per_minute=args.rpm,
        debug=args.debug,
        fallback_to_html=args.html_fallback,
        resume=not args.no_resume,
        retry_failed=args.retry_failed,
        flush_every=args.flush_every,
    )

    if args.toml_output:
        write_toml(Path(args.toml_output), records)

    print(f"Done. Wrote {len(records)} game records to {args.output}")


if __name__ == "__main__":
    main()
