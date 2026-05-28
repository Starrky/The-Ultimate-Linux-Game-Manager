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
from curr_test import get_wikidata_from_page_id
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


def list_dirs():
    print("\n")
    dirs_list = dirs

    for dir in dirs_list:
        print(dir)


def tree_dirs():
    print("\n")
    dirs_list = dirs

    for dir in dirs_list:
        dirs_content = os.listdir(dir)
        print(dirs_content)


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


@pcgw_api_rate_limit
def get_wikidata_from_steam_appid(pageid: int) -> str | None:
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


PCGW_PATH_VARIABLES = {
    "appdata": "<compdata>/pfx/drive_c/users/steamuser/AppData/",
    "%LOCALAPPDATA%": "<compdata>/pfx/drive_c/users/steamuser/AppData/Local/",  # Can also sometimes be AppData/LocalLow/ probably have to check both just in case
    "userprofile": "<compdata>/pfx/drive_c/users/steamuser/",
    "documents": "<compdata>/pfx/drive_c/users/steamuser/Documents",
    "savedgames": "<compdata>/pfx/drive_c/users/steamuser/Saved Games",
    "steam": "<Steam-folder>",
    "steamid": "<SteamID>",
    "{{p|game}}": "<gameinstalldir>",
    "": "",
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


def remove_template_blocks(text: str, template_names: set[str]) -> str:
    result = []
    i = 0

    while i < len(text):
        if text.startswith("{{", i):
            start = i
            i += 2

            name_start = i
            while i < len(text) and text[i] not in "|}":
                i += 1

            template_name = text[name_start:i].strip().lower()

            if template_name in template_names:
                depth = 1

                while i < len(text) and depth > 0:
                    if text.startswith("{{", i):
                        depth += 1
                        i += 2
                    elif text.startswith("}}", i):
                        depth -= 1
                        i += 2
                    else:
                        i += 1

                continue

            result.append(text[start:i])
            continue

        result.append(text[i])
        i += 1

    return "".join(result)


def replace_p_templates(text: str) -> str:
    def repl(match):
        key = match.group(1).strip().lower()
        return PCGW_PATH_VARIABLES.get(key, f"<{key}>")

    return re.sub(r"\{\{P\|([^{}|]+)\}\}", repl, text)


def clean_pcgw_save_path(raw: str) -> str:
    text = raw.strip()

    # Remove references and explanatory metadata
    text = remove_template_blocks(
        text,
        {
            "note",
            "refcheck",
            "refurl",
            "refdevice",
            "refsnip",
        },
    )

    # Remove HTML ref blocks if any remain
    text = re.sub(r"<ref\b[^>]*>.*?</ref>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<ref\b[^/]*/>", "", text, flags=re.IGNORECASE)

    # Convert PCGW path placeholders
    text = replace_p_templates(text)

    # Remove simple code tags but keep their content
    text = re.sub(r"</?code>", "", text, flags=re.IGNORECASE)

    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()

    return text


def get_save_data_location(wikitext_data, game_app_id):
    parsed = wtp.parse(wikitext_data)
    for section in parsed.sections:
        if (
            "===Save game data location===" in section
            and "Save game cloud syncing" not in section
        ):
            # print(f"Found Save game data location section: \n{section}")
            game_save_location_section = section
            lines = str(section).splitlines()

            for line in lines:
                try:
                    if "Game data/saves|Windows|" in line:
                        line = line.removeprefix("{{Game data/saves|Windows|")
                        # line = line.replace(
                        #    "{{P|userprofile\\Documents}}", "\\userprofile\\Documents"
                        # )
                        line = line.replace('"string of numbers"|}}', str(game_app_id))
                        line = line.rstrip("}}")
                        line = clean_pcgw_save_path(line)
                        print(f"DEBUG: Found the line: {line}")
                        return game_save_location_section

                except Exception as e:
                    print(f"An error occurred during retrieval: {e}")
                    # ALWAYS return None on failure
                    return None


@pcgw_api_rate_limit
def main():
    # Get steam games save data location
    steam_games = get_installed_steam_games_list()
    for game_name, game_info in steam_games.items():
        steam_game_name = game_name
        steam_game_install_dir = game_info["steam_game_install_dir"]
        steam_game_appid = game_info["steam_game_appid"]
        steam_game_compdata = game_info["steam_game_compdata"]

        # print(f"DEBUG: Steam Game Name: {steam_game_name}")
        # print(f"DEBUG: Steam Game Install Dir: {steam_game_install_dir}")
        # print(f"DEBUG: Steam Game AppID: {steam_game_appid}")
        # print(f"DEBUG: Steam Game CompData: {steam_game_compdata}")

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
