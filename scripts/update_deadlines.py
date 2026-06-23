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
    """Return (year, link, deadlines) scraped from the live CFP, or None."""
    template = meta.get("url_template")
    if not template:
        return None
    best = None  # (year, url, deadlines)
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
            return (year, url, deadlines)         # newest edition with an open deadline wins
        if best is None:
            best = (year, url, deadlines)          # remember newest live page as backup
    return best


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
        "place": place, "date": date, "link": link, "timezone": tz,
        "deadlines": out_deadlines,
        "primary_utc": primary["utc"] if primary else None,
    }


def card_from_fallback(name, meta):
    fb = meta["fallback"]
    return build_card(name, meta.get("full_name", name), meta.get("tags", []),
                      fb.get("edition", ""), fb.get("place", "TBA"), fb.get("date", "TBA"),
                      fb.get("link", "#"), fb.get("timezone", meta.get("timezone", "AoE")),
                      fb.get("deadlines", []))


def card_from_scrape(name, meta, year, link, deadlines):
    fb = meta.get("fallback", {})
    same = (year == fb.get("year"))
    edition = fb.get("edition") if same else f"'{year % 100:02d}"
    # Only trust the curated place/date when the scraped edition matches it;
    # for a newer edition we don't yet know them, so show TBA rather than stale.
    place = fb.get("place", "TBA") if same else "TBA"
    date = fb.get("date", "TBA") if same else f"{year}"
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
                year, link, deadlines = res
                card = card_from_scrape(name, meta, year, link, deadlines)
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

    print("selftest:", "PASS" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    main()
