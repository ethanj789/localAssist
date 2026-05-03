"""
tools/events.py — Recent news and weather tool implementations.
News: GNews API. Weather: wttr.in (JSON).
"""
import os
import logging
import datetime
import httpx
from mcp import types

log = logging.getLogger(__name__)

GNEWS_API_KEY = os.getenv("GNEWS_API_KEY", "")


# ── recent_events dispatcher ──────────────────────────────────────────────────

async def _recent_events(infoType: str, details: str) -> list[types.TextContent]:
    log.info("%s, for %s", infoType, details)
    try:
        if infoType == "news":
            return await _gnews(details)
        elif infoType == "weather":
            return await _weather_simple(details)
    except Exception as e:
        return [types.TextContent(type="text", text=f"failed getting info on {infoType}': {e}")]


# ── Weather ───────────────────────────────────────────────────────────────────

async def _weather_simple(city: str) -> list[types.TextContent]:
    try:
        url = f"https://wttr.in/{city}"
        params = {"format": "j1"}
        headers = {"User-Agent": "curl/7.68.0"}

        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, params=params, headers=headers)
            if r.status_code != 200:
                return [types.TextContent(type="text", text="Weather fetch failed.")]
            data = r.json()

        # Current weather
        curr = data["current_condition"][0]

        # Get local time to find future slots (0-23)
        # localObsDateTime looks like "2023-10-27 10:15 AM"
        obs_time = datetime.datetime.strptime(curr["localObsDateTime"], "%Y-%m-%d %I:%M %p")
        curr_hour = obs_time.hour

        def get_hourly_data(target_hour):
            # Slots are every 3 hours: 0, 3, 6, 9, 12, 15, 18, 21
            day_offset = target_hour // 24
            hour_in_day = target_hour % 24
            slot_idx = min(hour_in_day // 3, 7)  # Round down to nearest 3-hour slot

            day_data = data["weather"][day_offset]
            hour_data = day_data["hourly"][slot_idx]
            return f"{hour_data['tempC']}°C & {hour_data['weatherDesc'][0]['value']}"

        return [
            types.TextContent(
                type="text",
                text=(
                    f"Weather for {city}:\n"
                    f"NOW: {curr['temp_C']}°C, {curr['weatherDesc'][0]['value']}\n"
                    f"+6H:  {get_hourly_data(curr_hour + 6)}\n"
                    f"+12H: {get_hourly_data(curr_hour + 12)}\n"
                    f"+18H: {get_hourly_data(curr_hour + 18)}\n"
                    f"+24H: {get_hourly_data(curr_hour + 24)}"
                )
            )
        ]
    except Exception as e:
        return [types.TextContent(type="text", text=f"Error: {e}")]


# ── News ──────────────────────────────────────────────────────────────────────

async def _gnews(query: str) -> list[types.TextContent]:
    query_clean = query.strip().lower()

    general_categories = {
        "technology", "sports", "business", "health",
        "science", "entertainment", "world", "nation",
        "general"
    }

    use_headlines = query_clean in general_categories

    if use_headlines:
        url = "https://gnews.io/api/v4/top-headlines"
        params = {
            "category": query_clean if query_clean in general_categories else "general",
            "lang": "en",
            "max": 5,
            "token": GNEWS_API_KEY
        }
    else:
        url = "https://gnews.io/api/v4/search"
        params = {
            "q": query,
            "lang": "en",
            "max": 10,
            "token": GNEWS_API_KEY
        }

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, params=params)
        data = r.json()

    articles = data.get("articles", [])

    text = "\n\n".join(
        f"{a.get('title', '')}\n{a.get('url', '')}\n{a.get('description', '')}"
        for a in articles
    )

    return [types.TextContent(type="text", text=text)]
