import os
from datetime import date, timedelta
import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
SUPABASE = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
TMDB_TOKEN = os.environ["TMDB_READ_TOKEN"].strip()

def tmdb_get(path: str, params: dict) -> dict:
    url = f"https://api.themoviedb.org/3{path}"
    headers = {"accept": "application/json"}
    query = dict(params)

    # Support both v4 Read Access Token (JWT) and v3 API Key automatically
    if len(TMDB_TOKEN) > 50:
        headers["Authorization"] = f"Bearer {TMDB_TOKEN}"
    else:
        query["api_key"] = TMDB_TOKEN

    res = requests.get(url, headers=headers, params=query, timeout=20)
    if res.status_code != 200:
        raise RuntimeError(f"TMDB error {res.status_code} on {path}: {res.text[:200]}")
    return res.json()

def parse_movies(results: list, status: str) -> list[dict]:
    rows = []
    for m in results:
        rows.append({
            "tmdb_id": m["id"],
            "title": m["title"],
            "original_title": m.get("original_title", m["title"]),
            "language": m.get("original_language", "unknown"),
            "release_date": m.get("release_date") or None,
            "status": status,
        })
    return rows

def fetch_running(pages: int = 3) -> list[dict]:
    rows = []
    for page in range(1, pages + 1):
        data = tmdb_get("/movie/now_playing", {"region": "IN", "page": page})
        rows.extend(parse_movies(data.get("results", []), "running"))
    return rows

def fetch_upcoming(pages: int = 5) -> list[dict]:
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    in_90_days = (date.today() + timedelta(days=90)).isoformat()
    rows = []

    # 1. Discover upcoming Telugu, Hindi, English, Tamil, Malayalam, Kannada releases
    for page in range(1, pages + 1):
        data = tmdb_get(
            "/discover/movie",
            {
                "primary_release_date.gte": tomorrow,
                "primary_release_date.lte": in_90_days,
                "with_original_language": "te|hi|en|ta|ml|kn",
                "sort_by": "popularity.desc",
                "page": page,
            },
        )
        rows.extend(parse_movies(data.get("results", []), "upcoming"))
        if page >= data.get("total_pages", 1):
            break

    # 2. Also pull TMDB's official India upcoming list as a bonus
    for page in range(1, 3):
        data = tmdb_get("/movie/upcoming", {"region": "IN", "page": page})
        rows.extend(parse_movies(data.get("results", []), "upcoming"))

    return rows

if __name__ == "__main__":
    running = fetch_running(pages=3)
    upcoming = fetch_upcoming(pages=5)

    # Deduplicate by tmdb_id (running takes priority if a movie is in both)
    unique = {}
    for m in upcoming:
        unique[m["tmdb_id"]] = m
    for m in running:
        unique[m["tmdb_id"]] = m

    final_list = list(unique.values())
    running_count = sum(1 for m in final_list if m["status"] == "running")
    upcoming_count = sum(1 for m in final_list if m["status"] == "upcoming")

    if final_list:
        SUPABASE.table("movies").upsert(final_list, on_conflict="tmdb_id").execute()

    print(f"Synced {len(final_list)} movies (Running: {running_count}, Upcoming: {upcoming_count}).")