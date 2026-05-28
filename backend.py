import os
import vdf
import requests
from ratelimit import limits, RateLimitException
from backoff import on_exception, expo

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
                            "game_install_dir": steam_game_installdir,
                            "game_appid": steam_game_appid,
                            "compdata": steam_compatdata_dir
                            if has_compatdata
                            else "Native Linux Game (No Proton Prefix)",
                        }

                    else:
                        print("game/ app is steam redist/ proton")
    return steam_games


@on_exception(expo, RateLimitException, max_tries=8)
@limits(calls=30, period=60)  # 30 requests / 1 min
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


def main():
    # Step 1: Scan for Steam libraries
    steam_games = get_installed_steam_games_list()
    for game_name, game_info in steam_games.items():
        steam_game_name = game_name
        steam_game_install_dir = game_info["game_install_dir"]
        steam_game_appid = game_info["game_appid"]
        steam_game_compdata = game_info["compdata"]

        print(f"DEBUG: Game Name: {steam_game_name}")
        print(f"DEBUG: Install Dir: {steam_game_install_dir}")
        print(f"DEBUG: AppID: {steam_game_appid}")
        print(f"DEBUG: CompData: {steam_game_compdata}")

        steam_game_page_id = get_page_id_from_steam_appid(steam_game_appid)
        if steam_game_page_id:
            steam_games[steam_game_name]["pcgw_game_page_id"] = steam_game_page_id
            print(
                f"DEBUG: PC Gaming Wiki ID: {steam_games[steam_game_name]['pcgw_game_page_id']}"
            )


if __name__ == "__main__":
    main()
