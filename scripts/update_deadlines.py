#!/usr/bin/env python3
"""
Build data/conferences.json by researching each venue's official Call for Papers.

How it works
------------
For every venue in scripts/conferences.seed.yml the updater:
  1. Takes the venue's `url_template` and fills in the year -- the only part of
     these CFP URLs that changes between editions (osdi26 -> osdi27, fast27 ->
     fast28, asplos2027 -> asplos2028, ...). It probes the next few years and
     keeps the newest edition whose CFP page is live.
  2. Fetches that page and scrapes the *submission* deadlines straight from it
     (abstract / paper / registration), reading the date, time and timezone and
     deliberately ignoring notification / camera-ready / conference dates.
  3. Converts each wall-clock deadline into an absolute UTC instant so the
     browser only has to count down to it.

If a venue cannot be fetched or yields nothing plausible, it keeps the curated
`fallback` from the seed -- the site is never overwritten with bad data.

No third-party data feed (no ccfddl). Only the official CFP pages.

Usage:
  python update_deadlines.py            # online: research + write data/conferences.json
  python update_deadlines.py --offline  # skip network, build from curated fallbacks
  python update_deadlines.py --selftest # run timezone + scraper unit tests
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - bs4 always present in CI/local
    BeautifulSoup = None

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "scripts" / "conferences.seed.yml"
OUT = ROOT / "data" / "conferences.json"
LOG = ROOT / "data" / "update_log.txt"

UTC = dt.timezone.utc
PROBE_AHEAD = 2          # probe current year .. current+2 for the newest edition
PAST_WINDOW = 550        # accept scraped dates at most ~18 months in the past
FUTURE_WINDOW = 800      # ...and at most ~26 months in the future

# ---------------------------------------------------------------- timezones ---
FIXED_OFFSETS = {
    "AOE": -12, "AT": -12,
    "EST": -5, "EDT": -4, "CST": -6, "CDT": -5,
    "MST": -7, "MDT": -6, "PST": -8, "PDT": -7,
    "BST": +1, "CET": +1, "CEST": +2, "JST": +9, "KST": +9, "GMT": 0, "UTC": 0,
}
NAMED_ZONES = {
    "PT": "America/Los_Angeles", "ET": "America/New_York",
    "CT": "America/Chicago", "MT": "America/Denver",
}
MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_NUM = {}
for _i, _full in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"], start=1):
    MONTH_NUM[_full] = _i
    MONTH_NUM[_full[:3]] = _i
MONTH_NUM["sept"] = 9


def parse_tz(tz: str):
    tz = (tz or "AoE").strip()
    up = tz.upper()
    if up in FIXED_OFFSETS:
        return ("fixed", FIXED_OFFSETS[up] * 60)
    if up in NAMED_ZONES:
        return ("zone", ZoneInfo(NAMED_ZONES[up]))
    if up.startswith("UTC"):
        rest = up[3:].replace(" ", "") or "+0"
        sign = -1 if rest[0] == "-" else 1
        rest = rest.lstrip("+-")
        h, m = (rest.split(":") + ["0"])[:2] if ":" in rest else (rest, "0")
        return ("fixed", sign * (int(h) * 60 + int(m)))
    try:
        return ("zone", ZoneInfo(tz))
    except Exception:
        return ("fixed", 0)


def to_utc(datetime_str: str, tz: str):
    """('YYYY-MM-DD HH:MM:SS', tz) -> (iso_utc, human_display) or (None, 'TBD')."""
    s = (datetime_str or "").strip()
    if not s or s.upper() in ("TBD", "TBA", "NULL", "NONE"):
        return None, "TBD"
    try:
        local = dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        local = dt.datetime.strptime(s, "%Y-%m-%d %H:%M")
    kind, val = parse_tz(tz)
    if kind == "fixed":
        utc = (local - dt.timedelta(minutes=val)).replace(tzinfo=UTC)
    else:
        utc = local.replace(tzinfo=val).astimezone(UTC)
    iso = utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    display = f"{MONTHS[local.month]} {local.day}, {local.year}, {local.strftime('%H:%M')} {(tz or 'AoE').strip()}"
    return iso, display


def _local_to_utc(local, tz):
    kind, val = parse_tz(tz)
    if kind == "fixed":
        return (local - dt.timedelta(minutes=val)).replace(tzinfo=UTC)
    return local.replace(tzinfo=val).astimezone(UTC)


def _utc_to_local(utc, tz):
    """Inverse of _local_to_utc: render a UTC instant as wall-clock in `tz`."""
    kind, val = parse_tz(tz)
    if kind == "fixed":
        return utc.replace(tzinfo=None) + dt.timedelta(minutes=val)
    return utc.astimezone(val).replace(tzinfo=None)


# ----------------------------------------------------------------- scraping ---
DATE_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t)?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I)
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)?", re.I)
TZ_RE = re.compile(
    r"\b(AoE|Anywhere on Earth|PST|PDT|EST|EDT|CST|CDT|MST|MDT|"
    r"UTC[+-]\d{1,2}(?::\d{2})?|UTC|PT|ET)\b", re.I)

# A line is about a *submission* deadline only if it has a positive cue and no
# negative cue (notification / camera-ready / the conference dates themselves).
POS_ABSTRACT = ("abstract", "title", "registration")
POS_PAPER = ("submission", "submit", "paper", "deadline", "due")
NEG = ("notification", "notify", "camera", "accept", "reject", "rebuttal",
       "response", "poster", "artifact", "review", "conference", "held",
       "will take place", "workshop date", "program", "presentation",
       "registration opens", "early bird", "travel", "venue")


def classify_label(text: str):
    s = text.lower()
    if any(n in s for n in NEG):
        return None
    if any(k in s for k in POS_ABSTRACT) and "paper submission" not in s:
        return "Abstract"
    if any(k in s for k in POS_PAPER):
        return "Paper"
    return None


def find_date(text: str):
    m = DATE_RE.search(text)
    if not m:
        return None
    mon = MONTH_NUM.get(m.group(1).lower().rstrip("."))
    if not mon:
        return None
    day, year = int(m.group(2)), int(m.group(3))
    try:
        return dt.date(year, mon, day)
    except ValueError:
        return None


def find_time_tz(context: str, default_tz: str):
    hh, mm, ss = 23, 59, 59
    tm = TIME_RE.search(context)
    if tm:
        hh, mm = int(tm.group(1)), int(tm.group(2))
        ap = (tm.group(3) or "").replace(".", "").lower()
        if ap == "pm" and hh < 12:
            hh += 12
        elif ap == "am" and hh == 12:
            hh = 0
        ss = 0
    tz = default_tz
    tzm = TZ_RE.search(context)
    if tzm:
        tok = tzm.group(1)
        tz = "AoE" if tok.lower() in ("aoe", "anywhere on earth") else tok.upper()
    return f"{hh:02d}:{mm:02d}:{ss:02d}", tz


def extract_deadlines(html: str, default_tz: str, today: dt.date):
    """Scrape (label, datetime, timezone) submission deadlines from CFP HTML."""
    if BeautifulSoup is None:
        return []
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    raw = []  # (label_text, date, context_text)

    # 1) Tables (SIGOPS-style "Important Dates" grids): label cell + date cell.
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            label = cells[0]
            for cell in cells[1:]:
                d = find_date(cell)
                if d:
                    raw.append((label, d, " ".join(cells)))

    # 2) Prose / bullet lists: any line with a date + a deadline cue.
    text = soup.get_text("\n")
    for line in (ln.strip() for ln in text.split("\n")):
        if len(line) < 6 or len(line) > 400:
            continue
        if find_date(line):
            raw.append((line, find_date(line), line))

    seen, out = set(), []
    lo, hi = today - dt.timedelta(days=PAST_WINDOW), today + dt.timedelta(days=FUTURE_WINDOW)
    for label_text, d, ctx in raw:
        label = classify_label(label_text)
        if not label or not (lo <= d <= hi):
            continue
        time_str, tz = find_time_tz(ctx, default_tz)
        key = (label, d.isoformat())
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": label, "datetime": f"{d.isoformat()} {time_str}", "timezone": tz})
    out.sort(key=lambda x: x["datetime"])
    return out


# --- conference date / place extraction (best-effort; falls back to seed) ----
MONTH_ALT = (r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
             r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t)?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
RANGE_SAME = re.compile(MONTH_ALT + r"\.?\s+(\d{1,2})\s*[–—\-]\s*(\d{1,2}),?\s+(\d{4})", re.I)
RANGE_CROSS = re.compile(MONTH_ALT + r"\.?\s+(\d{1,2})\s*[–—\-]\s*"
                         + MONTH_ALT + r"\.?\s+(\d{1,2}),?\s+(\d{4})", re.I)
# A run of 2–4 comma-separated capitalised phrases, e.g. "Renton, WA, USA",
# "Heraklion, Crete, Greece", "Hyatt Hotel, Shatin, Hong Kong".
LOC_SEQ = re.compile(r"[A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*"
                     r"(?:,\s+[A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*){1,3}")
# Leading venue components to strip ("Hyatt Hotel, Shatin, Hong Kong" -> "Shatin, Hong Kong").
VENUE_WORDS = {"hotel", "regency", "hyatt", "marriott", "hilton", "westin", "sheraton",
               "center", "centre", "convention", "resort", "inn", "plaza", "palace",
               "ballroom", "campus", "building", "hall", "conference"}
# Components that mean this isn't a place at all.
BAD_WORDS = {"committee", "university", "institute", "conference", "symposium", "workshop",
             "program", "proceedings", "department", "call", "paper", "papers", "association",
             "computing", "track", "session", "chair", "school", "society", "sponsored"}


def _words(s):
    return set(re.findall(r"[a-z]+", s.lower()))


def _page_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" "))


MONTH_WORDS = set()
for _m in "january february march april may june july august september october november december".split():
    MONTH_WORDS.update({_m, _m[:3]})
MONTH_WORDS.add("sept")
STOP_WORDS = {"where", "when", "welcome", "home", "about", "call", "papers", "program",
              "sponsored", "menu", "contact", "organizers", "attend", "participate",
              "venue", "news", "register", "registration", "hotel", "deadline"}


def _ranges(text, today):
    """All plausible multi-day date ranges in a block, as (start_date, clean_str)."""
    lo, hi = today - dt.timedelta(days=120), today + dt.timedelta(days=900)
    out = []
    for m in RANGE_CROSS.finditer(text):
        mn = MONTH_NUM.get(m.group(1).lower().rstrip("."))
        if mn:
            try:
                sd = dt.date(int(m.group(5)), mn, int(m.group(2)))
            except ValueError:
                continue
            if lo <= sd <= hi:
                out.append((sd, f"{m.group(1).capitalize()} {int(m.group(2))} - {m.group(3).capitalize()} {int(m.group(4))}, {m.group(5)}"))
    for m in RANGE_SAME.finditer(text):
        mn = MONTH_NUM.get(m.group(1).lower().rstrip("."))
        if mn:
            try:
                sd = dt.date(int(m.group(4)), mn, int(m.group(2)))
            except ValueError:
                continue
            if lo <= sd <= hi:
                out.append((sd, f"{m.group(1).capitalize()} {int(m.group(2))}-{int(m.group(3))}, {m.group(4)}"))
    return out


def _clean_part(p):
    """Trim a place component at a month name, heading word, or year."""
    kept = []
    for w in p.split():
        lw = re.sub(r"[^a-z]", "", w.lower())
        if not lw or lw in MONTH_WORDS or lw in STOP_WORDS or re.fullmatch(r"\d{4}", w):
            break
        kept.append(w)
    return " ".join(kept).strip(" ,.")


def _place_in(text):
    """Find 'City, Region/Country' in a single block, skipping venue names."""
    for m in LOC_SEQ.finditer(text):
        parts = [_clean_part(p.strip()) for p in m.group(0).split(",")]
        parts = [p for p in parts if p]
        while parts and (_words(parts[0]) & VENUE_WORDS):       # drop leading venue names
            parts.pop(0)
        parts = [p for p in parts if not (_words(p) & BAD_WORDS)]
        if len(parts) >= 2:
            place = ", ".join(parts[:3])
            if 4 <= len(place) <= 46:
                return place
    return None


def extract_conf_info(htmls, today):
    """Return (conference_date_str, place_str), best-effort, or (None, None).

    Works block by block (so a place can't bleed across a heading) and prefers the
    latest multi-day date range on the page — that's the conference, not a deadline."""
    date, place = None, None
    for html in htmls:
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style"]):
            t.decompose()
        blocks = [re.sub(r"\s+", " ", b).strip() for b in soup.get_text("\n").split("\n")]
        blocks = [b for b in blocks if b]
        best = None  # (start_date, clean, block_index)
        for i, b in enumerate(blocks):
            for sd, clean in _ranges(b, today):
                if best is None or sd > best[0]:
                    best = (sd, clean, i)
        if not best:
            continue
        if date is None:
            date = best[1]
        if place is None:
            for j in (best[2], best[2] + 1, best[2] - 1, best[2] + 2):
                if 0 <= j < len(blocks):
                    found = _place_in(blocks[j])
                    if found:
                        place = found
                        break
        if date and place:
            break
    return date, place


# --- HotCRP: most systems CFPs link to a *.hotcrp.com submission site whose
#     /deadlines page is server-rendered and authoritative (exact time + tz). ---
HOTCRP_LINK = re.compile(r"https?://([A-Za-z0-9.\-]+\.hotcrp\.com)", re.I)
HOTCRP_DL = re.compile(
    r"(?P<kind>registration|submission|abstract|title|paper)\s+deadline\s*:?\s*"
    r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+)?"
    r"(?P<mon>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t)?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+"
    r"(?P<day>\d{1,2}),?\s+(?P<year>\d{4})"
    r"(?:,?\s+(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?\s*(?P<ap>[AaPp]\.?[Mm]\.?)?"
    r"\s*(?P<tz>[A-Za-z]{2,5})?)?", re.I)
KNOWN_TZ = set(FIXED_OFFSETS) | set(NAMED_ZONES) | {"AOE"}


def hotcrp_deadline_urls(html):
    seen, urls = set(), []
    for m in HOTCRP_LINK.finditer(html or ""):
        host = m.group(1).lower()
        if host not in seen:
            seen.add(host)
            urls.append(f"https://{host}/deadlines")
    return urls


def parse_hotcrp_deadlines(html, today):
    """Parse a HotCRP /deadlines page. A deadline that lands exactly on an AoE
    day boundary is shown in AoE; otherwise it keeps the timezone HotCRP reports
    (e.g. NSDI's 8:59 PM PDT). The exact instant is preserved either way."""
    text = _page_text(html)
    lo, hi = today - dt.timedelta(days=550), today + dt.timedelta(days=800)
    seen, out = set(), []
    for m in HOTCRP_DL.finditer(text):
        mn = MONTH_NUM.get(m.group("mon").lower().rstrip("."))
        tok = (m.group("tz") or "").upper()
        if not mn or tok not in KNOWN_TZ:
            continue
        H = int(m.group("h")) if m.group("h") else 23
        mi = int(m.group("mi")) if m.group("mi") else 59
        s = int(m.group("s")) if m.group("s") else 59
        ap = (m.group("ap") or "").replace(".", "").lower()
        if ap == "pm" and H < 12:
            H += 12
        elif ap == "am" and H == 12:
            H = 0
        try:
            local = dt.datetime(int(m.group("year")), mn, int(m.group("day")), H, mi, s)
        except ValueError:
            continue
        aoe = _utc_to_local(_local_to_utc(local, tok), "AoE")
        if aoe.hour == 23 and aoe.minute == 59:
            disp_tz, d0 = "AoE", aoe
        else:
            disp_tz, d0 = tok, local
        if not (lo <= d0.date() <= hi):
            continue
        label = "Abstract" if m.group("kind").lower() in ("registration", "abstract", "title") else "Paper"
        key = (label, d0.strftime("%Y-%m-%d"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": label, "datetime": d0.strftime("%Y-%m-%d %H:%M:%S"), "timezone": disp_tz})
    return out


def homepage_url(url):
    if url.endswith("/call-for-papers"):
        return url[: -len("/call-for-papers")]
    if url.endswith("/cfp.html"):
        return url[: -len("cfp.html")] + "index.html"
    if url.endswith("/cfp/"):
        return url[: -len("cfp/")]
    if url.endswith("/cfp"):
        return url[: -len("cfp")]
    return None


def fetch(url: str, timeout: int = 25):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; sysconfdeadlines/1.0; +https://github.com/yulistic/sysconfdeadlines)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            charset = r.headers.get_content_charset() or "utf-8"
            return r.read().decode(charset, errors="replace")
    except Exception as e:  # noqa: BLE001
        print(f"    fetch error {url}: {e}", file=sys.stderr)
        return None


def probe_years(today: dt.date, biennial_odd: bool):
    years = list(range(today.year + PROBE_AHEAD, today.year - 1, -1))  # newest first
    if biennial_odd:
        years = [y for y in years if y % 2 == 1]
    return years


def url_for(template: str, year: int):
    return template.replace("{yyyy}", str(year)).replace("{yy}", f"{year % 100:02d}")


def research_venue(name: str, meta: dict, today: dt.date, log: list):
    """Return (year, link, deadlines, conf_date, place) from the live CFP, or None."""
    template = meta.get("url_template")
    if not template:
        return None
    chosen = None  # (year, url, deadlines, html)
    for year in probe_years(today, meta.get("biennial_odd", False)):
        url = url_for(template, year)
        html = fetch(url)
        if not html:
            continue
        deadlines = extract_deadlines(html, meta.get("timezone", "AoE"), today)
        if not deadlines:
            continue
        has_future = any(dl["datetime"][:10] >= today.isoformat() for dl in deadlines)
        log.append(f"  {name}: {url} -> {len(deadlines)} deadline(s)"
                   f"{' (incl. upcoming)' if has_future else ' (all past)'}")
        if has_future:
            chosen = (year, url, deadlines, html)   # newest edition with an open deadline wins
            break
        if chosen is None:
            chosen = (year, url, deadlines, html)   # remember newest live page as backup
    if not chosen:
        return None
    year, url, deadlines, html = chosen

    # Refine with authoritative, exact deadlines from HotCRP submission sites:
    # seed-configured URLs (templated by year) plus any linked from the CFP.
    hc_urls = [url_for(t, year) for t in (meta.get("hotcrp") or [])]
    hc_urls += hotcrp_deadline_urls(html)
    hc, fetched = {}, set()
    for hurl in hc_urls:
        if hurl in fetched:
            continue
        fetched.add(hurl)
        hp = fetch(hurl)
        if hp:
            for d in parse_hotcrp_deadlines(hp, today):
                hc[(d["label"], d["datetime"][:10])] = d
    if hc:
        merged = {(d["label"], d["datetime"][:10]): d for d in deadlines}
        merged.update(hc)                       # HotCRP wins on the same (label, date)
        deadlines = sorted(merged.values(), key=lambda d: d["datetime"])
        log.append(f"    {name}: merged {len(hc)} HotCRP deadline(s) from {len(fetched)} site(s)")

    home = homepage_url(url)
    conf_date, place = extract_conf_info([fetch(home) if home else None, html], today)
    if conf_date:
        log.append(f"    {name}: conference dates -> {conf_date}")
    if place:
        log.append(f"    {name}: place -> {place}")
    return (year, url, deadlines, conf_date, place)


# ------------------------------------------------------------------- build ----
def build_card(name, full_name, tags, edition, place, date, link, tz, deadlines):
    out_deadlines = []
    for d in deadlines:
        iso, display = to_utc(d.get("datetime"), d.get("timezone", tz))
        out_deadlines.append({"label": d.get("label", "Deadline"), "utc": iso, "display": display})
    dated = [d for d in out_deadlines if d["utc"]]
    primary = max(dated, key=lambda d: d["utc"]) if dated else None
    return {
        "id": re.sub(r"[^a-z0-9]+", "-", f"{name}-{edition}".lower()).strip("-"),
        "conf": name, "edition": edition, "full_name": full_name, "tags": tags,
        "place": place, "date": date, "link": link, "homepage": homepage_url(link) or link,
        "timezone": tz,
        "deadlines": out_deadlines,
        "primary_utc": primary["utc"] if primary else None,
    }


def card_from_fallback(name, meta):
    fb = meta["fallback"]
    return build_card(name, meta.get("full_name", name), meta.get("tags", []),
                      fb.get("edition", ""), fb.get("place", "TBA"), fb.get("date", "TBA"),
                      fb.get("link", "#"), fb.get("timezone", meta.get("timezone", "AoE")),
                      fb.get("deadlines", []))


def card_from_scrape(name, meta, year, link, deadlines, conf_date=None, place=None):
    fb = meta.get("fallback", {})
    same = (year == fb.get("year"))
    edition = fb.get("edition") if same else f"'{year % 100:02d}"
    # Prefer freshly scraped date/place; else the curated fallback (only when the
    # edition matches); else TBA for a newer edition we don't yet have details for.
    place = place or (fb.get("place", "TBA") if same else "TBA")
    date = conf_date or (fb.get("date", "TBA") if same else f"{year}")
    return build_card(name, meta.get("full_name", name), meta.get("tags", []),
                      edition, place, date, link, meta.get("timezone", "AoE"), deadlines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="Skip network; use curated fallbacks.")
    args = ap.parse_args()

    seed = yaml.safe_load(SEED.read_text(encoding="utf-8"))
    venues = seed.get("venues", {})
    today = dt.datetime.now(UTC).date()
    log = [f"Run {dt.datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%SZ')} (offline={args.offline})"]

    cards = []
    for name, meta in venues.items():
        card = None
        if not args.offline:
            try:
                res = research_venue(name, meta, today, log)
            except Exception as e:  # noqa: BLE001 - never let one venue break the run
                res = None
                log.append(f"  {name}: ERROR {e}")
            if res:
                year, link, deadlines, conf_date, place = res
                card = card_from_scrape(name, meta, year, link, deadlines, conf_date, place)
        if card is None:
            card = card_from_fallback(name, meta)
            log.append(f"  {name}: using curated fallback")
        cards.append(card)

    cards.sort(key=lambda c: (c["primary_utc"] is None, c["primary_utc"] or ""))
    all_tags = sorted({t for c in cards for t in c["tags"]})
    payload = {
        "generated": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Scraped from official Call-for-Papers pages",
        "tags": all_tags,
        "conferences": cards,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    LOG.write_text("\n".join(log) + "\n", encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)} with {len(cards)} venues "
          f"({'offline/fallback' if args.offline else 'scraped CFPs'}).")
    for line in log:
        print(line)


# --------------------------------------------------------------- self-tests ---
def _selftest():
    ok = True

    # 1) timezone conversions
    for (s, tz), expect in [
        (("2026-09-09 23:59:59", "AoE"), "2026-09-10T11:59:59Z"),
        (("2025-12-04 17:59:00", "EST"), "2025-12-04T22:59:00Z"),
        (("2026-01-01 00:00:00", "UTC+9"), "2025-12-31T15:00:00Z"),
    ]:
        got, _ = to_utc(s, tz)
        ok &= got == expect
        print(f"  [{'ok ' if got == expect else 'FAIL'}] tz {s} {tz} -> {got}")

    today = dt.date(2026, 6, 22)

    # 2) SIGOPS-style table: keep the submission deadline, drop notification/conference.
    table_html = """
    <table>
      <tr><th>Event</th><th>Date</th></tr>
      <tr><td><b>Submission deadline (no extensions)</b></td><td>June 10, 2026</td><td><a>HotCRP</a></td></tr>
      <tr><td>Author notification</td><td>September 18, 2026</td></tr>
      <tr><td><b>Conference</b></td><td>November 16-18, 2026</td></tr>
    </table>"""
    got = extract_deadlines(table_html, "AoE", today)
    want = [{"label": "Paper", "datetime": "2026-06-10 23:59:59", "timezone": "AoE"}]
    ok &= got == want
    print(f"  [{'ok ' if got == want else 'FAIL'}] table -> {got}")

    # 3) USENIX-style prose: two cycle deadlines w/ AoE, ignore the notification.
    prose_html = """
    <h2>Important Dates</h2>
    <ul>
      <li>Spring Deadline: Paper submissions due Tuesday, March 17, 2026, 23:59 AoE</li>
      <li>Fall Deadline: Paper submissions due Tuesday, September 15, 2026, 23:59 AoE</li>
      <li>Notification of acceptance: December 9, 2026</li>
    </ul>"""
    got = extract_deadlines(prose_html, "AoE", today)
    want = [
        {"label": "Paper", "datetime": "2026-03-17 23:59:00", "timezone": "AoE"},
        {"label": "Paper", "datetime": "2026-09-15 23:59:00", "timezone": "AoE"},
    ]
    ok &= got == want
    print(f"  [{'ok ' if got == want else 'FAIL'}] prose -> {got}")

    # 4) Abstract vs paper classification + EST time.
    abs_html = "<p>Paper title and abstract registration due December 4, 2025, 5:59 pm EST</p>"
    got = extract_deadlines(abs_html, "AoE", dt.date(2025, 6, 1))
    want = [{"label": "Abstract", "datetime": "2025-12-04 17:59:00", "timezone": "EST"}]
    ok &= got == want
    print(f"  [{'ok ' if got == want else 'FAIL'}] abstract -> {got}")

    # 5) conference date + place: pick the latest range (the conference), grab nearby place
    ci = ("<p>The 25th USENIX Conference on File and Storage Technologies (FAST '27) "
          "will take place February 23–25, 2027, at the Hyatt Regency Lake Washington "
          "in Renton, WA, USA. Author response period August 29 – September 2, 2026.</p>")
    gd, gp = extract_conf_info([ci], dt.date(2026, 6, 1))
    good = (gd == "February 23-25, 2027" and gp == "Renton, WA, USA")
    ok &= good
    print(f"  [{'ok ' if good else 'FAIL'}] conf-info -> {gd} | {gp}")

    ci2 = "<p>SOSP 2026 will be held September 29 – October 2, 2026 in Prague, Czechia.</p>"
    gd2, gp2 = extract_conf_info([ci2], dt.date(2026, 6, 1))
    good2 = (gd2 == "September 29 - October 2, 2026" and gp2 == "Prague, Czechia")
    ok &= good2
    print(f"  [{'ok ' if good2 else 'FAIL'}] conf-info2 -> {gd2} | {gp2}")

    # venue name in front of the city must be stripped ("Hyatt Hotel, Shatin, Hong Kong")
    ci3 = ("<h1>ATC 2026</h1><p>November 15-18, 2026 · Hyatt Hotel, Shatin, Hong Kong</p>"
           "<h3>Where</h3><p>Hyatt Hotel, Shatin, Hong Kong</p>")
    gd3, gp3 = extract_conf_info([ci3], dt.date(2026, 6, 1))
    good3 = (gd3 == "November 15-18, 2026" and gp3 == "Shatin, Hong Kong")
    ok &= good3
    print(f"  [{'ok ' if good3 else 'FAIL'}] conf-info3 -> {gd3} | {gp3}")

    # 6) HotCRP /deadlines: EDT times become the venue's AoE wall-clock
    hc_html = ("<h3>Upcoming</h3>"
               "<p>Registration deadline: Friday May 8, 2026, 7:59:59 AM EDT</p>"
               "<p>Submission deadline: Friday May 15, 2026, 7:59:59 AM EDT</p>")
    hc = parse_hotcrp_deadlines(hc_html, dt.date(2026, 1, 1))
    want_hc = [
        {"label": "Abstract", "datetime": "2026-05-07 23:59:59", "timezone": "AoE"},
        {"label": "Paper", "datetime": "2026-05-14 23:59:59", "timezone": "AoE"},
    ]
    ok &= hc == want_hc
    print(f"  [{'ok ' if hc == want_hc else 'FAIL'}] hotcrp(AoE) -> {hc}")

    # NSDI-style: 8:59 PM PDT is NOT an AoE boundary, so keep PDT
    nsdi_html = ("<p>Submission deadline: Thursday Apr 23, 2026, 8:59 PM PDT</p>"
                 "<p>Registration deadline: Thursday Apr 16, 2026, 8:59 PM PDT</p>")
    hcn = sorted(parse_hotcrp_deadlines(nsdi_html, dt.date(2026, 1, 1)), key=lambda d: d["datetime"])
    want_n = [
        {"label": "Abstract", "datetime": "2026-04-16 20:59:59", "timezone": "PDT"},
        {"label": "Paper", "datetime": "2026-04-23 20:59:59", "timezone": "PDT"},
    ]
    ok &= hcn == want_n
    print(f"  [{'ok ' if hcn == want_n else 'FAIL'}] hotcrp(PDT) -> {hcn}")
    # ...and the submission-site URL is discovered from the CFP link
    urls = hotcrp_deadline_urls('Submit at <a href="https://eurosys27-spring.hotcrp.com/">here</a>')
    ok &= urls == ["https://eurosys27-spring.hotcrp.com/deadlines"]
    print(f"  [{'ok ' if urls == ['https://eurosys27-spring.hotcrp.com/deadlines'] else 'FAIL'}] hotcrp-url -> {urls}")

    print("selftest:", "PASS" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    main()
