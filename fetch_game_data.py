import os
import json
import requests
from pathlib import Path
import time
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
import subprocess
import re
import asyncio
import configparser
from playwright.async_api import async_playwright

# --- Constants & environment --- #
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "")
EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME", "")
TRIGGER_SOURCE = os.environ.get("TRIGGER_SOURCE", "")

GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
if GITHUB_REPOSITORY:
    owner, repo = GITHUB_REPOSITORY.split("/")
    FALLBACK_ICON_URL = f"https://{owner}.github.io/{repo}/default_icon.png"
else:
    FALLBACK_ICON_URL = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='64' height='64'%3E%3Crect width='64' height='64' fill='%233d5a6c'/%3E%3Ctext x='50%25' y='50%25' text-anchor='middle' dy='.3em' fill='%23c7d5e0' font-size='24'%3E%3F%3C/text%3E%3C/svg%3E"

appid_dir = Path("AppID")
game_data_path = Path("game-data.json")
top_owners_file = Path("top_owners.json")

DEFAULT_OWNERS = [76561198028121353, 76561198017975643, 76561197979911851, 76561198355953202]

# --- Helper functions --- #

async def fetch_steamhunters_achievements(appid):
    url = f"https://steamhunters.com/apps/{appid}/achievements?group=&sort=name"
    print(f"    → Fetching groups from SteamHunters...")
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            # Navigate with a long timeout because SH can be slow
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            
            # Try to extract the sh.model variable
            sh_data = await page.evaluate("""() => {
                const scripts = Array.from(document.querySelectorAll('script'));
                const target = scripts.find(s => s.textContent.includes('var sh'));
                if (target) {
                    eval(target.textContent);
                    return sh?.model || {};
                }
                return {};
            }""")
            
            if not sh_data:
                return []

            updates_map = {}
            for update in sh_data.get("updates", []):
                u_id = update.get("updateId")
                name = update.get("dlcAppName") or update.get("steamEventName") or (f"Update {update.get('updateNumber')}" if update.get("updateNumber", 0) > 0 else "Base Game")
                updates_map[u_id] = name

            results = []
            for item in sh_data.get("listData", {}).get("pagedList", {}).get("items", []):
                results.append({
                    "name": item.get("apiName"),
                    "displayName": item.get("name"),
                    "description": item.get("description") or " ",
                    "icon": item.get("icon"),
                    "icongray": item.get("iconGray"),
                    "group": updates_map.get(item.get("updateId", 0), "Base Game"),
                    "hidden": 1 if item.get("hidden") else 0
                })
            return results
        except Exception as e:
            print(f"    ✗ SteamHunters Error: {e}")
            return []
        finally:
            await browser.close()

def parse_achievements_ini_lowercase(file_path):
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    try:
        parser.read(file_path, encoding="utf-8")
        result = {}
        for section in parser.sections():
            if section.lower() == "steamachievements": continue
            achieved = parser.get(section, "Achieved", fallback="0")
            result[section] = {"earned": achieved in ("1", "true", "True"), "earned_time": parser.getint(section, "UnlockTime", fallback=0)}
        return result
    except: return {}

def parse_achievements_ini_uppercase(file_path):
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    try:
        parser.read(file_path, encoding="utf-8")
        result = {}
        for section in parser.sections():
            achieved = parser.get(section, "achieved", fallback="false")
            result[section] = {"earned": achieved.lower() in ("1", "true"), "earned_time": parser.getint(section, "timestamp", fallback=0)}
        return result
    except: return {}

def load_achievements_progress(folder):
    appid = folder.name
    files = [
        (folder / "achievements.json", "json"),
        (folder / f"{appid}.db", "db"),
        (folder / "achievements.ini", "ini_low"),
        (folder / "Achievements.ini", "ini_up")
    ]
    for path, ftype in files:
        if not path.exists(): continue
        try:
            if ftype in ("json", "db"):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return {a["apiname"]: {"earned": a.get("achieved", 0) == 1, "earned_time": a.get("unlocktime", 0)} for a in data if "apiname" in a}
                return data
            elif ftype == "ini_low": return parse_achievements_ini_lowercase(path)
            elif ftype == "ini_up": return parse_achievements_ini_uppercase(path)
        except Exception as e: print(f"Error loading {path}: {e}")
    return None

def fetch_steam_store_info(appid):
    try:
        res = requests.get(f"https://store.steampowered.com/api/appdetails?appids={appid}", timeout=10)
        if res.ok:
            data = res.json().get(appid, {})
            if data.get("success"):
                return {"name": data["data"].get("name"), "icon": data["data"].get("header_image")}
    except: pass
    return {"name": f"Game {appid}", "icon": ""}

def fetch_community_xml(appid):
    try:
        res = requests.get(f"https://steamcommunity.com/stats/{appid}/achievements/?xml=1", timeout=10)
        if res.ok:
            # Using basic regex to fix common XML errors before parsing
            clean_content = re.sub(r"&(?!(amp|lt|gt|quot|apos);)", "&amp;", res.text)
            root = ET.fromstring(clean_content)
            achievements = {}
            for ach in root.findall(".//achievement"):
                api_name = ach.findtext("apiname")
                if api_name:
                    achievements[api_name] = {
                        "name": ach.findtext("name"),
                        "description": ach.findtext("description"),
                        "icon": ach.findtext("iconOpen"),
                        "icongray": ach.findtext("iconClosed")
                    }
            return achievements
    except Exception as e: print(f"  ✗ XML Error: {e}")
    return {}

async def process_game(appid, base_path):
    print(f"\nProcessing AppID {appid}...")
    
    # 1. Load local progress first so we have the API names
    local_progress = load_achievements_progress(base_path)
    if not local_progress:
        print(f"  ✗ No local achievement file found (json/db/ini). Skipping.")
        return None

    # 2. Start building metadata
    store_info = fetch_steam_store_info(appid)
    game_info = {
        "appid": appid,
        "name": store_info["name"],
        "icon": store_info["icon"],
        "platform": (base_path.glob("*.platform").__next__().stem if list(base_path.glob("*.platform")) else None),
        "uses_db": (base_path / f"{appid}.db").exists(),
        "uses_ini": (base_path / "achievements.ini").exists() or (base_path / "Achievements.ini").exists(),
        "achievements": {}
    }

    # 3. Fetch Online Data (XML and SteamHunters)
    xml_data = fetch_community_xml(appid)
    sh_data = await fetch_steamhunters_achievements(appid)
    
    # 4. Merge Data
    # Use local keys as the foundation
    for api_name in local_progress.keys():
        # Default Stub
        game_info["achievements"][api_name] = {
            "name": api_name,
            "description": "",
            "icon": FALLBACK_ICON_URL,
            "icongray": FALLBACK_ICON_URL,
            "hidden": False,
            "group": "Base Game"
        }

        # Enrich with XML
        if api_name in xml_data:
            game_info["achievements"][api_name].update({
                "name": xml_data[api_name]["name"] or api_name,
                "description": xml_data[api_name]["description"] or "",
                "icon": xml_data[api_name]["icon"],
                "icongray": xml_data[api_name]["icongray"]
            })

        # Enrich with SteamHunters (DLC groups!)
        for sh_ach in sh_data:
            if sh_ach["name"] == api_name:
                game_info["achievements"][api_name].update({
                    "name": sh_ach["displayName"],
                    "description": sh_ach["description"],
                    "icon": sh_ach["icon"],
                    "icongray": sh_ach["icongray"],
                    "group": sh_ach["group"],
                    "hidden": sh_ach["hidden"] == 1
                })
                break

    # Fix relative icon URLs
    for api_name, ach in game_info["achievements"].items():
        for key in ["icon", "icongray"]:
            if ach[key] and not ach[key].startswith("http"):
                ach[key] = f"https://cdn.steamstatic.com/steamcommunity/public/images/apps/{appid}/{ach[key]}.jpg"
            if not ach[key]: ach[key] = FALLBACK_ICON_URL

    # 5. Save and Return
    with open(base_path / "game-info.json", "w", encoding="utf-8") as f:
        json.dump(game_info, f, indent=2, ensure_ascii=False)
    
    return {"appid": appid, "info": game_info, "achievements": local_progress}

# --- Main Logic --- #

def get_changed_appids():
    try:
        res = subprocess.run(["git", "diff", "--name-only", "HEAD~1", "HEAD"], capture_output=True, text=True)
        ids = set(re.findall(r"AppID/(\d+)/", res.stdout))
        return list(ids)
    except: return []

async def main():
    if EVENT_NAME == "schedule" or TRIGGER_SOURCE == "manual":
        target_ids = [f.name for f in appid_dir.iterdir() if f.is_dir() and f.name.isdigit()]
    else:
        target_ids = get_changed_appids()

    if not target_ids:
        print("No changes detected.")
        return

    processed_games = []
    for appid in target_ids:
        result = await process_game(appid, appid_dir / appid)
        if result: processed_games.append(result)

    # Rebuild master game-data.json
    all_data = []
    for folder in appid_dir.iterdir():
        if folder.is_dir() and folder.name.isdigit():
            info_file = folder / "game-info.json"
            progress = load_achievements_progress(folder)
            if info_file.exists() and progress:
                with open(info_file, 'r', encoding='utf-8') as f:
                    all_data.append({"appid": folder.name, "info": json.load(f), "achievements": progress})
    
    with open(game_data_path, "w", encoding="utf-8") as f:
        json.dump({"last_updated": int(time.time()), "total_games": len(all_data), "games": all_data}, f, indent=2)

if __name__ == "__main__":
    asyncio.run(main())
