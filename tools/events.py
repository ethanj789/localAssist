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

            # The j1 JSON carries no timezone, and current_condition's
            # observation_time is UTC. wttr.in's one-line "%T" format returns the
            # location's local time with offset (e.g. "01:34:34-0500"), which we
            # use to align the (local-time) hourly slots to the right clock hour.
            curr_hour = None
            try:
                tr = await client.get(url, params={"format": "%T"}, headers=headers)
                if tr.status_code == 200:
                    curr_hour = int(tr.text.strip()[:2])
            except (httpx.HTTPError, ValueError):
                curr_hour = None

        # Current weather
        curr = data["current_condition"][0]

        if curr_hour is None:
            # Fallback: observation_time is UTC "HH:MM AM/PM" — not local, so
            # slot labels may be off, but better than failing outright.
            curr_hour = datetime.datetime.strptime(
                curr["observation_time"].strip(), "%I:%M %p"
            ).hour

        # Each day has 8 hourly slots, one per 3 hours: 0,3,6,...,21.
        SLOTS_PER_DAY = 8

        def fmt_block(hour_from_now: int) -> str | None:
            """Format the 3-hour slot `hour_from_now` hours ahead of now."""
            abs_hour = curr_hour + hour_from_now
            day_offset = abs_hour // 24
            if day_offset >= len(data["weather"]):
                return None
            slot_idx = min((abs_hour % 24) // 3, SLOTS_PER_DAY - 1)
            h = data["weather"][day_offset]["hourly"][slot_idx]

            label = f"{(abs_hour % 24):02d}:00"
            if day_offset == 1:
                label += " (tmrw)"
            desc = h["weatherDesc"][0]["value"].strip()
            return (
                f"{label}  {h['tempC']}°C (feels {h['FeelsLikeC']}°C)  "
                f"{desc}, wind {h['windspeedKmph']}km/h {h['winddir16Point']}, "
                f"rain {h['chanceofrain']}%"
            )

        # Next 24h in 3-hour blocks (8 blocks, starting at the next slot).
        start = ((curr_hour // 3) + 1) * 3  # next 3-hour boundary after now
        blocks = []
        for i in range(SLOTS_PER_DAY):
            line = fmt_block(start - curr_hour + i * 3)
            if line:
                blocks.append(line)

        # Daily summaries for the remaining forecast days.
        def fmt_day(day_offset: int) -> str | None:
            if day_offset >= len(data["weather"]):
                return None
            d = data["weather"][day_offset]
            # Day-level condition: use the midday (12:00) slot's description.
            midday = d["hourly"][min(4, len(d["hourly"]) - 1)]
            desc = midday["weatherDesc"][0]["value"].strip()
            max_rain = max(int(h["chanceofrain"]) for h in d["hourly"])
            return (
                f"{d['date']}: {d['mintempC']}–{d['maxtempC']}°C, "
                f"{desc}, rain up to {max_rain}%"
            )

        day_lines = [s for off in (1, 2) if (s := fmt_day(off))]

        now_line = (
            f"NOW: {curr['temp_C']}°C (feels {curr['FeelsLikeC']}°C), "
            f"{curr['weatherDesc'][0]['value'].strip()}, "
            f"wind {curr['windspeedKmph']}km/h {curr['winddir16Point']}, "
            f"humidity {curr['humidity']}%"
        )

        text = f"Weather for {city}:\n{now_line}\n\nNext 24h (3-hour blocks):\n"
        text += "\n".join(blocks)
        if day_lines:
            text += "\n\nFollowing days:\n" + "\n".join(day_lines)

        return [types.TextContent(type="text", text=text)]
    except Exception as e:
        log.info(e)
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
