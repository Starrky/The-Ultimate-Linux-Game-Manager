import re
import requests
from bs4 import BeautifulSoup


API = "https://www.pcgamingwiki.com/w/api.php"


def get_rendered_html(page: str) -> str:
    r = requests.get(
        API,
        params={
            "action": "parse",
            "page": page,
            "prop": "text",
            "format": "json",
            "disableeditsection": 1,
        },
        headers={"User-Agent": "SaveScanner/0.1"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["parse"]["text"]["*"]


def clean_text(s: str) -> str:
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def normalize_path(s: str) -> str:
    # Remove citation/note markers like [Note 1], [1], [citation needed]
    s = re.sub(r"\[[^\]]+\]", "", s)

    s = re.sub(r"\s+", " ", s).strip()

    # "<SteamLibrary-folder> /steamapps/" -> "<SteamLibrary-folder>/steamapps/"
    s = re.sub(r"\s*([\\/])\s*", r"\1", s)

    # Remove spaces around common placeholder boundaries if BS4 inserted them
    s = re.sub(r"\s+(?=[<>])", "", s)
    s = re.sub(r"(?<=[<>])\s+", "", s)

    # Remove trailing slash/backslash
    s = s.rstrip("\\/")

    return s


def extract_save_locations(page: str) -> dict[str, str]:
    html = get_rendered_html(page)
    soup = BeautifulSoup(html, "lxml")

    headline = soup.find(id="Save_game_data_location")
    if not headline:
        return {}

    heading = headline.find_parent(["h2", "h3", "h4"])
    if not heading:
        return {}

    results = {}

    # walk through elements after the heading until next same/higher heading
    for el in heading.find_all_next():
        if el.name in ["h2", "h3"] and el is not heading:
            break

        if el.name != "table":
            continue

        for row in el.find_all("tr"):
            cells = row.find_all(["td", "th"])

            if len(cells) < 2:
                continue

            platform = clean_text(cells[0].get_text(" ", strip=True))
            path = cells[1].get_text(" ", strip=True)

            if not platform or not path:
                continue

            if platform.lower() in {"system", "location"}:
                continue

            results[platform] = normalize_path(path)

    return results


if __name__ == "__main__":
    saves = extract_save_locations("")

    for platform, path in saves.items():
        print(f"{platform}:")
        print(f"  {path}")

