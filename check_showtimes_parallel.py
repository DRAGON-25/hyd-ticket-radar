import asyncio
import os
import re
from collections import defaultdict
from datetime import date
import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from supabase import create_client

load_dotenv()
SUPABASE = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
IS_CI = os.environ.get("GITHUB_ACTIONS") == "true"

TIME_REGEX = re.compile(r"\b(0?[1-9]|1[0-2]):[0-5]\d\s?(?:AM|PM)\b", re.IGNORECASE)

def clean_title(text: str) -> str:
    """Normalizes punctuation and spacing so TMDB vs BMS/District titles match cleanly."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

def send_alert(platform: str, movie: str, theatre: str, target_date: str, new_times: list[str], click_url: str):
    label = "BookMyShow" if platform == "bms" else "District"
    times_str = ", ".join(sorted(new_times))
    body = f"{movie}\n{theatre} ({target_date})\nNew shows: {times_str}"
    res = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={
            "Title": f"[{label}] Tickets LIVE!",
            "Priority": "urgent",
            "Tags": "ticket,movie_camera",
            "Click": click_url,
        },
        timeout=10,
    )
    print(f"[{platform}] ntfy status={res.status_code} | sent: {times_str}")

def format_bms_date_url(base_url: str, target_date: str) -> str:
    ymd = target_date.replace("-", "")
    m = re.search(r"/cinemas/hyderabad/([^/]+)/([A-Za-z0-9]+)/?$", base_url.rstrip("/"))
    if m:
        slug, code = m.group(1), m.group(2).upper()
        return f"https://in.bookmyshow.com/buytickets/{slug}/cinema-hyd-{code}-MT/{ymd}"
    cleaned = re.sub(r"/\d{8}/?$", "", base_url.rstrip("/"))
    return f"{cleaned}/{ymd}"

def format_district_date_url(base_url: str, target_date: str) -> str:
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}fromdate={target_date}"

def extract_movie_showtimes(page_text: str, movie_title: str) -> tuple[bool, list[str]]:
    """
    Returns (movie_matched, sorted_showtimes).
    Stops scanning once it moves past the movie's showtime block to avoid bleeding into the next movie.
    """
    lines = [line.strip() for line in page_text.splitlines() if line.strip()]
    target = clean_title(movie_title)
    found_times = set()
    matched = False

    for idx, line in enumerate(lines):
        if target and target in clean_title(line):
            matched = True
            seen_any_time = False
            for w in lines[idx + 1 : idx + 35]:
                times_in_line = [m.group(0).upper().replace(" ", "") for m in TIME_REGEX.finditer(w)]
                if times_in_line:
                    seen_any_time = True
                    found_times.update(times_in_line)
                elif seen_any_time and len(w) > 12 and not any(k in w.lower() for k in ["dolby", "laser", "atmos", "imax", "pcx", "4k", "3d", "2d", "telugu", "hindi", "english", "tamil", "malayalam", "cancellable", "fast filling", "almost full"]):
                    # Likely hit the next movie's title
                    break

    return matched, sorted(found_times)

async def check_single_platform(browser, platform: str, url: str, watches: list[dict]):
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        locale="en-IN",
    )
    await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
    page = await context.new_page()

    try:
        print(f"[{platform}] Opening: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=35000)
        await page.wait_for_timeout(5000)
        page_title = await page.title()
        body_text = await page.inner_text("body")
        print(f"[{platform}] Loaded title: '{page_title}' (text length: {len(body_text)})")
    except Exception as e:
        print(f"[{platform}] ERROR loading {url}: {e}")
        await context.close()
        return
    await context.close()

    for w in watches:
        theatre_name = w["theatres"]["canonical_name"]
        movie_title = w["movie_title"]
        matched, live_times = extract_movie_showtimes(body_text, movie_title)
        print(f"[{platform}] '{movie_title}' @ '{theatre_name}' -> matched={matched}, times={live_times}")

        if not live_times:
            continue

        existing_res = (
            SUPABASE.table("seen_shows")
            .select("normalized_time")
            .eq("watchlist_id", w["id"])
            .eq("detected_on", platform)
            .execute()
        )
        already_seen = {r["normalized_time"] for r in existing_res.data}
        new_times = [t for t in live_times if t not in already_seen]

        if new_times:
            rows = [{"watchlist_id": w["id"], "normalized_time": t, "detected_on": platform} for t in new_times]
            SUPABASE.table("seen_shows").upsert(rows, on_conflict="watchlist_id,detected_on,normalized_time").execute()
            send_alert(platform, movie_title, theatre_name, w["target_date"], new_times, url)
        else:
            print(f"[{platform}] All {len(live_times)} showtime(s) already notified. Skipping.")

async def main():
    today_str = date.today().isoformat()
    res = (
        SUPABASE.table("watchlists")
        .select("id, theatre_id, movie_title, target_date, theatres(canonical_name, bms_url, district_url)")
        .eq("is_active", True)
        .gte("target_date", today_str)
        .execute()
    )
    if not res.data:
        print("No active watchlists for today or future dates. Exiting.")
        return

    grouped = defaultdict(list)
    for row in res.data:
        grouped[(row["theatre_id"], row["target_date"])].append(row)

    print(f"Found {len(res.data)} active watch rule(s) across {len(grouped)} theatre-date group(s).")

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

        tasks = []
        for (_, target_date), watches in grouped.items():
            theatre = watches[0]["theatres"]
            if theatre.get("bms_url"):
                tasks.append(check_single_platform(browser, "bms", format_bms_date_url(theatre["bms_url"], target_date), watches))
            if theatre.get("district_url"):
                tasks.append(check_single_platform(browser, "district", format_district_date_url(theatre["district_url"], target_date), watches))

        await asyncio.gather(*tasks, return_exceptions=True)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())