import os
import vdf
import requests
import html
import re
from bs4 import BeautifulSoup


home_dir = os.path.expanduser("~")

share_dir = home_dir + "/.local/share/"
config_dir = home_dir + "/.config/"

steam_lib_vdf = home_dir + "/.local/share/Steam/steamapps/libraryfolders.vdf"
dirs = [config_dir, share_dir]
steam_lib_dirs = []
steam_games = {}
steam_blacklist = ["steam", "proton", "runtime"]
API = "https://www.pcgamingwiki.com/w/api.php"


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


def get_installed_steam_games_list():
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


get_installed_steam_games_list()
for steamlib in steam_lib_dirs:
    steamapps_dir = steamlib + "/steamapps/"

    for file in os.listdir(steamapps_dir):
        if "appmanifest" in str(file):
            manifest_file = steamapps_dir + file

            with open(manifest_file, "r") as f:
                acf_file = vdf.load(f)
                game_appid = acf_file["AppState"]["appid"]
                game_name = acf_file["AppState"]["name"]
                game_installdir = acf_file["AppState"]["installdir"]
                compatdata_dir = (
                    steamlib + "/steamapps/compatdata/" + str(game_appid) + "/pfx/"
                )
                has_compatdata = os.path.exists(compatdata_dir)

                if not any(word in game_name.lower() for word in steam_blacklist):
                    steam_games[game_name] = {
                        "game_install_dir": game_installdir,
                        "game_appid": game_appid,
                        "compdata": compatdata_dir
                        if has_compatdata
                        else "Native Linux Game (No Proton Prefix)",
                    }

                else:
                    print("game/ app is steam redist/ proton")


#
def get_page_id_from_steam_appid(appid: int) -> str | None:
    """
    Get PCGamingWiki page ID based off Steam AppID.
    """
    appid = int(appid)

    r = requests.get(
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

    r.raise_for_status()

    data = r.json()
    rows = data.get("cargoquery", [])

    # print("Rows is:", rows)

    if not rows:
        return None

    page_id = rows[0]["title"]["PageID"]

    return str(page_id)


get_page_id_from_steam_appid(39510)
