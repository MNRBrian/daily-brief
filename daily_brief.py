#!/usr/bin/env python3
"""
Daily HTML email brief with deterministic data + Ollama summaries.

Deterministic:
- Weather (Open-Meteo)
- Markets (Stooq + CoinGecko)
- Calendars (ICS)
- Bible (BibleGateway VOTD + bible-api)
- Sports (ESPN scoreboards, MN teams, last night)
- Headlines (RSS)

Ollama-only:
- POTUS/Admin summary from WhiteHouse.gov headlines
- News themes summary from collected headlines
- Full brief wrap-up summary from all collected sections
"""

from __future__ import annotations

import html
import json
import re
import ssl
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote as urlquote
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo


# ----------------------------- Config ----------------------------- #

TZ = ZoneInfo("America/Chicago")
HTTP_TIMEOUT = 25
RSS_LIMIT = 5

TO_EMAIL = "brian.overton@ymail.com"
FROM_EMAIL = "brian.overton@ymail.com"
SUBJECT_PREFIX = "AI Daily Brief"

OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_MODEL = "gemma3:latest"

WEATHER_LAT = 45.3036
WEATHER_LON = -93.5672
WEATHER_TZ = "America/Chicago"

CAL_O365 = "https://outlook.office365.com/owa/calendar/cad449d11c4c42c28cfb707c54bda628@mnrealtor.com/ed51571060f54e7e9764ef30eb168b9313742464364861286019/S-1-8-2812003574-1231967269-709150430-2363919127/reachcalendar.ics"
CAL_GOOGLE = "https://calendar.google.com/calendar/ical/boverton%40northstarmls.com/public/basic.ics"

RSS_FEEDS = [
    ("NY Times (Home)", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
    ("Daily Wire", "https://www.dailywire.com/feeds/rss.xml"),
    ("9to5Mac", "https://9to5mac.com/feed/"),
]

WHITEHOUSE_BRIEFINGS = "https://www.whitehouse.gov/briefings-statements/"
WHITEHOUSE_NEWS = "https://www.whitehouse.gov/news/"

MN_TEAMS = {
    "nba": {"Minnesota Timberwolves", "Timberwolves", "MIN"},
    "nhl": {"Minnesota Wild", "Wild", "MIN"},
    "mlb": {"Minnesota Twins", "Twins", "MIN"},
    "nfl": {"Minnesota Vikings", "Vikings", "MIN"},
}

ESPN_SCOREBOARD = {
    "nba": "https://site.api.espn.com/apis/v2/sports/basketball/nba/scoreboard",
    "nhl": "https://site.api.espn.com/apis/v2/sports/hockey/nhl/scoreboard",
    "mlb": "https://site.api.espn.com/apis/v2/sports/baseball/mlb/scoreboard",
    "nfl": "https://site.api.espn.com/apis/v2/sports/football/nfl/scoreboard",
}

BIBLE_VOTD_ATOM = "https://www.biblegateway.com/votd/get/?format=atom"
BIBLE_API_BASE = "https://bible-api.com/"


# ----------------------------- Utils ------------------------------ #

def log(msg: str) -> None:
    print(msg, flush=True)


def http_get(url: str, timeout: int = HTTP_TIMEOUT, headers: dict | None = None) -> bytes:
    hdrs = {"User-Agent": "daily-brief/2.0", "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    req = Request(url, headers=hdrs)
    ctx = ssl.create_default_context()
    with urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read()


def safe_json(payload: bytes) -> dict:
    return json.loads(payload.decode("utf-8", errors="replace"))


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def compact_text(text: str) -> str:
    return " ".join(strip_tags(text).split())


def html_link(title: str, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(title)}</a>'


def fmt_dt_local(value: datetime) -> str:
    return value.astimezone(TZ).strftime("%a %b %-d, %-I:%M %p")


def fmt_date_local(value: datetime) -> str:
    return value.astimezone(TZ).strftime("%A, %B %-d, %Y")


def c_to_f(celsius: float | None) -> float | None:
    if celsius is None:
        return None
    return celsius * 9 / 5 + 32


def section_error(title: str, err: Exception) -> str:
    return f"<h2>{html.escape(title)}</h2><p><i>Failed:</i> {html.escape(str(err))}</p>"


# ----------------------------- Ollama ----------------------------- #

def ollama_generate(prompt: str, temperature: float = 0.3) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "top_p": 0.9},
    }
    req = Request(
        f"{OLLAMA_URL}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    ctx = ssl.create_default_context()
    with urlopen(req, timeout=240, context=ctx) as resp:
        out = resp.read()
    return (safe_json(out).get("response") or "").strip()


def lines_to_ul(lines: list[str]) -> str:
    items = "".join(f"<li>{html.escape(line)}</li>" for line in lines if line.strip())
    return f"<ul>{items}</ul>" if items else ""


def clean_llm_section(text: str, title: str) -> str:
    if not text:
        return f"<h2>{html.escape(title)}</h2><p><i>Summary unavailable.</i></p>"

    text = re.sub(r"```(?:html)?\s*", "", text, flags=re.I).replace("```", "").strip()
    if "<ul" in text and "<li" in text:
        return text if "<h2" in text.lower() else f"<h2>{html.escape(title)}</h2>\n{text}"

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lines = [ln for ln in lines if ln.lower() not in {title.lower(), f"{title.lower()}:"}]
    lines = [ln.lstrip("•*- \t") for ln in lines]
    ul = lines_to_ul(lines[:12])

    if not ul:
        return f"<h2>{html.escape(title)}</h2><p><i>Summary unavailable.</i></p>"
    return f"<h2>{html.escape(title)}</h2>\n{ul}"


def clean_llm_paragraph_section(text: str, title: str) -> str:
    if not text:
        return f"<h2>{html.escape(title)}</h2><p><i>Summary unavailable.</i></p>"

    text = re.sub(r"```(?:html)?\s*", "", text, flags=re.I).replace("```", "").strip()

    # Normalize list-style output into plain text first.
    text = re.sub(r"(?is)</li>\s*<li[^>]*>", " ", text)
    text = re.sub(r"(?is)<li[^>]*>", "", text)
    text = re.sub(r"(?is)</li>", " ", text)
    text = re.sub(r"(?is)</p>\s*<p[^>]*>", " ", text)
    text = strip_tags(text)

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lines = [ln for ln in lines if ln.lower() not in {title.lower(), f"{title.lower()}:"}]
    cleaned_lines: list[str] = []
    for ln in lines:
        ln = ln.lstrip("•*- \t")
        ln = re.sub(r"^\[(?:NOW|TODAY|WATCH)\]\s*", "", ln, flags=re.I)
        cleaned_lines.append(ln)

    merged = " ".join(cleaned_lines).strip()
    merged = re.sub(r"\s+", " ", merged)
    if not merged:
        return f"<h2>{html.escape(title)}</h2><p><i>Summary unavailable.</i></p>"
    return f"<h2>{html.escape(title)}</h2>\n<p>{html.escape(merged)}</p>"


# ----------------------------- Weather ---------------------------- #

def get_weather_realtime() -> dict:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
        "&current_weather=true"
        "&hourly=temperature_2m,precipitation_probability,wind_speed_10m"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        f"&timezone={WEATHER_TZ}"
    )
    return safe_json(http_get(url))


def render_weather(weather: dict) -> str:
    cur = weather.get("current_weather", {}) or {}
    daily = weather.get("daily", {}) or {}
    hourly = weather.get("hourly", {}) or {}

    cur_temp_f = c_to_f(cur.get("temperature"))
    tmax_f = c_to_f((daily.get("temperature_2m_max") or [None])[0])
    tmin_f = c_to_f((daily.get("temperature_2m_min") or [None])[0])

    out = ["<h2>Weather — Elk River, MN (real-time)</h2>", "<ul>"]
    if cur.get("time"):
        out.append(f"<li><b>As of:</b> {html.escape(cur['time'])} (local)</li>")
    if cur_temp_f is not None:
        out.append(f"<li><b>Now:</b> {cur_temp_f:.1f}°F</li>")
    if tmin_f is not None and tmax_f is not None:
        out.append(f"<li><b>Today:</b> {tmin_f:.1f}°F – {tmax_f:.1f}°F</li>")
    if (pmax := (daily.get("precipitation_probability_max") or [None])[0]) is not None:
        out.append(f"<li><b>Precip chance (max today):</b> {int(pmax)}%</li>")
    if (wind := cur.get("windspeed")) is not None:
        out.append(f"<li><b>Wind:</b> {wind:.0f} km/h</li>")
    if (code := cur.get("weathercode")) is not None:
        out.append(f"<li><b>Weather code:</b> {code}</li>")
    out.append("</ul>")

    now = datetime.now(TZ)
    rows = []
    h_times = hourly.get("time") or []
    h_temps = hourly.get("temperature_2m") or []
    h_pop = hourly.get("precipitation_probability") or []
    h_wind = hourly.get("wind_speed_10m") or []

    for i, ts in enumerate(h_times):
        try:
            dth = datetime.fromisoformat(ts).replace(tzinfo=TZ)
        except Exception:
            continue
        if dth < now:
            continue
        rows.append((
            dth,
            c_to_f(h_temps[i]) if i < len(h_temps) else None,
            h_pop[i] if i < len(h_pop) else None,
            h_wind[i] if i < len(h_wind) else None,
        ))
        if len(rows) >= 3:
            break

    if rows:
        out.append("<h3>Next few hours</h3>")
        out.append('<table border="1" cellpadding="6" cellspacing="0">')
        out.append("<tr><th>Time</th><th>Temp (°F)</th><th>Precip %</th><th>Wind (km/h)</th></tr>")
        for dth, tf, pop, wind in rows:
            out.append(
                "<tr>"
                f"<td>{html.escape(dth.strftime('%-I:%M %p'))}</td>"
                f"<td>{'' if tf is None else f'{tf:.1f}'}</td>"
                f"<td>{'' if pop is None else int(pop)}</td>"
                f"<td>{'' if wind is None else f'{wind:.0f}'}</td>"
                "</tr>"
            )
        out.append("</table>")

    return "\n".join(out)


# ----------------------------- Markets ---------------------------- #

def stooq_last(symbol: str) -> float | None:
    url = f"https://stooq.com/q/l/?s={urlquote(symbol)}&f=sd2t2ohlcv&h&e=csv"
    lines = http_get(url).decode("utf-8", errors="replace").strip().splitlines()
    if len(lines) < 2:
        return None
    keys = lines[0].split(",")
    vals = lines[1].split(",")
    close = dict(zip(keys, vals)).get("Close") or ""
    try:
        value = float(close)
        return None if value == 0.0 else value
    except Exception:
        return None


def get_xrp_usd() -> tuple[float | None, float | None]:
    url = "https://api.coingecko.com/api/v3/simple/price?ids=ripple&vs_currencies=usd&include_24hr_change=true"
    ripple = safe_json(http_get(url)).get("ripple") or {}
    try:
        price = float(ripple["usd"]) if ripple.get("usd") is not None else None
    except Exception:
        price = None
    try:
        change = float(ripple["usd_24h_change"]) if ripple.get("usd_24h_change") is not None else None
    except Exception:
        change = None
    return price, change


def render_markets() -> str:
    ndq = stooq_last("^ndq")
    dji = stooq_last("^dji")
    xrp_price, xrp_change = get_xrp_usd()

    out = ["<h2>Markets</h2>", "<ul>"]
    out.append(f"<li><b>NASDAQ (Composite):</b> {f'{ndq:,.2f}' if ndq is not None else '(unavailable)'}</li>")
    out.append(f"<li><b>Dow (DJIA):</b> {f'{dji:,.2f}' if dji is not None else '(unavailable)'}</li>")
    if xrp_price is None:
        out.append("<li><b>XRP (USD):</b> (unavailable)</li>")
    else:
        suffix = f" ({xrp_change:+.2f}% 24h)" if xrp_change is not None else ""
        out.append(f"<li><b>XRP (USD):</b> {xrp_price:,.4f}{suffix}</li>")
    out.append("</ul>")
    return "\n".join(out)


# ----------------------------- RSS ---------------------------- #

def parse_rss_items(xml_bytes: bytes, limit: int) -> list[tuple[str, str]]:
    root = ET.fromstring(xml_bytes.decode("utf-8", errors="replace"))
    items: list[tuple[str, str]] = []

    if root.tag.endswith("feed"):
        for entry in root.findall(".//{*}entry"):
            title = (entry.findtext("{*}title") or "").strip()
            link = next((l.attrib.get("href", "") for l in entry.findall("{*}link") if l.attrib.get("href")), "")
            if title and link:
                items.append((title, link))
            if len(items) >= limit:
                break
        return items

    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if title and link:
            items.append((title, link))
        if len(items) >= limit:
            break
    return items


def render_rss_block(name: str, url: str, limit: int) -> tuple[str, list[tuple[str, str]]]:
    out = [f"<h3>{html.escape(name)}</h3>"]
    try:
        items = parse_rss_items(http_get(url), limit)
    except Exception as err:
        return "\n".join(out + [f"<p><i>Failed to load feed:</i> {html.escape(str(err))}</p>"]), []

    if not items:
        return "\n".join(out + ["<p><i>No items found.</i></p>"]), []

    out.append("<ol>")
    out.extend(f"<li>{html_link(title, link)}</li>" for title, link in items[:limit])
    out.append("</ol>")
    return "\n".join(out), items[:limit]


# ----------------------------- Calendar --------------------------- #

@dataclass
class CalEvent:
    start: datetime
    end: datetime | None
    summary: str
    location: str | None


def unfold_ics_lines(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith((" ", "\t")) and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def parse_ics_events(ics_bytes: bytes, window_hours: int = 24) -> list[CalEvent]:
    now = datetime.now(TZ)
    end_window = now + timedelta(hours=window_hours)
    events: list[CalEvent] = []
    current: dict[str, str] | None = None

    def parse_dt(raw_key: str, value: str) -> datetime | None:
        tzid = None
        m = re.search(r";TZID=([^;]+)", raw_key, flags=re.I)
        if m:
            tzid = m.group(1)

        value = value.strip()
        if value.endswith("Z"):
            try:
                return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)
            except Exception:
                return None
        if re.fullmatch(r"\d{8}", value):
            try:
                return datetime.strptime(value, "%Y%m%d").replace(tzinfo=TZ)
            except Exception:
                return None

        for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
            try:
                tz = ZoneInfo(tzid) if tzid else TZ
            except Exception:
                tz = TZ
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=tz).astimezone(TZ)
            except Exception:
                pass
        return None

    for line in unfold_ics_lines(ics_bytes.decode("utf-8", errors="replace")):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT" and current is not None:
            start = end = None
            summary = ""
            location = None

            for key, value in current.items():
                upper = key.upper()
                if upper.startswith("DTSTART"):
                    start = parse_dt(key, value)
                elif upper.startswith("DTEND"):
                    end = parse_dt(key, value)
                elif upper == "SUMMARY":
                    summary = value.strip()
                elif upper == "LOCATION":
                    location = value.strip() or None

            if start and summary and (now - timedelta(hours=1)) <= start <= end_window:
                events.append(CalEvent(start=start, end=end, summary=summary, location=location))
            current = None
            continue

        if current is not None and ":" in line:
            key, value = line.split(":", 1)
            current[key] = value

    return sorted(events, key=lambda e: e.start)


def render_calendar_block() -> str:
    out = ["<h2>Calendar — Next 24 hours</h2>"]
    sources = [("MNR Work Calendar", CAL_O365), ("NorthstarMLS (Public)", CAL_GOOGLE)]

    for name, url in sources:
        out.append(f"<h3>{html.escape(name)}</h3>")
        try:
            events = parse_ics_events(http_get(url), window_hours=24)
        except Exception as err:
            out.append(f"<p><i>Failed to load calendar:</i> {html.escape(str(err))}</p>")
            continue

        if not events:
            out.append("<p><i>No events in the next 24 hours.</i></p>")
            continue

        out.append("<ul>")
        for event in events:
            loc = f" — {html.escape(event.location)}" if event.location else ""
            out.append(f"<li><b>{html.escape(fmt_dt_local(event.start))}</b>: {html.escape(event.summary)}{loc}</li>")
        out.append("</ul>")

    return "\n".join(out)


# ----------------------------- Bible ------------------------------ #

def get_bible_votd() -> tuple[str, str, str]:
    root = ET.fromstring(http_get(BIBLE_VOTD_ATOM).decode("utf-8", errors="replace"))
    entry = root.find(".//{*}entry")
    if entry is None:
        return "(unavailable)", "", ""

    title = (entry.findtext("{*}title") or "").strip()
    summary_html = (entry.findtext("{*}summary") or "").strip()
    text = html.unescape(strip_tags(summary_html))

    m = re.match(r"^(.+?)\s+\d+:\d+", title)
    book = m.group(1).strip() if m else title
    return title, text, book


def get_more_verses_same_book(book: str, count: int = 2) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    if not book:
        return results

    for chapter in range(1, 11):
        url = f"{BIBLE_API_BASE}{urlquote(f'{book} {chapter}') }"
        try:
            verses = (safe_json(http_get(url)).get("verses") or [])
        except Exception:
            continue

        for verse in verses:
            ref = f"{verse.get('book_name','')} {verse.get('chapter','')}:{verse.get('verse','')}".strip()
            text = " ".join((verse.get("text") or "").split())
            if ref and text:
                results.append((ref, text))
            if len(results) >= count:
                return results
    return results[:count]


def render_bible_block() -> str:
    ref, text, book = get_bible_votd()
    out = ["<h2>Bible</h2>", f"<p><b>Verse of the Day:</b> {html.escape(ref)}<br>{html.escape(text)}</p>"]

    more = get_more_verses_same_book(book, count=2)
    if more:
        out.append("<p><b>2 more from the same book:</b></p><ul>")
        out.extend(f"<li><b>{html.escape(ref_)}</b> — {html.escape(txt)}</li>" for ref_, txt in more)
        out.append("</ul>")
    else:
        out.append("<p><i>Could not fetch 2 additional verses today.</i></p>")

    return "\n".join(out)


# ----------------------------- Sports ----------------------------- #

def get_scoreboard(league: str, yyyymmdd: str) -> dict:
    return safe_json(http_get(f"{ESPN_SCOREBOARD[league]}?dates={yyyymmdd}"))


def render_sports_block() -> str:
    out = ["<h2>Sports — Last Night (MN teams only)</h2>"]
    target_date = (datetime.now(TZ) - timedelta(days=1)).strftime("%Y%m%d")
    found_any = False

    for league in ("nba", "nhl", "mlb", "nfl"):
        try:
            events = (get_scoreboard(league, target_date).get("events") or [])
        except Exception:
            continue

        hits: list[tuple[str, str]] = []
        for event in events:
            comps = event.get("competitions") or []
            if not comps:
                continue
            comp = comps[0]
            competitors = comp.get("competitors") or []
            if len(competitors) < 2:
                continue

            team_names = [(c.get("team") or {}).get("displayName") or "" for c in competitors]
            abbrevs = [(c.get("team") or {}).get("abbreviation") or "" for c in competitors]
            if not any((name in MN_TEAMS[league]) or (abbr in MN_TEAMS[league]) for name, abbr in zip(team_names, abbrevs)):
                continue

            detail = (((comp.get("status") or {}).get("type") or {}).get("shortDetail")) or ""
            headline = event.get("name") or "Game"
            line = f"{headline} — {detail}".strip(" —")

            scores = [c.get("score") for c in competitors]
            if scores[0] is not None and scores[1] is not None:
                line += f" (Score: {abbrevs[0]} {scores[0]} — {abbrevs[1]} {scores[1]})"

            link = ((event.get("links") or [{}])[0].get("href") or "")
            hits.append((line, link))

        if hits:
            found_any = True
            out.append(f"<h3>{league.upper()}</h3><ul>")
            out.extend(f"<li>{html_link(line, link) if link else html.escape(line)}</li>" for line, link in hits)
            out.append("</ul>")

    if not found_any:
        out.append("<p><i>No MN-team games found last night.</i></p>")
    return "\n".join(out)


# -------------------------- White House + LLM --------------------- #

def fetch_whitehouse_headlines(limit: int = 18) -> list[str]:
    def extract(page: str) -> list[str]:
        heads = []
        for tag in ("h2", "h3"):
            for match in re.finditer(rf"(?is)<{tag}[^>]*>(.*?)</{tag}>", page):
                title = strip_tags(match.group(1))
                if 12 <= len(title) <= 180:
                    heads.append(title)
        unique = []
        seen = set()
        for h in heads:
            key = h.lower()
            if key not in seen:
                seen.add(key)
                unique.append(h)
        return unique

    briefings = http_get(WHITEHOUSE_BRIEFINGS).decode("utf-8", errors="replace")
    news = http_get(WHITEHOUSE_NEWS).decode("utf-8", errors="replace")
    return (extract(briefings) + extract(news))[:limit]


def render_potus_summary(wh_headlines: list[str]) -> str:
    if not wh_headlines:
        return "<h2>POTUS / ADMIN UPDATE</h2><p><i>No new WhiteHouse.gov headlines retrieved (skipping AI summary).</i></p>"

    prompt = (
        "Return HTML ONLY.\n"
        "Create exactly:\n"
        "<h2>POTUS / ADMIN UPDATE</h2>\n"
        "<ul><li>...</li></ul>\n\n"
        "Use ONLY these WhiteHouse.gov headlines. Do NOT invent facts.\n"
        "Write 4-8 short bullets.\n\n"
        "Headlines:\n"
        + "\n".join(f"- {h}" for h in wh_headlines[:18])
    )
    try:
        return clean_llm_section(ollama_generate(prompt, temperature=0.15), "POTUS / ADMIN UPDATE")
    except Exception:
        return clean_llm_section("", "POTUS / ADMIN UPDATE")


def render_news_themes_summary(headlines: list[tuple[str, str]]) -> str:
    prompt = (
        "Return HTML ONLY.\n"
        "Create exactly:\n"
        "<h2>TODAY'S NEWS THEMES</h2>\n"
        "<ul><li>...</li></ul>\n\n"
        "Use ONLY these headlines. Do NOT invent facts.\n"
        "Write 3-6 bullets.\n\n"
        "Headlines:\n"
        + ("\n".join(f"- {title} — {url}" for title, url in headlines[:30]) if headlines else "- (none)")
    )
    try:
        return clean_llm_section(ollama_generate(prompt, temperature=0.2), "TODAY'S NEWS THEMES")
    except Exception:
        return clean_llm_section("", "TODAY'S NEWS THEMES")


def render_full_brief_summary(section_snapshots: list[tuple[str, str]]) -> str:
    lines = []
    for name, content in section_snapshots:
        txt = compact_text(content)
        if not txt:
            txt = "(no data)"
        lines.append(f"[{name}] {txt[:2500]}")

    prompt = (
        "Return HTML ONLY.\n"
        "Create exactly:\n"
        "<h2>GEMMA FULL BRIEF SUMMARY</h2>\n"
        "<p>...</p>\n\n"
        "Write one concise, action-oriented paragraph.\n"
        "Use ONLY provided data. No invented facts.\n"
        "Focus on what to do in the next 24 hours.\n"
        "Include clear priorities and specific next actions in plain language.\n"
        "If data is missing, include that as a brief watch-out sentence.\n"
        "Do not use bullet points, line breaks, or tag prefixes like [NOW]/[TODAY]/[WATCH].\n\n"
        "Section data:\n"
        + "\n".join(f"- {line}" for line in lines)
    )
    try:
        return clean_llm_paragraph_section(ollama_generate(prompt, temperature=0.1), "GEMMA FULL BRIEF SUMMARY")
    except Exception:
        return clean_llm_paragraph_section("", "GEMMA FULL BRIEF SUMMARY")


# ----------------------------- Email ------------------------------ #

def send_email_html(to_email: str, subject: str, html_body: str) -> None:
    message = "\n".join([
        f"From: {FROM_EMAIL}",
        f"To: {to_email}",
        f"Subject: {subject}",
        "MIME-Version: 1.0",
        'Content-Type: text/html; charset="utf-8"',
        "",
        html_body,
    ]).encode("utf-8", errors="replace")

    log("Sending email via /usr/sbin/sendmail...")
    result = subprocess.run(
        ["/usr/sbin/sendmail", "-t", "-i"],
        input=message,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"sendmail failed ({result.returncode}): "
            f"{result.stderr.decode('utf-8', errors='replace')}"
        )
    log("Email sent.")


# ----------------------------- Build ------------------------------ #

def build_html() -> str:
    now = datetime.now(TZ)
    title = f"{SUBJECT_PREFIX} — {fmt_date_local(now)}"

    blocks: list[str] = [
        f"<h1>{html.escape(title)}</h1>",
        f"<p><i>Generated:</i> {html.escape(now.strftime('%-I:%M %p %Z'))}</p>",
        "<hr>",
    ]

    summary_snapshots: list[tuple[str, str]] = []

    log("Weather...")
    try:
        weather_html = render_weather(get_weather_realtime())
    except Exception as err:
        weather_html = section_error("Weather — Elk River, MN", err)
    blocks.append(weather_html)
    summary_snapshots.append(("Weather", weather_html))
    blocks.append("<hr>")

    log("Markets...")
    try:
        markets_html = render_markets()
    except Exception as err:
        markets_html = section_error("Markets", err)
    blocks.append(markets_html)
    summary_snapshots.append(("Markets", markets_html))
    blocks.append("<hr>")

    log("Calendar...")
    calendar_html = render_calendar_block()
    blocks.append(calendar_html)
    summary_snapshots.append(("Calendar", calendar_html))
    blocks.append("<hr>")

    log("Bible...")
    try:
        bible_html = render_bible_block()
    except Exception as err:
        bible_html = section_error("Bible", err)
    blocks.append(bible_html)
    summary_snapshots.append(("Bible", bible_html))
    blocks.append("<hr>")

    log("Sports...")
    sports_html = render_sports_block()
    blocks.append(sports_html)
    summary_snapshots.append(("Sports", sports_html))
    blocks.append("<hr>")

    log("Headlines...")
    blocks.append("<h2>Headlines</h2>")
    all_headlines: list[tuple[str, str]] = []
    rss_snapshot: list[str] = []

    for name, url in RSS_FEEDS:
        block, items = render_rss_block(name, url, limit=RSS_LIMIT)
        blocks.append(block)
        all_headlines.extend(items)
        rss_snapshot.extend([f"{name}: {title} ({link})" for title, link in items])

    summary_snapshots.append(("RSS Headlines", "\n".join(rss_snapshot) if rss_snapshot else "(none)"))
    blocks.append("<hr>")

    log("POTUS/Admin summary...")
    try:
        wh_headlines = fetch_whitehouse_headlines(limit=18)
    except Exception:
        wh_headlines = []
    potus_html = render_potus_summary(wh_headlines)
    blocks.append(potus_html)
    summary_snapshots.append(("White House Headlines", "\n".join(wh_headlines) if wh_headlines else "(none)"))
    blocks.append("<hr>")

    log("News themes summary...")
    themes_html = render_news_themes_summary(all_headlines)
    blocks.append(themes_html)
    summary_snapshots.append(("News Themes Summary", themes_html))
    blocks.append("<hr>")

    log("Gemma full brief summary...")
    blocks.append(render_full_brief_summary(summary_snapshots))
    blocks.append("<hr>")

    style = """
    <style>
      body { font-family: Arial, Helvetica, sans-serif; line-height: 1.35; }
      h1 { margin-bottom: .2rem; }
      h2 { margin-top: 1.2rem; }
      h3 { margin-top: 1rem; }
      table { border-collapse: collapse; }
      th { text-align: left; }
      hr { margin: 1.2rem 0; }
      a { text-decoration: none; }
      a:hover { text-decoration: underline; }
    </style>
    """
    return f"<!doctype html><html><head><meta charset='utf-8'>{style}</head><body>\n" + "\n".join(blocks) + "\n</body></html>"


def main() -> int:
    log("Building daily brief...")
    body = build_html()
    subject = f"{SUBJECT_PREFIX} — {datetime.now(TZ).strftime('%Y-%m-%d')}"
    send_email_html(TO_EMAIL, subject, body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
