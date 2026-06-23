#!/usr/bin/env python3
"""
Build data/conferences.json for the systems-conference deadlines site.

Pipeline
--------
1. Load scripts/conferences.seed.yml (the human-maintained source of truth).
2. If online, pull the latest editions for each tracked venue from the
   community-maintained ccfddl dataset (https://github.com/ccfddl/ccf-deadlines)
   and merge them in additively, so newly announced deadlines appear
   automatically without ever dropping the curated seed data.
3. Convert every wall-clock deadline (+ timezone) into an absolute UTC instant
   so the browser only ever does `new Date(utc) - now` — no timezone parsing.
4. Write data/conferences.json.

Run `python update_deadlines.py --offline` to skip the network and build from
the seed only (used for local/seed generation). The GitHub Action runs it
online on a schedule.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "scripts" / "conferences.seed.yml"
OUT = ROOT / "data" / "conferences.json"

CCFDDL_RAW = "https://raw.githubusercontent.com/ccfddl/ccf-deadlines/main/conference/{path}"

UTC = dt.timezone.utc

# Fixed-offset timezone labels (hours east of UTC). AoE = Anywhere on Earth.
FIXED_OFFSETS = {
    "AOE": -12, "AT": -12,
    "EST": -5, "EDT": -4, "CST": -6, "CDT": -5,
    "MST": -7, "MDT": -6, "PST": -8, "PDT": -7,
    "BST": +1, "CET": +1, "CEST": +2, "JST": +9, "KST": +9, "GMT": 0, "UTC": 0,
}
# DST-aware named zones.
NAMED_ZONES = {
    "PT": "America/Los_Angeles", "ET": "America/New_York",
    "CT": "America/Chicago", "MT": "America/Denver",
}
MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def parse_tz(tz: str):
    """Return ('fixed', minutes_east) or ('zone', ZoneInfo) for a tz label."""
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
        if ":" in rest:
            h, m = rest.split(":")
        else:
            h, m = rest, "0"
        return ("fixed", sign * (int(h) * 60 + int(m)))
    # Last resort: treat as a tz database name, else UTC.
    try:
        return ("zone", ZoneInfo(tz))
    except Exception:
        return ("fixed", 0)


def to_utc(datetime_str: str, tz: str):
    """('YYYY-MM-DD HH:MM:SS', tz) -> ('2026-09-10T11:59:59Z', 'Sep 9, 2026, 23:59 AoE') or (None, 'TBD')."""
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
    label = (tz or "AoE").strip()
    display = f"{MONTHS[local.month]} {local.day}, {local.year}, {local.strftime('%H:%M')} {label}"
    return iso, display


def build_card(venue, full_name, tags, edition, place, date, link, tz, deadlines):
    """Assemble one website card from already-structured fields."""
    out_deadlines = []
    for d in deadlines:
        iso, display = to_utc(d.get("datetime"), d.get("timezone", tz))
        out_deadlines.append({
            "label": d.get("label", "Deadline"),
            "utc": iso,
            "display": display,
        })
    # Primary deadline = latest one with a real date (usually the paper deadline).
    dated = [d for d in out_deadlines if d["utc"]]
    primary = max(dated, key=lambda d: d["utc"]) if dated else None
    return {
        "id": f"{venue}-{edition}".lower().replace(" ", "-").replace("'", "").replace("(", "").replace(")", ""),
        "conf": venue,
        "edition": edition,
        "full_name": full_name,
        "tags": tags,
        "place": place,
        "date": date,
        "link": link,
        "timezone": tz,
        "deadlines": out_deadlines,
        "primary_utc": primary["utc"] if primary else None,
    }


def cards_from_seed_venue(venue, meta):
    cards = []
    for ed in meta.get("editions", []):
        cards.append(build_card(
            venue, meta.get("full_name", venue), meta.get("tags", []),
            ed.get("edition", ""), ed.get("place", "TBA"), ed.get("date", "TBA"),
            ed.get("link", "#"), ed.get("timezone", "AoE"), ed.get("deadlines", []),
        ))
    return cards


def fetch_ccfddl(path, timeout=20):
    url = CCFDDL_RAW.format(path=path)
    req = urllib.request.Request(url, headers={"User-Agent": "sysconfdeadlines-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return yaml.safe_load(r.read().decode("utf-8"))


def cards_from_ccfddl(venue, meta, recent_years=2):
    """Best-effort: build cards for a venue from ccfddl. Returns [] on any failure."""
    path = meta.get("ccfddl")
    if not path:
        return []
    try:
        data = fetch_ccfddl(path)
    except Exception as e:  # noqa: BLE001 - network/parse errors are non-fatal
        print(f"  ! ccfddl fetch failed for {venue} ({path}): {e}", file=sys.stderr)
        return []
    if not data:
        return []
    entry = data[0] if isinstance(data, list) else data
    confs = entry.get("confs", []) or []
    years = sorted({c.get("year") for c in confs if c.get("year")}, reverse=True)
    keep = set(years[:recent_years])

    cards = []
    for c in confs:
        if c.get("year") not in keep:
            continue
        tz = c.get("timezone", "AoE")
        timelines = c.get("timeline", []) or []
        multi = len(timelines) > 1
        for tl in timelines:
            deadlines = []
            if tl.get("abstract_deadline"):
                deadlines.append({"label": "Abstract", "datetime": str(tl["abstract_deadline"]), "timezone": tz})
            if tl.get("deadline") and str(tl["deadline"]).upper() != "TBD":
                deadlines.append({"label": "Paper", "datetime": str(tl["deadline"]), "timezone": tz})
            if not deadlines:
                continue
            edition = f"'{str(c.get('year'))[-2:]}"
            if multi:
                # Disambiguate cycles by the paper deadline's month.
                try:
                    m = dt.datetime.strptime(deadlines[-1]["datetime"][:10], "%Y-%m-%d").month
                    edition = f"{edition} ({MONTHS[m]} cycle)"
                except Exception:
                    pass
            cards.append(build_card(
                venue, meta.get("full_name", venue), meta.get("tags", []),
                edition, c.get("place", "TBA"), c.get("date", "TBA"),
                c.get("link", "#"), tz, deadlines,
            ))
    return cards


def dedup_key(card):
    """Identity for merging: venue + year + month of the primary deadline."""
    if card["primary_utc"]:
        return (card["conf"], card["primary_utc"][:7])  # YYYY-MM
    return (card["conf"], card["edition"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="Skip ccfddl; build from seed only.")
    args = ap.parse_args()

    seed = yaml.safe_load(SEED.read_text(encoding="utf-8"))
    venues = seed.get("venues", {})

    merged = {}  # dedup_key -> card
    for venue, meta in venues.items():
        seed_cards = cards_from_seed_venue(venue, meta)
        for card in seed_cards:
            merged[dedup_key(card)] = card

        if not args.offline:
            for card in cards_from_ccfddl(venue, meta):
                # ccfddl is fresher: it overrides a seed card with the same
                # (venue, year-month) and adds any new editions/cycles.
                merged[dedup_key(card)] = card

    cards = list(merged.values())
    # Sort: dated deadlines ascending (soonest first), TBD/undated last.
    cards.sort(key=lambda c: (c["primary_utc"] is None, c["primary_utc"] or ""))

    all_tags = sorted({t for c in cards for t in c["tags"]})
    payload = {
        "generated": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "scripts/conferences.seed.yml + ccfddl (best-effort)",
        "tags": all_tags,
        "conferences": cards,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)} with {len(cards)} cards "
          f"({'offline/seed only' if args.offline else 'seed + ccfddl'}).")


# --- minimal self-test for the timezone math (run: python update_deadlines.py --selftest)
def _selftest():
    checks = [
        (("2026-09-09 23:59:59", "AoE"), "2026-09-10T11:59:59Z"),   # AoE = UTC-12
        (("2025-12-04 17:59:00", "EST"), "2025-12-04T22:59:00Z"),   # EST = UTC-5
        (("2026-09-15 23:59:59", "AoE"), "2026-09-16T11:59:59Z"),
        (("2026-01-01 00:00:00", "UTC+9"), "2025-12-31T15:00:00Z"),
        (("2026-06-10 23:59:59", "AoE"), "2026-06-11T11:59:59Z"),
    ]
    ok = True
    for (s, tz), expect in checks:
        got, _ = to_utc(s, tz)
        status = "ok " if got == expect else "FAIL"
        if got != expect:
            ok = False
        print(f"  [{status}] {s} {tz} -> {got} (expected {expect})")
    assert to_utc("TBD", "AoE") == (None, "TBD")
    print("selftest:", "PASS" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    main()
