#!/usr/bin/env python3
"""
Daily HTML email brief with deterministic data + Claude AI summaries.

Deterministic:
- Weather (NWS api.weather.gov)
- Markets (Yahoo Finance + CoinGecko)
- Calendars (ICS)
- Bible (BibleGateway VOTD + bible-api)
- Sports (ESPN scoreboards, MN teams, last night)
- Headlines (RSS)

Claude AI:
- POTUS/Admin summary from WhiteHouse.gov headlines
- News themes summary from collected headlines
- Full brief wrap-up summary from all collected sections
"""

from __future__ import annotations

import html
import json
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from urllib.parse import quote as urlquote
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import anthropic


# ----------------------------- Config ----------------------------- #

TZ = ZoneInfo("America/Chicago")
HTTP_TIMEOUT = 25
RSS_LIMIT = 5
MAX_HTML = 240_000

TO_EMAIL = "brian.overton@ymail.com"
FROM_EMAIL = "brian.overton@ymail.com"
SUBJECT_PREFIX = "AI Daily Brief"

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
CLAUDE_KEY_FILE = "/run/secrets/anthropic_api_key"

SMTP_HOST = "smtp.mail.yahoo.com"
SMTP_PORT = 587
SMTP_USER = FROM_EMAIL
SMTP_PASS_FILE = "/run/secrets/yahoo_app_password"

WEATHER_LAT = 45.3036
WEATHER_LON = -93.5672

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
    "nba": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "nhl": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard",
    "mlb": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
    "nfl": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
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


def snapshot_text(text: str, limit: int = 650) -> str:
    compact = compact_text(text)
    if len(compact) <= limit:
        return compact
    return compact[:limit].rsplit(" ", 1)[0] + "..."


def deterministic_full_summary(section_snapshots: list[tuple[str, str]], title: str) -> str:
    parts: list[str] = []
    for name, content in section_snapshots:
        txt = compact_text(content)
        if not txt or txt == "(no data)":
            continue
        if "Summary unavailable." in txt:
            continue
        if "Failed:" in txt:
            continue
        short = txt[:200].rsplit(" ", 1)[0].strip()
        if short:
            parts.append(f"{name}: {short}.")

    if not parts:
        return f"<h2>{html.escape(title)}</h2><p><i>Summary unavailable.</i></p>"

    paragraph = " ".join(parts)
    paragraph = re.sub(r"\s+", " ", paragraph).strip()
    return f"<h2>{html.escape(title)}</h2>\n<p>{html.escape(paragraph)}</p>"


def html_link(title: str, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(title)}</a>'


def fmt_dt_local(value: datetime) -> str:
    return value.astimezone(TZ).strftime("%a %b %-d, %-I:%M %p")


def fmt_date_local(value: datetime) -> str:
    return value.astimezone(TZ).strftime("%A, %B %-d, %Y")


def section_error(title: str, err: Exception) -> str:
    return f"<h2>{html.escape(title)}</h2><p><i>Failed:</i> {html.escape(str(err))}</p>"


# ----------------------------- Claude AI -------------------------- #

def _load_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        with open(CLAUDE_KEY_FILE) as f:
            return f.read().strip()
    except Exception as err:
        raise RuntimeError(f"No Anthropic API key found (set ANTHROPIC_API_KEY or {CLAUDE_KEY_FILE})") from err


def claude_generate(prompt: str, system: str | None = None) -> str:
    client = anthropic.Anthropic(api_key=_load_api_key())
    kwargs: dict = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    message = client.messages.create(**kwargs)
    return (message.content[0].text or "").strip()


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

def get_weather_nws() -> dict:
    nws_headers = {"User-Agent": "daily-brief/2.0 (boverton@mnrealtor.com)", "Accept": "application/json"}
    points = safe_json(http_get(
        f"https://api.weather.gov/points/{WEATHER_LAT},{WEATHER_LON}",
        headers=nws_headers,
    ))
    props = points.get("properties", {})
    forecast = safe_json(http_get(props["forecast"], headers=nws_headers))
    hourly = safe_json(http_get(props["forecastHourly"], headers=nws_headers))
    return {"forecast": forecast, "hourly": hourly}


def render_weather(weather: dict) -> str:
    periods = (weather.get("forecast", {}).get("properties", {}).get("periods") or [])
    hourly = (weather.get("hourly", {}).get("properties", {}).get("periods") or [])
    now = datetime.now(TZ)

    out = ["<h2>Weather — Elk River, MN</h2>", "<ul>"]
    for period in periods[:2]:
        name = period.get("name", "")
        temp = period.get("temperature")
        unit = period.get("temperatureUnit", "F")
        wind_speed = period.get("windSpeed", "")
        wind_dir = period.get("windDirection", "")
        short = period.get("shortForecast", "")
        if temp is not None:
            wind = f"{wind_speed} {wind_dir}".strip()
            out.append(
                f"<li><b>{html.escape(name)}:</b> {temp}°{unit} — "
                f"{html.escape(short)}, Wind: {html.escape(wind)}</li>"
            )
    out.append("</ul>")

    rows = []
    for period in hourly:
        start = period.get("startTime", "")
        try:
            dth = datetime.fromisoformat(start).astimezone(TZ)
        except Exception:
            continue
        if dth < now:
            continue
        rows.append((dth, period.get("temperature"), period.get("temperatureUnit", "F"), period.get("shortForecast", "")))
        if len(rows) >= 3:
            break

    if rows:
        out.append("<h3>Next few hours</h3>")
        out.append('<table border="1" cellpadding="6" cellspacing="0">')
        out.append("<tr><th>Time</th><th>Temp</th><th>Conditions</th></tr>")
        for dth, temp, unit, short in rows:
            out.append(
                "<tr>"
                f"<td>{html.escape(dth.strftime('%-I:%M %p'))}</td>"
                f"<td>{temp}°{unit}</td>"
                f"<td>{html.escape(short)}</td>"
                "</tr>"
            )
        out.append("</table>")

    return "\n".join(out)


# ----------------------------- Markets ---------------------------- #

def yahoo_last(symbol: str) -> float | None:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urlquote(symbol)}"
    result = (
        safe_json(http_get(url, headers={"User-Agent": "Mozilla/5.0"}))
        .get("chart", {})
        .get("result") or []
    )
    if not result:
        return None
    price = (result[0].get("meta") or {}).get("regularMarketPrice")
    try:
        return float(price) if price is not None else None
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
    ndq = yahoo_last("^IXIC")
    dji = yahoo_last("^DJI")
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

    url = f"{BIBLE_API_BASE}{urlquote(f'{book} 1')}"
    try:
        verses = (safe_json(http_get(url)).get("verses") or [])
    except Exception:
        return results

    for verse in verses:
        ref = f"{verse.get('book_name','')} {verse.get('chapter','')}:{verse.get('verse','')}".strip()
        text = " ".join((verse.get("text") or "").split())
        if ref and text:
            results.append((ref, text))
        if len(results) >= count:
            break
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

    briefings = http_get(WHITEHOUSE_BRIEFINGS).decode("utf-8", errors="replace")[:MAX_HTML]
    news = http_get(WHITEHOUSE_NEWS).decode("utf-8", errors="replace")[:MAX_HTML]
    return (extract(briefings) + extract(news))[:limit]


def render_potus_summary(wh_headlines: list[str]) -> str:
    if not wh_headlines:
        return "<h2>POTUS / ADMIN UPDATE</h2><p><i>No new WhiteHouse.gov headlines retrieved (skipping AI summary).</i></p>"

    prompt = (
        "Return HTML ONLY.\n"
        "Create exactly:\n"
        "<h2>POTUS / ADMIN UPDATE</h2>\n"
        "<ul><li>...</li></ul>\n\n"
        "Group these headlines into 4-8 bullets by topic.\n"
        "For each bullet, write one sentence describing what the administration did or announced — not just the title.\n"
        "Be factual and concise. Do NOT invent details beyond what the headlines imply.\n\n"
        "Headlines:\n"
        + "\n".join(f"- {h}" for h in wh_headlines[:18])
    )
    try:
        return clean_llm_section(claude_generate(prompt), "POTUS / ADMIN UPDATE")
    except Exception:
        return clean_llm_section("", "POTUS / ADMIN UPDATE")


def render_news_themes_summary(headlines: list[tuple[str, str]]) -> str:
    prompt = (
        "Return HTML ONLY.\n"
        "Create exactly:\n"
        "<h2>TODAY'S NEWS THEMES</h2>\n"
        "<ul><li>...</li></ul>\n\n"
        "Identify 3-6 major themes across these headlines.\n"
        "For each theme, write one sentence describing the pattern or story — not just a headline title.\n"
        "Do NOT invent facts not present in the headlines.\n\n"
        "Headlines:\n"
        + ("\n".join(f"- {title}" for title, url in headlines[:30]) if headlines else "- (none)")
    )
    try:
        return clean_llm_section(claude_generate(prompt), "TODAY'S NEWS THEMES")
    except Exception:
        return clean_llm_section("", "TODAY'S NEWS THEMES")


def render_full_brief_summary(section_snapshots: list[tuple[str, str]], headlines: list[tuple[str, str]]) -> str:
    data_block = "\n".join(
        f"[{name}] {compact_text(content)[:2500] or '(no data)'}"
        for name, content in section_snapshots
    )

    system = (
        "You are a personal daily briefing assistant writing a tight wrap-up paragraph. "
        "Every sentence must cite actual facts, numbers, or names from the brief data provided. "
        "Never write generic filler like 'check the weather' or 'stay informed'. "
        "If a section has no data, skip it rather than padding the paragraph."
    )

    prompt = (
        "Return HTML ONLY — no markdown, no code fences.\n"
        "Output this exact structure and nothing else:\n"
        "<h2>CLAUDE FULL BRIEF SUMMARY</h2>\n"
        "<p>[your paragraph]</p>\n\n"
        "Write one paragraph of 3-5 sentences that synthesizes today's brief:\n"
        "- Open with the single most pressing item (weather event, top headline, or urgent calendar item)\n"
        "- Mention specific values: temperatures, market points/%, game scores, or named headlines\n"
        "- Call out any calendar deadlines or appointments that need action today\n"
        "- Close with one concrete priority for the next 24 hours\n"
        "- No bullet points or line breaks inside the paragraph\n\n"
        "Brief data:\n"
        + data_block
    )

    def _is_valid(html_text: str) -> bool:
        plain = compact_text(html_text)
        if len(plain) < 200:
            return False
        vague = ("summary unavailable", "check the weather", "stay informed", "keep an eye", "no data available")
        return not any(p in plain.lower() for p in vague)

    try:
        result = clean_llm_paragraph_section(claude_generate(prompt, system=system), "CLAUDE FULL BRIEF SUMMARY")
        if _is_valid(result):
            return result
    except Exception:
        pass

    # Retry with a stripped-down prompt before falling back to deterministic
    try:
        retry = (
            "HTML only. No markdown.\n"
            "<h2>CLAUDE FULL BRIEF SUMMARY</h2>\n"
            "<p>Write 3-4 sentences using specific numbers and names from the data below. "
            "Cover weather, markets, top news, and any calendar items. "
            "End with one priority action for today.</p>\n\n"
            "Data:\n" + data_block
        )
        result = clean_llm_paragraph_section(claude_generate(retry), "CLAUDE FULL BRIEF SUMMARY")
        if _is_valid(result):
            return result
    except Exception:
        pass

    return deterministic_full_summary(section_snapshots, "CLAUDE FULL BRIEF SUMMARY")


# ----------------------------- Email ------------------------------ #

def send_email_html(to_email: str, subject: str, html_body: str) -> None:
    smtp_pass = os.environ.get("SMTP_PASSWORD")
    if not smtp_pass:
        try:
            with open(SMTP_PASS_FILE) as f:
                smtp_pass = f.read().strip()
        except Exception as err:
            raise RuntimeError(f"No SMTP password found ({SMTP_PASS_FILE})") from err

    msg = MIMEMultipart("alternative")
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    log("Sending email via Yahoo SMTP...")
    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls(context=ctx)
        server.login(SMTP_USER, smtp_pass)
        server.sendmail(FROM_EMAIL, to_email, msg.as_string())
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
        weather_html = render_weather(get_weather_nws())
    except Exception as err:
        weather_html = section_error("Weather — Elk River, MN", err)
    blocks.append(weather_html)
    summary_snapshots.append(("Weather", snapshot_text(weather_html)))
    blocks.append("<hr>")

    log("Markets...")
    try:
        markets_html = render_markets()
    except Exception as err:
        markets_html = section_error("Markets", err)
    blocks.append(markets_html)
    summary_snapshots.append(("Markets", snapshot_text(markets_html)))
    blocks.append("<hr>")

    log("Calendar...")
    calendar_html = render_calendar_block()
    blocks.append(calendar_html)
    summary_snapshots.append(("Calendar", snapshot_text(calendar_html)))
    blocks.append("<hr>")

    log("Bible...")
    try:
        bible_html = render_bible_block()
    except Exception as err:
        bible_html = section_error("Bible", err)
    blocks.append(bible_html)
    summary_snapshots.append(("Bible", snapshot_text(bible_html)))
    blocks.append("<hr>")

    log("Sports...")
    sports_html = render_sports_block()
    blocks.append(sports_html)
    summary_snapshots.append(("Sports", snapshot_text(sports_html)))
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

    summary_snapshots.append(
        ("RSS Headlines", snapshot_text("\n".join(rss_snapshot) if rss_snapshot else "(none)", limit=900))
    )
    blocks.append("<hr>")

    log("POTUS/Admin summary...")
    try:
        wh_headlines = fetch_whitehouse_headlines(limit=18)
    except Exception:
        wh_headlines = []
    potus_html = render_potus_summary(wh_headlines)
    blocks.append(potus_html)
    summary_snapshots.append(
        ("White House Headlines", snapshot_text("\n".join(wh_headlines) if wh_headlines else "(none)", limit=900))
    )
    blocks.append("<hr>")

    log("News themes summary...")
    themes_html = render_news_themes_summary(all_headlines)
    blocks.append(themes_html)
    summary_snapshots.append(("News Themes Summary", snapshot_text(themes_html)))
    blocks.append("<hr>")

    log("Full brief summary...")
    blocks.append(render_full_brief_summary(summary_snapshots, all_headlines))
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
