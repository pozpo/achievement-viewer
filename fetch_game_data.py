import os
import json
import re
import subprocess
import time
import asyncio
import configparser
from collections import OrderedDict
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from lxml import etree as lxml_etree
from playwright.async_api import async_playwright

# ─── Environment ──────────────────────────────────────────────────────────────
STEAM_API_KEY   = os.environ.get("STEAM_API_KEY", "").strip()
EVENT_NAME      = os.environ.get("GITHUB_EVENT_NAME", "")
TRIGGER_SOURCE  = os.environ.get("TRIGGER_SOURCE", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")

if GITHUB_REPOSITORY:
    _owner, _repo = GITHUB_REPOSITORY.split("/", 1)
    FALLBACK_ICON_URL = f"https://{_owner}.github.io/{_repo}/default_icon.png"
else:
    FALLBACK_ICON_URL = (
        "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
        "width='64' height='64'%3E%3Crect width='64' height='64' fill='%233d5a6c'/"
        "%3E%3Ctext x='50%25' y='50%25' text-anchor='middle' dy='.3em' "
        "fill='%23c7d5e0' font-size='24'%3E%3F%3C/text%3E%3C/svg%3E"
    )

print(f"Using fallback icon URL: {FALLBACK_ICON_URL}")
if STEAM_API_KEY:
    print("Steam API key found — will use ISteamUserStats/GetSchemaForGame")
else:
    print("No Steam API key — falling back to SteamDB / SteamHunters / community XML")

appid_dir       = Path("AppID")
game_data_path  = Path("game-data.json")
top_owners_file = Path("top_owners.json")

# ─── Default owner IDs for hidden-achievement scraping ────────────────────────
DEFAULT_OWNERS = [
    76561198028121353, 76561198017975643, 76561197979911851,
    76561198355953202, 76561197993544755, 76561198001237877,
    76561198355625888, 76561198217186687, 76561198152618007,
    76561198237402290, 76561198213148949, 76561197973009892,
    76561198037867621, 76561197969050296, 76561198019712127,
    76561198094227663, 76561197965319961, 76561197976597747,
    76561197963550511, 76561198044596404, 76561198134044398,
    76561198367471798, 76561199492215670, 76561197962473290,
    76561198842603734, 76561198119667710, 76561197969810632,
    76561197995070100, 76561198017902347, 76561197996432822,
    76561198082995144, 76561198027214426,
]

# ─── Helpers ──────────────────────────────────────────────────────────────────
def load_json_file(path):
    if Path(path).exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"  ✗ Error loading {path}: {e}")
    return None

def save_json_file(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"  ✗ Error saving {path}: {e}")

def load_top_owner_ids():
    if top_owners_file.exists():
        try:
            return load_json_file(top_owners_file).get("steam_ids", DEFAULT_OWNERS)
        except Exception:
            pass
    try:
        save_json_file(top_owners_file, {"steam_ids": DEFAULT_OWNERS, "updated": "hardcoded"})
    except Exception:
        pass
    return DEFAULT_OWNERS

TOP_OWNER_IDS = load_top_owner_ids()

# ─── INI parsers ──────────────────────────────────────────────────────────────
def parse_ini(file_path, timestamp_key):
    """Generic INI parser for both CODEX/ALI213 and GoldBerg/CreamAPI styles."""
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    parser.read(file_path, encoding="utf-8")
    result = {}
    for section in parser.sections():
        if section.lower() == "steamachievements":
            continue
        achieved = parser.get(section, "Achieved", fallback="0") \
                   or parser.get(section, "achieved", fallback="0")
        ts_raw   = parser.get(section, timestamp_key, fallback="0") \
                   or parser.get(section, timestamp_key.lower(), fallback="0")
        try:
            ts = int(ts_raw)
        except ValueError:
            ts = 0
        result[section] = {
            "earned": achieved.strip().lower() in ("1", "true"),
            "earned_time": ts,
        }
    return result

def load_achievements_file(folder):
    appid = folder.name
    for fpath in [folder / "achievements.json", folder / f"{appid}.db"]:
        if fpath.exists():
            try:
                data = load_json_file(fpath)
                if isinstance(data, list):
                    data = {
                        a["apiname"]: {"earned": bool(a.get("achieved")), "earned_time": a.get("unlocktime", 0)}
                        for a in data if "apiname" in a
                    }
                if isinstance(data, dict):
                    return data, fpath.name
            except Exception as e:
                print(f"  ✗ Error reading {fpath}: {e}")

    # GoldBerg/CreamAPI lowercase
    lower = folder / "achievements.ini"
    if lower.exists():
        try:
            data = parse_ini(lower, "UnlockTime")
            if data:
                print(f"  ✓ Loaded {len(data)} achievements from achievements.ini (GoldBerg/CreamAPI)")
                return data, lower.name
        except Exception as e:
            print(f"  ✗ Error reading {lower}: {e}")

    # CODEX/ALI213 uppercase
    upper = folder / "Achievements.ini"
    if upper.exists():
        try:
            data = parse_ini(upper, "timestamp")
            if data:
                print(f"  ✓ Loaded {len(data)} achievements from Achievements.ini (CODEX/ALI213)")
                return data, upper.name
        except Exception as e:
            print(f"  ✗ Error reading {upper}: {e}")

    return None, None

# ─── CODEX ACHn remapping ─────────────────────────────────────────────────────
_CODEX_RE = re.compile(r'^ACH_?(\d+)$', re.IGNORECASE)

def is_codex_numeric(keys):
    return bool(keys) and all(_CODEX_RE.match(k) for k in keys)

def remap_codex(ini_data, ordered_real_names):
    """Map ACH1/ACH_1 → 1st real API name (1-indexed)."""
    real = list(ordered_real_names)
    out  = {}
    for k, v in ini_data.items():
        m = _CODEX_RE.match(k)
        if not m:
            continue
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(real):
            out[real[idx]] = v
        else:
            print(f"  ⚠ CODEX key {k} index {idx+1} out of range ({len(real)} achievements)")
    return out

def build_full_user_data(ini_data, xml_ordered):
    """
    Produce a complete earned dict covering ALL Steam achievements.
    Handles direct name match OR CODEX ACHn index remapping.
    """
    if not xml_ordered:
        return ini_data

    ini_keys = list(ini_data.keys())
    if is_codex_numeric(ini_keys):
        print(f"  → CODEX ACHn scheme detected ({len(ini_keys)} earned keys), remapping…")
        earned = remap_codex(ini_data, xml_ordered)
    else:
        earned = ini_data.copy()
        unmatched = [k for k in ini_keys if k not in xml_ordered]
        if unmatched:
            print(f"  ⚠ {len(unmatched)} ini keys not in Steam schema: {unmatched[:5]}")

    full = {}
    for name in xml_ordered:
        full[name] = earned.get(name, {"earned": False, "earned_time": 0})

    n_earned = sum(1 for v in full.values() if v["earned"])
    print(f"  ✓ Full achievement list: {len(full)} total, {n_earned} earned")
    return full

# ─── Steam data fetchers ──────────────────────────────────────────────────────
def fetch_store_info(appid):
    try:
        r = requests.get(
            f"https://store.steampowered.com/api/appdetails?appids={appid}", timeout=10)
        if r.ok:
            d = r.json().get(appid, {})
            if d.get("success"):
                return {
                    "name": d["data"].get("name", f"Game {appid}"),
                    "icon": d["data"].get("header_image", ""),
                }
    except Exception as e:
        print(f"  ✗ Store info error: {e}")
    return {"name": f"Game {appid}", "icon": ""}

def fetch_schema_with_key(appid):
    """
    Fetch achievement schema via Steam Web API key.
    Returns OrderedDict of api_name → {name, description, icon, icongray, hidden}.
    """
    if not STEAM_API_KEY:
        return OrderedDict()
    try:
        r = requests.get(
            f"https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/"
            f"?key={STEAM_API_KEY}&appid={appid}", timeout=15)
        if not r.ok:
            print(f"  ✗ Schema API returned HTTP {r.status_code}")
            return OrderedDict()
        schema_list = (r.json()
                       .get("game", {})
                       .get("availableGameStats", {})
                       .get("achievements", []))
        result = OrderedDict()
        for ach in schema_list:
            api_name = ach.get("name", "")
            if not api_name:
                continue
            icon_raw  = ach.get("icon",     "")
            gray_raw  = ach.get("icongray", "")

            def fix_icon(raw):
                if not raw:
                    return FALLBACK_ICON_URL
                if raw.startswith("http"):
                    return raw
                return f"https://cdn.steamstatic.com/steamcommunity/public/images/apps/{appid}/{raw}.jpg"

            result[api_name] = {
                "name":        ach.get("displayName", api_name),
                "description": ach.get("description", ""),
                "icon":        fix_icon(icon_raw),
                "icongray":    fix_icon(gray_raw),
                "hidden":      ach.get("hidden", 0) == 1,
            }
        print(f"  ✓ Steam API schema: {len(result)} achievements")
        return result
    except Exception as e:
        print(f"  ✗ Schema API error: {e}")
        return OrderedDict()

def fetch_community_xml(appid):
    """
    Fetch achievement list from steamcommunity.com stats XML endpoint.
    Uses lxml recover mode to handle malformed XML (unescaped & / < in descriptions).
    Returns OrderedDict preserving Steam's ordering (needed for CODEX ACHn mapping).
    """
    result = OrderedDict()
    try:
        r = requests.get(
            f"https://steamcommunity.com/stats/{appid}/achievements/?xml=1",
            timeout=12)
        if not r.ok:
            print(f"  ✗ Community XML returned HTTP {r.status_code}")
            return result
        # Sanity-check: if it's HTML (age-gate / block), bail out
        if b"<!DOCTYPE" in r.content[:100] or b"<html" in r.content[:100].lower():
            print(f"  ✗ Community XML returned HTML (blocked or age-gated)")
            return result
        parser = lxml_etree.XMLParser(recover=True)
        root   = lxml_etree.fromstring(r.content, parser=parser)
        for ach in root.findall(".//achievement"):
            api_name = (ach.findtext("apiname") or "").strip()
            if not api_name:
                continue

            def icon_url(raw):
                raw = (raw or "").strip()
                if not raw:
                    return FALLBACK_ICON_URL
                if raw.startswith("http"):
                    return raw
                return f"https://cdn.steamstatic.com/steamcommunity/public/images/apps/{appid}/{raw}.jpg"

            result[api_name] = {
                "name":        (ach.findtext("name")        or "").strip() or api_name,
                "description": (ach.findtext("description") or "").strip(),
                "icon":        icon_url(ach.findtext("iconOpen")),
                "icongray":    icon_url(ach.findtext("iconClosed")),
                "hidden":      False,
            }
        if result:
            print(f"  ✓ Community XML: {len(result)} achievements")
        else:
            print(f"  ✗ Community XML parsed OK but found 0 achievements")
    except Exception as e:
        print(f"  ✗ Community XML error: {e}")
    return result

def fetch_global_percentages_order(appid):
    """
    Fetch the ordered list of achievement API names from the global percentages API.
    This endpoint requires no API key and returns achievements in schema order.
    Used for CODEX ACHn mapping when the schema API is unavailable.
    Returns an OrderedDict of api_name → {percent} (names/icons unknown here).
    """
    result = OrderedDict()
    try:
        r = requests.get(
            f"https://api.steampowered.com/ISteamUserStats/"
            f"GetGlobalAchievementPercentagesForApp/v0002/?gameid={appid}",
            timeout=10)
        if not r.ok:
            print(f"  ✗ Global percentages API returned HTTP {r.status_code}")
            return result
        for ach in r.json().get("achievementpercentages", {}).get("achievements", []):
            name = ach.get("name", "")
            if name:
                result[name] = {"percent": ach.get("percent", 0)}
        if result:
            print(f"  ✓ Global percentages: {len(result)} achievement names")
    except Exception as e:
        print(f"  ✗ Global percentages error: {e}")
    return result

# ─── SteamDB scraper (Playwright, Python port of Achievements app logic) ──────
async def fetch_steamdb(appid):
    """
    Scrape achievement metadata from steamdb.info/app/{appid}/stats/.
    Mirrors the scrapeSteamDB() logic from the Achievements desktop app.
    Returns OrderedDict of api_name → {name, description, icon, icongray, hidden, group}.
    """
    url = f"https://steamdb.info/app/{appid}/stats/"
    print(f"    → Fetching from SteamDB…")
    result = OrderedDict()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"),
            viewport={"width": 1400, "height": 1000})

        # Block media/font to speed up loading
        async def block_resources(route):
            if re.search(r'\.(mp4|webm|gif|woff2?|ttf|otf)$', route.request.url, re.I):
                await route.abort()
            else:
                await route.continue_()
        await ctx.route("**/*", block_resources)

        page = await ctx.new_page()
        page.set_default_timeout(20000)

        try:
            await page.goto(url, wait_until="domcontentloaded")

            # Wait for achievement rows to appear
            try:
                await page.wait_for_selector('[id^="achievement-"]', timeout=15000)
            except Exception:
                print(f"    ✗ SteamDB: no achievements found for {appid} (timeout or no data)")
                return result

            # Scroll through all items to trigger lazy-load images (mirrors the app's hover loop)
            items = await page.query_selector_all('[id^="achievement-"]')
            if not items:
                print(f"    ✗ SteamDB: no achievement elements for {appid}")
                return result

            for item in items:
                try:
                    await item.scroll_into_view_if_needed()
                    await item.hover()
                    await asyncio.sleep(0.012)
                except Exception:
                    pass

            # Parse from page HTML (mirrors extractSteamDbFromHtml)
            html = await page.content()
            from bs4 import BeautifulSoup as _BS
            soup = _BS(html, "lxml")

            def safe_text(el):
                return el.get_text(strip=True) if el else ""

            def to_abs(raw_url):
                if not raw_url:
                    return ""
                if raw_url.startswith("http"):
                    return raw_url
                if raw_url.startswith("//"):
                    return "https:" + raw_url
                return raw_url

            def fix_icon(raw_url, appid_str):
                raw_url = to_abs(raw_url)
                if not raw_url:
                    return FALLBACK_ICON_URL
                return raw_url

            seen = set()
            for el in soup.find_all(id=re.compile(r'^achievement-')):
                el_id = el.get("id", "")
                if not el_id.startswith("achievement-"):
                    continue

                # API name: div.achievement_api inside div.achievement_right
                api_el = el.select_one(
                    "div.achievement_inner > div > div.achievement_right > div.achievement_api")
                api_name = safe_text(api_el) or el_id.replace("achievement-", "")
                if not api_name or api_name in seen:
                    continue
                seen.add(api_name)

                # Display name
                name_el = el.select_one(
                    "div.achievement_inner > div > div:nth-child(1) > div.achievement_name")
                display_name = safe_text(name_el) or api_name

                # Description (may contain "Hidden achievement: ..." prefix)
                desc_el = el.select_one(
                    "div.achievement_inner > div > div:nth-child(1) > div.achievement_desc")
                raw_desc = safe_text(desc_el)

                # Detect and strip hidden prefix (mirrors normalizeHidden)
                hidden = False
                description = raw_desc
                if re.match(r'^\s*Hidden achievement', raw_desc, re.IGNORECASE):
                    hidden = True
                    description = re.sub(
                        r'^\s*Hidden achievement[:\.]?\s*', '', raw_desc, flags=re.IGNORECASE).strip()
                    if re.match(r'^This achievement is hidden\.$', description, re.IGNORECASE):
                        description = ""

                # Icon (unlocked) — div.achievement_inner > img
                icon_el = el.select_one("div.achievement_inner > img")
                icon_url = ""
                if icon_el:
                    icon_url = (icon_el.get("src") or icon_el.get("data-src") or
                                icon_el.get("data-original") or "")
                if not icon_url:
                    pic_img = el.select_one(".achievement_inner picture img")
                    if pic_img:
                        icon_url = (pic_img.get("src") or pic_img.get("data-src") or "")
                icon_url = fix_icon(icon_url, appid)

                # Gray icon (locked) — div.achievement_checkmark > img
                gray_el = el.select_one("div.achievement_checkmark > img")
                gray_url = ""
                if gray_el:
                    gray_url = (gray_el.get("src") or gray_el.get("data-src") or
                                gray_el.get("data-original") or "")
                    if not gray_url:
                        data_name = gray_el.get("data-name")
                        if data_name:
                            gray_url = (f"https://cdn.fastly.steamstatic.com/"
                                        f"steamcommunity/public/images/apps/{appid}/{data_name}")
                gray_url = fix_icon(gray_url, appid) or icon_url

                result[api_name] = {
                    "name":        display_name,
                    "description": description,
                    "icon":        icon_url or FALLBACK_ICON_URL,
                    "icongray":    gray_url or FALLBACK_ICON_URL,
                    "hidden":      hidden,
                    "group":       "Base Game",
                }

            if result:
                print(f"    ✓ SteamDB: {len(result)} achievements for {appid}")
            else:
                print(f"    ✗ SteamDB: parsed HTML but found 0 achievements for {appid}")

        except Exception as e:
            print(f"    ✗ SteamDB error for {appid}: {e}")
        finally:
            await browser.close()

    return result

async def fetch_steamhunters(appid):
    """Fetch achievement metadata from SteamHunters (includes group/DLC info)."""
    url = f"https://steamhunters.com/apps/{appid}/achievements?group=&sort=name"
    print(f"    → Fetching from SteamHunters…")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 800})
        page = await ctx.new_page()
        try:
            await page.goto(url, timeout=30000)
            try:
                await page.wait_for_function(
                    "() => Array.from(document.querySelectorAll('script'))"
                    ".some(s => s.textContent.includes('var sh'))",
                    timeout=20000)
            except Exception:
                print(f"    ✗ SteamHunters blocked or timed out for {appid}")
                return []
            await page.evaluate(
                "() => { const s = Array.from(document.querySelectorAll('script'))"
                ".find(s => s.textContent.includes('var sh')); eval(s.textContent); }")
            sh = await page.evaluate("() => sh?.model || {}")

            updates_map = {}
            for u in sh.get("updates", []):
                uid  = u.get("updateId")
                if u.get("dlcAppName"):
                    label = u["dlcAppName"]
                elif u.get("steamEventName"):
                    label = u["steamEventName"]
                elif u.get("updateNumber", 0) == 0 and not u.get("dlcAppId"):
                    label = "Base Game"
                elif u.get("updateNumber", 0) > 0:
                    label = f"Update {u['updateNumber']}"
                else:
                    label = "Base Game"
                updates_map[uid] = label

            items = sh.get("listData", {}).get("pagedList", {}).get("items", [])
            results = []
            for item in items:
                uid   = item.get("updateId", 0)
                icon  = item.get("icon") or ""
                gray  = item.get("iconGray") or ""
                results.append({
                    "name":        item.get("apiName"),
                    "displayName": item.get("name"),
                    "hidden":      1 if item.get("hidden") else 0,
                    "description": item.get("description") or "",
                    "icon":        icon if icon.startswith("http") else (
                        f"https://cdn.steamstatic.com/steamcommunity/public/images/apps/{appid}/{icon}.jpg"
                        if icon else FALLBACK_ICON_URL),
                    "icongray":    gray if gray.startswith("http") else (
                        f"https://cdn.steamstatic.com/steamcommunity/public/images/apps/{appid}/{gray}.jpg"
                        if gray else FALLBACK_ICON_URL),
                    "group":       updates_map.get(uid, "Base Game"),
                })
            return results
        except Exception as e:
            print(f"    ✗ SteamHunters error: {e}")
            return []
        finally:
            await browser.close()

def fetch_achievements_metadata(appid, existing_info):
    """
    Fetch achievement metadata from the best available source:
      1. Steam API key (most reliable, requires STEAM_API_KEY secret)
      2. SteamDB (Playwright scrape — no key needed, same approach as Achievements app)
      3. SteamHunters (Playwright scrape — no key, includes DLC grouping)
      4. Community XML (plain HTTP, no key, fallback)
    Returns: (OrderedDict of metadata, list of hidden api names needing descriptions)
    """
    # ── Source 1: Steam API key ────────────────────────────────────────────────
    if STEAM_API_KEY:
        schema = fetch_schema_with_key(appid)
        if schema:
            # Merge group info from SteamHunters if possible (don't block on failure)
            sh_data = []
            try:
                sh_data = asyncio.run(fetch_steamhunters(appid))
            except Exception:
                pass
            sh_map = {item["name"]: item for item in sh_data if item.get("name")}
            for api_name, info in schema.items():
                info["group"] = sh_map.get(api_name, {}).get("group", "Base Game")
                # Fill missing descriptions from existing data
                if not info["description"]:
                    info["description"] = (
                        existing_info.get("achievements", {})
                        .get(api_name, {}).get("description", ""))
            hidden = [k for k, v in schema.items() if v["hidden"] and not v["description"]]
            return schema, hidden

    # ── Source 2: SteamDB ─────────────────────────────────────────────────────
    try:
        steamdb_data = asyncio.run(fetch_steamdb(appid))
    except Exception as e:
        print(f"  ✗ SteamDB exception: {e}")
        steamdb_data = OrderedDict()

    if steamdb_data:
        for api, info in steamdb_data.items():
            if not info["description"]:
                info["description"] = (
                    existing_info.get("achievements", {})
                    .get(api, {}).get("description", ""))
        hidden = [k for k, v in steamdb_data.items() if v["hidden"] and not v["description"]]
        return steamdb_data, hidden

    # ── Source 3: SteamHunters ────────────────────────────────────────────────
    try:
        sh_data = asyncio.run(fetch_steamhunters(appid))
    except Exception as e:
        print(f"  ✗ SteamHunters exception: {e}")
        sh_data = []

    if sh_data:
        result = OrderedDict()
        for item in sh_data:
            api = item.get("name")
            if not api:
                continue
            result[api] = {
                "name":        item.get("displayName", api),
                "description": item.get("description", ""),
                "icon":        item["icon"],
                "icongray":    item["icongray"],
                "hidden":      item.get("hidden", 0) == 1,
                "group":       item.get("group", "Base Game"),
            }
            if not result[api]["description"]:
                result[api]["description"] = (
                    existing_info.get("achievements", {})
                    .get(api, {}).get("description", ""))
        hidden = [k for k, v in result.items() if v["hidden"] and not v["description"]]
        print(f"  ✓ SteamHunters: {len(result)} achievements")
        return result, hidden

    # ── Source 4: Community XML ───────────────────────────────────────────────
    xml_data = fetch_community_xml(appid)
    if xml_data:
        for api, info in xml_data.items():
            if not info["description"]:
                info["description"] = (
                    existing_info.get("achievements", {})
                    .get(api, {}).get("description", ""))
            info["group"] = "Base Game"
        hidden = [k for k, v in xml_data.items() if v["hidden"] and not v["description"]]
        return xml_data, hidden

    return OrderedDict(), []

def scrape_hidden_descriptions(appid, hidden_names, achievement_names_map, game_info):
    """Scrape hidden achievement descriptions from public Steam profiles."""
    if not hidden_names:
        return
    print(f"  → Scraping descriptions for {len(hidden_names)} hidden achievements…")
    found = 0
    for steam_id in TOP_OWNER_IDS[:32]:
        try:
            url = f"https://steamcommunity.com/profiles/{steam_id}/stats/{appid}/achievements"
            r   = requests.get(url, timeout=15)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for row in soup.find_all("div", class_="achieveRow"):
                h3 = row.find("h3")
                h5 = row.find("h5")
                if not h3 or not h5:
                    continue
                disp = h3.text.strip()
                desc = h5.text.strip()
                api  = achievement_names_map.get(disp.lower())
                if api and api in game_info["achievements"] and desc:
                    if not game_info["achievements"][api]["description"]:
                        game_info["achievements"][api]["description"] = desc
                        found += 1
        except Exception:
            pass

        still_missing = [a for a in hidden_names if not game_info["achievements"].get(a, {}).get("description")]
        print(f"    → {found}/{len(hidden_names)} found, {len(still_missing)} missing")
        if not still_missing:
            break
        time.sleep(2)

def fetch_percentages(appid, game_info, existing_info):
    """Fetch global unlock percentages and attach to achievements."""
    try:
        r = requests.get(
            f"https://api.steampowered.com/ISteamUserStats/"
            f"GetGlobalAchievementPercentagesForApp/v0002/?gameid={appid}",
            timeout=10)
        if r.ok:
            for entry in r.json().get("achievementpercentages", {}).get("achievements", []):
                name = entry.get("name")
                if name and name in game_info["achievements"]:
                    game_info["achievements"][name]["percent"] = entry.get("percent", 0)
            print(f"  ✓ Got percentages")
            return
        print(f"  ⚠ Percentages API HTTP {r.status_code} — preserving existing")
    except requests.exceptions.Timeout:
        print(f"  ⚠ Percentages API timed out — preserving existing")
    except Exception as e:
        print(f"  ⚠ Percentages error: {e} — preserving existing")
    # Fall back to existing data
    for api in game_info["achievements"]:
        old = existing_info.get("achievements", {}).get(api, {}).get("percent")
        if old is not None and "percent" not in game_info["achievements"][api]:
            game_info["achievements"][api]["percent"] = old

# ─── Changed-AppID detection ──────────────────────────────────────────────────
def get_changed_appids():
    changed = []
    sha   = os.environ.get("GITHUB_SHA", "")
    before= os.environ.get("GITHUB_BEFORE", "")
    if sha and before and before != "0" * 40:
        r = subprocess.run(["git","diff","--name-only", before, sha],
                           capture_output=True, text=True)
        if r.returncode == 0:
            changed = r.stdout.strip().splitlines()
    if not changed:
        r = subprocess.run(["git","diff","--name-only","HEAD~1","HEAD"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            changed = r.stdout.strip().splitlines()
    r2 = subprocess.run(["git","diff","--name-only","--cached"],
                        capture_output=True, text=True)
    if r2.returncode == 0:
        changed += r2.stdout.strip().splitlines()
    r3 = subprocess.run(["git","ls-files","--others","--exclude-standard","AppID/"],
                        capture_output=True, text=True)
    if r3.returncode == 0:
        changed += r3.stdout.strip().splitlines()
    found = set()
    for f in changed:
        m = re.match(r"AppID/(\d+)/", f)
        if m:
            found.add(m.group(1))
    if found:
        print(f"Detected changes in AppIDs: {', '.join(sorted(found))}")
    else:
        print("No AppID-specific changes detected")
    return list(found)

# ─── Determine which AppIDs to process ────────────────────────────────────────
if EVENT_NAME == "schedule" or (EVENT_NAME == "workflow_dispatch" and TRIGGER_SOURCE == "manual"):
    appids = [f.name for f in appid_dir.iterdir() if f.is_dir() and f.name.isdigit()]
    print(f"Processing all {len(appids)} games (Reason: {EVENT_NAME}/{TRIGGER_SOURCE})")
else:
    appids = get_changed_appids()
    if appids:
        print(f"Processing {len(appids)} changed game(s): {', '.join(appids)}")
    else:
        print("No game-specific changes. Exiting.")
        raise SystemExit(0)

# ─── Load existing game-data.json ─────────────────────────────────────────────
existing_game_data = {}
raw = load_json_file(game_data_path)
if raw:
    lst = raw.get("games", raw) if isinstance(raw, dict) else raw
    for g in (lst if isinstance(lst, list) else []):
        existing_game_data[g["appid"]] = g
    print(f"Loaded existing data for {len(existing_game_data)} games")

# ─── Main loop ────────────────────────────────────────────────────────────────
for appid in appids:
    print(f"\nProcessing AppID {appid}…")
    base = appid_dir / appid

    platform_files   = list(base.glob("*.platform"))
    current_platform = platform_files[0].stem if platform_files else None

    blacklist_file   = base / "blacklist"
    current_blacklist = (
        [l.strip() for l in open(blacklist_file, encoding="utf-8") if l.strip()]
        if blacklist_file.exists() else [])

    # ── Skip file ──────────────────────────────────────────────────────────────
    if (base / "skip").exists():
        print(f"  ! 'skip' file found, preserving existing data")
        ei = load_json_file(base / "game-info.json")
        ad, _ = load_achievements_file(base)
        if ei and ad:
            ei["platform"]  = current_platform
            ei["blacklist"] = current_blacklist
            existing_game_data[str(appid)] = {"appid": str(appid), "info": ei, "achievements": ad}
            print(f"  ✓ Preserved")
        else:
            print(f"  ✗ Missing game-info.json or achievements file")
        continue

    existing_info = load_json_file(base / "game-info.json") or {}

    # ── Fetch store info ───────────────────────────────────────────────────────
    store = fetch_store_info(appid)
    time.sleep(1)

    game_info = {
        "appid":    appid,
        "name":     store.get("name", f"Game {appid}"),
        "icon":     store.get("icon", ""),
        "platform": current_platform,
        "blacklist":current_blacklist,
        "uses_db":  (base / f"{appid}.db").exists(),
        "uses_ini": (base / "achievements.ini").exists() or (base / "Achievements.ini").exists(),
        "achievements": {},
    }

    # ── Fetch achievement metadata ─────────────────────────────────────────────
    metadata, hidden_names = fetch_achievements_metadata(appid, existing_info)
    time.sleep(1)

    # ── If no metadata at all, fall back to minimal stub ──────────────────────
    if not metadata:
        print(f"  ! All metadata sources returned 0 achievements for {appid}")
        ei = load_json_file(base / "game-info.json")
        ad, ft = load_achievements_file(base)

        if ei and ad:
            ei["platform"]  = current_platform
            ei["blacklist"] = current_blacklist
            save_json_file(base / "game-info.json", ei)
            existing_game_data[str(appid)] = {"appid": str(appid), "info": ei, "achievements": ad}
            print(f"  ✓ Existing game-info.json preserved")
        elif ad:
            print(f"  → Building minimal game-info.json from {ft} ({len(ad)} entries)")
            # Try to get at least the ordered names from percentages API for CODEX mapping
            pct_order = fetch_global_percentages_order(appid)
            if pct_order and ad:
                full_ad = build_full_user_data(ad, pct_order)
            else:
                full_ad = ad
            minimal = {
                **game_info,
                "achievements": {
                    name: {"name": name, "description": "", "icon": FALLBACK_ICON_URL,
                           "icongray": FALLBACK_ICON_URL, "hidden": False}
                    for name in full_ad
                }
            }
            save_json_file(base / "game-info.json", minimal)
            existing_game_data[str(appid)] = {"appid": str(appid), "info": minimal, "achievements": full_ad}
            print(f"  ✓ Wrote minimal game-info.json")
        else:
            print(f"  ✗ No achievements file found for {appid}, skipping")
        continue

    # ── Store metadata ─────────────────────────────────────────────────────────
    game_info["achievements"] = dict(metadata)
    print(f"  ✓ Merged {len(metadata)} achievements")

    # ── Build achievement names map for hidden scraping ────────────────────────
    names_map = {v["name"].lower(): k for k, v in metadata.items()}

    # ── Scrape hidden descriptions ─────────────────────────────────────────────
    if hidden_names:
        scrape_hidden_descriptions(appid, hidden_names, names_map, game_info)

    # ── Fetch percentages ──────────────────────────────────────────────────────
    fetch_percentages(appid, game_info, existing_info)

    # ── Handle missing hidden descriptions file ────────────────────────────────
    missing_path = base / "missing hidden achievements"
    still_missing = [a for a in hidden_names if not game_info["achievements"].get(a, {}).get("description")]
    if still_missing:
        with open(missing_path, "w", encoding="utf-8") as f:
            f.writelines(f"{a}\n" for a in still_missing)
        print(f"  ⚠ {len(still_missing)} hidden achievements still missing descriptions")
    elif missing_path.exists():
        missing_path.unlink()

    # ── Save game-info.json ────────────────────────────────────────────────────
    save_json_file(base / "game-info.json", game_info)

    # ── Load user achievement data (earned/unearned) ───────────────────────────
    raw_user_data, file_type = load_achievements_file(base)
    if raw_user_data is None:
        print(f"  ✗ No achievements file found for {appid}")
        continue
    print(f"  ✓ Loaded user data from {file_type}")

    # ── Remap CODEX/expand to full list ───────────────────────────────────────
    if file_type and file_type.endswith(".ini"):
        final_user_data = build_full_user_data(raw_user_data, metadata)
    else:
        final_user_data = raw_user_data

    existing_game_data[str(appid)] = {
        "appid":        str(appid),
        "info":         game_info,
        "achievements": final_user_data,
    }

# ─── Rebuild game-data.json ────────────────────────────────────────────────────
all_games = []
for folder in appid_dir.iterdir():
    if not (folder.is_dir() and folder.name.isdigit()):
        continue
    cid = folder.name
    user_data, _ = load_achievements_file(folder)
    if user_data is None:
        print(f"  ⚠ Skipping {cid} — no achievements file")
        continue
    if cid in existing_game_data:
        all_games.append(existing_game_data[cid])
    else:
        info = load_json_file(folder / "game-info.json") or {
            "appid": cid, "name": f"Game {cid}", "icon": "", "achievements": {}}
        all_games.append({"appid": cid, "info": info, "achievements": user_data})

save_json_file(game_data_path, {
    "last_updated": int(time.time()),
    "total_games":  len(all_games),
    "games":        all_games,
})
print(f"\n✓ Updated {len(appids)} game(s), total in data: {len(all_games)}")
