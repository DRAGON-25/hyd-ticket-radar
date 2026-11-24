import asyncio
import os
import re
from dotenv import load_dotenv
from rapidfuzz import fuzz
from playwright.async_api import async_playwright
from supabase import create_client

load_dotenv()
SUPABASE = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

BMS_CINEMAS_URL = "https://in.bookmyshow.com/hyderabad/cinemas"
DISTRICT_CINEMAS_URL = "https://www.district.in/movies/cinemas-in-hyderabad"
IS_CI = os.environ.get("GITHUB_ACTIONS") == "true"

def normalize_name(name: str) -> str:
    name = name.lower()
    for noise in ["hyderabad", "telangana", "70mm", "35mm", "4k", "laser", "dolby", "atmos", "a/c", "dts", "multiplex"]:
        name = name.replace(noise, "")
    return re.sub(r"[^a-z0-9]+", " ", name).strip()

async def new_stealth_page(browser):
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        viewport={"width": 1366, "height": 768},
        locale="en-IN",
    )
    await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
    return await context.new_page()

async def scroll_and_expand(page, max_scrolls=35):
    last_height = 0
    for _ in range(max_scrolls):
        # Click any button OR div/span acting as "Load More" / "Show More" / "View All"
        for label in ["Load More", "Show More", "View More", "See More", "View All"]:
            loc = page.locator(f"button:has-text('{label}'), div:text-is('{label}'), span:text-is('{label}')")
            if await loc.count() > 0:
                try:
                    if await loc.first.is_visible():
                        await loc.first.click()
                        await page.wait_for_timeout(1500)
                except Exception:
                    pass

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1200)
        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height

def extract_bms_venues_from_json(obj, found: dict):
    """Recursively scans BMS API JSON payloads for venue objects."""
    if isinstance(obj, dict):
        name = obj.get("VenueName") or obj.get("venueName") or obj.get("name")
        code = obj.get("VenueCode") or obj.get("venueCode")
        slug = obj.get("VenueSlug") or obj.get("slug") or obj.get("ctaUrl") or obj.get("url")
        if name and code and isinstance(name, str) and isinstance(code, str):
            if slug and str(slug).startswith("http"):
                url = str(slug).split("?")[0]
            elif slug and str(slug).startswith("/"):
                url = f"https://in.bookmyshow.com{slug}".split("?")[0]
            else:
                clean_slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
                url = f"https://in.bookmyshow.com/buytickets/{clean_slug}/cinema-hyd-{code}-MT"
            found[code] = {"name": name.strip(), "bms_url": url}
        for v in obj.values():
            extract_bms_venues_from_json(v, found)
    elif isinstance(obj, list):
        for item in obj:
            extract_bms_venues_from_json(item, found)

async def scrape_bms_theatres(browser):
    page = await new_stealth_page(browser)
    found = {}  # url -> {"name": ..., "bms_url": ...}

    async def collect_visible_links():
        links = await page.eval_on_selector_all(
            "a[href*='/cinemas/hyderabad/'], a[href*='/buytickets/']",
            "els => els.map(e => ({ name: e.innerText.trim().split('\\n')[0], url: e.href }))"
        )
        for item in links:
            url = item["url"].split("?")[0].rstrip("/")
            name = item["name"].strip()
            if (
                name
                and len(name) > 2
                and "/cinemas-list/" not in url
                and ("/cinemas/hyderabad/" in url or "/buytickets/" in url)
            ):
                found[url] = {"name": name, "bms_url": url}

    try:
        await page.goto(BMS_CINEMAS_URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(5000)
        print(f"[BMS] Page title: {await page.title()}")

        # 1. Check if BMS embedded full venue list in window.__INITIAL_STATE__
        init_state = await page.evaluate("() => window.__INITIAL_STATE__ || null")
        if init_state:
            json_venues = {}
            extract_bms_venues_from_json(init_state, json_venues)
            for v in json_venues.values():
                found[v["bms_url"]] = v

        # 2. Scroll incrementally to trigger infinite scroll & capture every card
        stagnant_steps = 0
        last_count = 0
        for _ in range(60):
            await collect_visible_links()
            await page.mouse.wheel(0, 900)
            await page.wait_for_timeout(700)

            if len(found) == last_count:
                stagnant_steps += 1
                if stagnant_steps >= 8:
                    break
            else:
                stagnant_steps = 0
                last_count = len(found)

        await collect_visible_links()
    finally:
        await page.close()

    theatres = list(found.values())
    print(f"Found {len(theatres)} Hyderabad venues on BookMyShow.")
    return theatres

async def scrape_district_theatres(browser):
    page = await new_stealth_page(browser)
    theatres = []
    try:
        await page.goto(DISTRICT_CINEMAS_URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(5000)
        print(f"[District] Page title: {await page.title()}")

        await scroll_and_expand(page, max_scrolls=40)

        links = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({ name: e.innerText.trim().split('\\n')[0], url: e.href }))"
        )
        seen_urls = set()
        # Exclude city/category/navigation links, keep all actual theatre pages
        skip_patterns = [
            "cinemas-in-", "upcoming-movies", "movie-tickets", "justickets",
            "/movies/hyderabad", "telugu-movies", "hindi-movies", "english-movies",
            "tamil-movies", "malayalam-movies", "kannada-movies"
        ]
        for item in links:
            url = item["url"].split("?")[0]
            name = item["name"].strip()
            url_l = url.lower()
            if (
                name
                and len(name) > 2
                and url not in seen_urls
                and "/movies/" in url_l
                and not url_l.endswith("/movies")
                and not url_l.endswith("/movies/")
                and not any(sp in url_l for sp in skip_patterns)
            ):
                seen_urls.add(url)
                theatres.append({"name": name, "district_url": url})
    finally:
        await page.close()

    print(f"Found {len(theatres)} Hyderabad venues on District.")
    return theatres

def merge_theatres(bms_list, district_list):
    merged = []
    for b in bms_list:
        key = normalize_name(b["name"])
        if not key:
            continue
        merged.append({
            "canonical_name": b["name"],
            "normalized_key": key,
            "city": "Hyderabad",
            "bms_url": b["bms_url"],
            "district_url": None,
            "source": "bms",
        })

    for d in district_list:
        d_key = normalize_name(d["name"])
        if not d_key:
            continue
        best_match, best_score = None, 0
        for row in merged:
            score = fuzz.token_set_ratio(d_key, row["normalized_key"])
            if score > best_score:
                best_match, best_score = row, score

        if best_match and best_score >= 82:
            best_match["district_url"] = d["district_url"]
            best_match["source"] = "both"
        else:
            merged.append({
                "canonical_name": d["name"],
                "normalized_key": d_key,
                "city": "Hyderabad",
                "bms_url": None,
                "district_url": d["district_url"],
                "source": "district",
            })

    # Final deduplication by normalized_key before Supabase upsert
    unique = {row["normalized_key"]: row for row in merged}
    return list(unique.values())

async def main():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                channel="chrome" if not IS_CI else None,
                headless=IS_CI,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception:
            browser = await p.chromium.launch(
                headless=IS_CI,
                args=["--disable-blink-features=AutomationControlled"],
            )

        bms_list, district_list = await asyncio.gather(
            scrape_bms_theatres(browser),
            scrape_district_theatres(browser),
        )
        await browser.close()

    unified = merge_theatres(bms_list, district_list)
    if unified:
        SUPABASE.table("theatres").upsert(unified, on_conflict="normalized_key").execute()
        print(f"Upserted {len(unified)} unified Hyderabad theatres.")

if __name__ == "__main__":
    asyncio.run(main())