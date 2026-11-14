"""
tools/events.py — Recent news and weather tool implementations.
News: GNews API. Weather: wttr.in (JSON).

Both news and weather results are run through a small PERSISTENT cache
(`events_cache.json` at the project root) so that:
  - repeated tool calls / dashboard loads don't hammer the upstream APIs,
  - the limited GNews daily budget (~100/day, shared with the agent) isn't
    burned by dev server restarts or identical repeat queries,
  - the UI dashboard and the model's own `recent_events` tool share ONE cache.

Layering (so a provider swap later only touches the fetch layer):
  _fetch_weather_raw(city)   — raw HTTP call, returns the wttr.in j1 dict (+hour)
  get_weather_cached(city)   — cache wrapper over the raw fetch (TTL 15 min)
  _format_weather(raw, city) — turn raw j1 into the model's text block
  get_weather_dashboard(city)— turn raw j1 into a compact JSON dict for the UI
  _weather_simple(city)      — the tool entry point (cached + text-formatted)

  _fetch_news_raw(query)     — raw GNews HTTP call, returns list[article dict]
  get_news_cached(query)     — cache wrapper over the raw fetch (TTL 60 min)
  _gnews(query)              — the tool entry point (cached + text-formatted)
  get_news_dashboard(query)  — cached articles as a JSON-friendly list for the UI
"""
import os
import json
import time
import asyncio
import logging
import datetime
from pathlib import Path

import httpx
from mcp import types

log = logging.getLogger(__name__)

GNEWS_API_KEY = os.getenv("GNEWS_API_KEY", "")

# ── Persistent cache ───────────────────────────────────────────────────────────
# A single JSON file at the project root (gitignored). Shape:
#   {
#     "weather": { "<city_key>": { "fetched_at": <epoch>, "raw": {...}, "hour": <int|null> } },
#     "news":    { "<query_key>": { "fetched_at": <epoch>, "articles": [ ... ] } }
#   }
# Weather entries store the raw wttr.in j1 dict so BOTH the text formatter (tool)
# and the compact dashboard JSON can be derived from one fetch.

CACHE_PATH = Path(__file__).resolve().parent.parent / "events_cache.json"

WEATHER_TTL = 15 * 60   # 15 minutes
NEWS_TTL = 60 * 60      # 60 minutes

# Guards against two concurrent refetches of the same kind stampeding the API.
_weather_lock = asyncio.Lock()
_news_lock = asyncio.Lock()


def _load_cache() -> dict:
    try:
        if CACHE_PATH.exists():
            data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("weather", {})
                data.setdefault("news", {})
                return data
    except Exception as exc:
        log.warning("events cache read failed: %s", exc)
    return {"weather": {}, "news": {}}


def _save_cache(cache: dict) -> None:
    try:
        tmp = CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CACHE_PATH)
    except Exception as exc:
        log.warning("events cache write failed: %s", exc)


def _norm_key(s: str) -> str:
    return "_".join((s or "").strip().lower().split()) or "_auto"


def _is_fresh(entry: dict | None, ttl: int) -> bool:
    return bool(entry) and (time.time() - entry.get("fetched_at", 0)) < ttl


def prune_stale_cache() -> int:
    """Drop every cache entry past its TTL and rewrite the file.

    Called once at server startup so one-off news-search queries (each keyed by
    its query string) don't accumulate forever — stale entries would never be
    revisited to be overwritten, so without a sweep the file grows unbounded.
    Fresh entries are kept so a restart still benefits from the warm cache.
    Returns the number of entries removed.
    """
    cache = _load_cache()
    removed = 0
    for bucket, ttl in (("weather", WEATHER_TTL), ("news", NEWS_TTL)):
        entries = cache.get(bucket, {})
        kept = {k: v for k, v in entries.items() if _is_fresh(v, ttl)}
        removed += len(entries) - len(kept)
        cache[bucket] = kept
    if removed:
        _save_cache(cache)
    return removed


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


# ── Weather: fetch layer ───────────────────────────────────────────────────────

async def _fetch_weather_raw(city: str) -> tuple[dict, int | None]:
    """Raw wttr.in call. Returns (j1_dict, current_local_hour_or_None).

    Isolated so swapping to a different weather provider later only touches this
    function (and the two formatters). An empty `city` lets wttr.in geolocate by
    IP.
    """
    url = f"https://wttr.in/{city}"
    headers = {"User-Agent": "curl/7.68.0"}

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, params={"format": "j1"}, headers=headers)
        r.raise_for_status()
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

    return data, curr_hour


async def get_weather_cached(city: str) -> tuple[dict, int | None]:
    """Return (raw_j1, local_hour), served from the persistent cache when fresh
    (<15 min) and refetched otherwise. Shared by the tool text formatter and the
    dashboard JSON builder so one fetch serves both."""
    key = _norm_key(city)
    cache = _load_cache()
    entry = cache["weather"].get(key)
    if _is_fresh(entry, WEATHER_TTL):
        return entry["raw"], entry.get("hour")

    async with _weather_lock:
        # Re-check after acquiring the lock — another coroutine may have just
        # refreshed it while we waited.
        cache = _load_cache()
        entry = cache["weather"].get(key)
        if _is_fresh(entry, WEATHER_TTL):
            return entry["raw"], entry.get("hour")

        raw, hour = await _fetch_weather_raw(city)
        cache["weather"][key] = {"fetched_at": time.time(), "raw": raw, "hour": hour}
        _save_cache(cache)
        return raw, hour


# ── Weather: formatters ─────────────────────────────────────────────────────────

def _format_weather(data: dict, curr_hour: int | None, city: str) -> str:
    """Build the model-facing text block from raw wttr.in j1 data."""
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
    return text


def get_weather_dashboard_from_raw(data: dict, city: str) -> dict:
    """Build the compact JSON the UI card needs from raw wttr.in j1 data."""
    curr = data["current_condition"][0]
    today = data["weather"][0] if data.get("weather") else {}
    area = ""
    try:
        area = data["nearest_area"][0]["areaName"][0]["value"].strip()
    except (KeyError, IndexError, TypeError):
        area = city or ""

    max_rain = 0
    try:
        max_rain = max(int(h["chanceofrain"]) for h in today.get("hourly", []))
    except (ValueError, KeyError):
        max_rain = 0

    return {
        "location": area or city or "your area",
        "temp_c": curr.get("temp_C"),
        "feels_c": curr.get("FeelsLikeC"),
        "desc": curr.get("weatherDesc", [{}])[0].get("value", "").strip(),
        "humidity": curr.get("humidity"),
        "wind_kmph": curr.get("windspeedKmph"),
        "wind_dir": curr.get("winddir16Point"),
        "hi_c": today.get("maxtempC"),
        "lo_c": today.get("mintempC"),
        "rain_chance": max_rain,
    }


# ── Weather: entry points ────────────────────────────────────────────────────

async def _weather_simple(city: str) -> list[types.TextContent]:
    """Model tool entry point — cached raw data, text-formatted."""
    try:
        data, curr_hour = await get_weather_cached(city)
        text = _format_weather(data, curr_hour, city)
        return [types.TextContent(type="text", text=text)]
    except Exception as e:
        log.info(e)
        return [types.TextContent(type="text", text=f"Error: {e}")]


async def get_weather_dashboard(city: str) -> dict:
    """Dashboard entry point — cached raw data, compact JSON."""
    data, _ = await get_weather_cached(city)
    return get_weather_dashboard_from_raw(data, city)


# ── News: fetch layer ───────────────────────────────────────────────────────────

async def _fetch_news_raw(query: str) -> list[dict]:
    """Raw GNews call. Returns the list of article dicts (possibly empty)."""
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
            "max": 10,
            "token": GNEWS_API_KEY,
        }
    else:
        url = "https://gnews.io/api/v4/search"
        params = {
            "q": query,
            "lang": "en",
            "max": 10,
            "token": GNEWS_API_KEY,
        }

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, params=params)
        data = r.json()

    return data.get("articles", []) or []


async def get_news_cached(query: str) -> list[dict]:
    """Return article dicts for a query, served from the persistent cache when
    fresh (<60 min) and refetched otherwise. Keyed per query since queries vary;
    this protects the shared ~100/day GNews budget across restarts and repeats."""
    key = _norm_key(query)
    cache = _load_cache()
    entry = cache["news"].get(key)
    if _is_fresh(entry, NEWS_TTL):
        return entry.get("articles", [])

    async with _news_lock:
        cache = _load_cache()
        entry = cache["news"].get(key)
        if _is_fresh(entry, NEWS_TTL):
            return entry.get("articles", [])

        articles = await _fetch_news_raw(query)
        # Only cache a non-empty success — don't poison the cache with an empty
        # result from a transient error / rate-limit, which would suppress
        # retries for a full hour.
        if articles:
            cache["news"][key] = {"fetched_at": time.time(), "articles": articles}
            _save_cache(cache)
        return articles


# ── News: entry points ───────────────────────────────────────────────────────

async def _gnews(query: str) -> list[types.TextContent]:
    articles = await get_news_cached(query)
    if not articles:
        from tools.web import _web_search
        today = datetime.date.today().isoformat()
        web = await _web_search(query, topic="news", time_range="week")
        web_text = web[0].text if web else "No results found."
        return [types.TextContent(type="text", text=(
            f"The news feed had nothing for '{query}' yet (free tier lags ~12h). "
            f"Live news-topic web results (past week, as of {today}) are below — "
            f"summarize them for the user:\n\n{web_text}"
        ))]
    text = "\n\n".join(
        f"{a.get('title', '')}\n{a.get('url', '')}\n{a.get('description', '')}"
        for a in articles
    )
    return [types.TextContent(type="text", text=text)]


async def get_news_dashboard(query: str, limit: int = 10) -> list[dict]:
    """Dashboard entry point — cached articles trimmed to the fields the UI uses."""
    articles = await get_news_cached(query)
    out = []
    for a in articles[:limit]:
        out.append({
            "title": a.get("title", ""),
            "url": a.get("url", ""),
            "description": a.get("description", ""),
            "source": (a.get("source") or {}).get("name", ""),
            "published_at": a.get("publishedAt", ""),
        })
    return out
