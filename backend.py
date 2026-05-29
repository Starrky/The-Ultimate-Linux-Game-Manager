import os
import vdf
import requests
from ratelimit import limits, RateLimitException
from backoff import on_exception, expo
import wikitextparser as wtp
from wikitextparser import remove_markup, parse
import time
from functools import wraps
import json
import mwparserfromhell
from pathvalidate import sanitize_filepath
import re

home_dir = os.path.expanduser("~")
share_dir = home_dir + "/.local/share/"
config_dir = home_dir + "/.config/"

steam_lib_vdf = home_dir + "/.local/share/Steam/steamapps/libraryfolders.vdf"
dirs = [config_dir, share_dir]
steam_lib_dirs = []
steam_games = {}
steam_blacklist = ["steam", "proton", "runtime"]
API = "https://www.pcgamingwiki.com/w/api.php"
game_save_location_section = ""


class RateLimiter:
    def __init__(self, calls_per_second: float):
        self.min_interval = 1 / calls_per_second
        self.last_call = 0

    def wait(self):
        now = time.monotonic()
        elapsed = now - self.last_call

        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

        self.last_call = time.monotonic()

    def __call__(self, func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            self.wait()
            return func(*args, **kwargs)

        return wrapper


pcgw_api_rate_limit = RateLimiter(
    calls_per_second=0.45
)  # PC Gaming Wiki API rate limit ( it has 30 limit per minute, so I set it slightly below)
# https://www.pcgamingwiki.com/wiki/PCGamingWiki:API


PCGW_PATH_VARS = {
    "localappdata": r"%LOCALAPPDATA%",
    "appdata": r"%APPDATA%",
    "steam": r"%STEAM%",
    "game": r"<GAME_FOLDER>",
    "uid": r"<USER_ID>",
    "userprofile": r"%USERPROFILE%",
    "userprofile\\documents": r"%USERPROFILE%\Documents",
    "userprofile/documents": r"%USERPROFILE%\Documents",
}
"""
{{p|game}} = <gameinstalldir>
userprofile = <compdata>/pfx/drive_c/users/steamuser/AppData/LocalLow/
{{p|appdata}} = <compdata>/pfx/drive_c/users/steamuser/AppData/Roaming/
%LOCALAPPDATA% = <compdata>/pfx/drive_c/users/steamuser/AppData/Local/
{{p|localappdata}} =  <compdata>/pfx/drive_c/users/steamuser/AppData/Local/
uid seem to be unique so has to listdir for it
steamuserdata_id has to be dirlisted from ~/.local/share/Steam/userdata/
"""
DROP_TEMPLATES = {
    "note",
    "ref",
    "refcheck",
    "refurl",
    "cn",
    "citation needed",
}


def extract_template_body(line: str) -> str | None:
    """
    Extracts the wikitext after:
    DEBUG: Found the location:
    """
    marker = "DEBUG: Found the location:"
    if marker not in line:
        return None

    value = line.split(marker, 1)[1].strip()
    return value or None


def render_pcgw_wikitext(wikitext: str) -> str:

    code = mwparserfromhell.parse(wikitext)

    # Replace nested templates from deepest to shallowest.
    for template in reversed(code.filter_templates(recursive=True)):
        name = str(template.name).strip().lower()

        if name in {"p", "path"}:
            try:
                key = str(template.get(1).value).strip().lower()
            except ValueError:
                replacement = ""
            else:
                replacement = PCGW_PATH_VARS.get(key, f"<{key.upper()}>")

            code.replace(template, replacement)

        elif name in DROP_TEMPLATES:
            code.replace(template, "")

    text = code.strip_code(normalize=True, collapse=True)
    return normalize_windows_path_text(text)


def normalize_windows_path_text(path: str) -> str:
    path = path.strip()

    # Remove wiki leftovers.
    path = re.sub(r"<ref[^>]*>.*?</ref>", "", path, flags=re.I | re.S)
    path = re.sub(r"<[^>]+>", "", path)

    # Normalize whitespace and slashes.
    path = path.replace("/", "\\")
    path = re.sub(r"\s+", " ", path)
    path = path.strip()

    # Remove spaces before path separators.
    path = re.sub(r"\s+\\", r"\\", path)
    path = re.sub(r"\\\s+", r"\\", path)

    # Collapse duplicate backslashes, but avoid changing UNC paths.
    path = re.sub(r"(?<!^)\\{2,}", r"\\", path)

    return path


def extract_game_data_path(wikitext: str) -> str | None:
    """
    Extracts the path argument from:

        {{Game data/saves|Windows|PATH}}

    Returns None if no usable path exists.
    """
    code = mwparserfromhell.parse(wikitext)

    for template in code.filter_templates(recursive=False):
        name = str(template.name).strip().lower()

        if name == "game data/saves":
            try:
                # Param 2 is the actual path.
                raw_path = str(template.get(2).value).strip()
            except ValueError:
                return None

            if not raw_path:
                return None

            return render_pcgw_wikitext(raw_path)

    return None


def sanitize_pcgw_location(text: str) -> str | None:
    marker = "DEBUG: Found the location:"

    if marker in text:
        text = text.split(marker, 1)[1].strip()
    else:
        text = text.strip()

    if not text:
        return None

    path = extract_game_data_path(text)

    if not path:
        return None

    path = sanitize_filepath(path, platform="Windows")

    return path or None


def get_vdf_list():
    print("Scanning steam games...")

    try:
        if os.path.exists(steam_lib_vdf):
            print("VDF Path: " + str(steam_lib_vdf))
            print("Steam VDF present")

        else:
            print("Steam VDF not present, Steam probably not installed")

        with open(steam_lib_vdf, "r") as f:
            vdf_data = vdf.load(f)
            library_folders = vdf_data["libraryfolders"]
            # print(library_folders)
            for paths in library_folders.values():
                steam_lib_dirs.append(paths["path"])

            # print(steam_lib_dirs)
        return steam_lib_dirs

    except FileNotFoundError:
        print("File not found: " + steam_lib_vdf)


def get_installed_steam_games_list():
    get_vdf_list()
    for steamlib in steam_lib_dirs:
        steamapps_dir = steamlib + "/steamapps/"

        for file in os.listdir(steamapps_dir):
            if "appmanifest" in str(file):
                manifest_file = steamapps_dir + file

                with open(manifest_file, "r") as f:
                    acf_file = vdf.load(f)
                    steam_game_appid = acf_file["AppState"]["appid"]
                    steam_game_name = acf_file["AppState"]["name"]
                    steam_game_installdir = acf_file["AppState"]["installdir"]
                    steam_game_installdir = (
                        steamapps_dir + "common/" + steam_game_installdir
                    )
                    steam_compatdata_dir = (
                        steamlib
                        + "/steamapps/compatdata/"
                        + str(steam_game_appid)
                        + "/pfx/"
                    )
                    has_compatdata = os.path.exists(steam_compatdata_dir)

                    if not any(
                        word in steam_game_name.lower() for word in steam_blacklist
                    ):
                        steam_games[steam_game_name] = {
                            "steam_game_install_dir": steam_game_installdir,
                            "steam_game_appid": steam_game_appid,
                            "steam_game_compdata": steam_compatdata_dir
                            if has_compatdata
                            else "Native Linux Game (No Proton Prefix)",
                        }

                    else:
                        print("game/ app is steam redist/ proton")
    return steam_games


@on_exception(expo, RateLimitException, max_tries=8)
@pcgw_api_rate_limit
def get_page_id_from_steam_appid(appid: int) -> str | None:
    """
    Get PCGamingWiki page ID based off Steam AppID.
    """
    appid = int(appid)

    response = requests.get(
        API,
        params={
            "action": "cargoquery",
            "tables": "Infobox_game",
            "fields": "Infobox_game._pageID=PageID",
            "where": f'Infobox_game.Steam_AppID HOLDS "{appid}"',
            "format": "json",
        },
        headers={
            "User-Agent": "SaveScanner/0.1 personal project; contact: local",
        },
        timeout=20,
    )
    if response.status_code != 200:
        raise Exception("API response: {}".format(response.status_code))

    data = response.json()
    rows = data.get("cargoquery", [])

    if not rows:
        return None

    pcgw_game_page_id = rows[0]["title"]["PageID"]

    return str(pcgw_game_page_id)


@on_exception(expo, RateLimitException, max_tries=8)
@pcgw_api_rate_limit
def get_wikidata_from_page_id(pageid: int) -> str | None:
    """
    Get PCGamingWiki page ID based off Steam AppID.
    """
    API = "https://www.pcgamingwiki.com/w/api.php"
    response = requests.get(
        API,
        params={
            "action": "parse",
            "prop": "wikitext",
            "pageid": pageid,
            "format": "json",
        },
        headers={
            "User-Agent": "SaveScanner/0.1 personal project; contact: local",
        },
        timeout=20,
    )
    if response.status_code != 200:
        raise Exception("API response: {}".format(response.status_code))

    data = response.json()
    parsed_data = data["parse"]
    wikitext_data = parsed_data["wikitext"]
    wikitext_data = wikitext_data["*"]
    # print("DEBUG wikitext: \n" + wikitext_data)

    return wikitext_data


def get_save_data_location(wikitext_data, game_app_id):
    parsed = wtp.parse(wikitext_data)
    for section in parsed.sections:
        if (
            "===Save game data location===" in section
            and "Save game cloud syncing" not in section
        ):
            # print(f"Found Save game data location section: \n{section}")
            lines = str(section).splitlines()

            for line in lines:
                try:
                    if "Game data/saves|Windows|" in line:
                        line = sanitize_pcgw_location(line)
                        print(f"DEBUG: Found the location: {line}")
                        return line

                except Exception as e:
                    print(f"An error occurred during retrieval: {e}")
                    return None


@on_exception(expo, RateLimitException, max_tries=8)
@pcgw_api_rate_limit
def main():
    # Get steam games save data location
    steam_games = get_installed_steam_games_list()
    for game_name, game_info in steam_games.items():
        steam_game_name = game_name
        steam_game_install_dir = game_info["steam_game_install_dir"]
        steam_game_appid = game_info["steam_game_appid"]
        steam_game_compdata = game_info["steam_game_compdata"]

        # print(f"""DEBUG: Steam Game Name: {steam_game_name}
        # DEBUG: Steam Game Install Dir: {steam_game_install_dir}
        # DEBUG: Steam Game AppID: {steam_game_appid}
        # DEBUG: Steam Game CompData: {steam_game_compdata}
        # """)

        steam_game_page_id = get_page_id_from_steam_appid(appid=steam_game_appid)
        if steam_game_page_id:
            steam_games[steam_game_name]["steam_game_pcgw_game_page_id"] = (
                steam_game_page_id
            )
            # print(f"DEBUG: PC Gaming Wiki ID: {steam_games[steam_game_name]['steam_game_pcgw_game_page_id']}")

            steam_game_wiki_data = get_wikidata_from_page_id(steam_game_page_id)
            get_save_data_location(steam_game_wiki_data, steam_game_appid)


if __name__ == "__main__":
    main()
