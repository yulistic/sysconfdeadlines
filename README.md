# ⏰ Systems Conference Deadlines

A tiny, static webpage that shows live submission-deadline countdowns for top
systems venues — **SOSP, OSDI, ASPLOS, USENIX ATC, EuroSys, FAST, NSDI, HotOS,
HotStorage** — and keeps itself up to date by **reading each venue's official
Call for Papers** every week. No server, no database, no third-party data feed.

- Compact **table** with a pinned column header; rows scroll beneath it
- **Paper-submission deadline** as the focus, with a live AoE countdown; venues
  with multiple cycles (ASPLOS, EuroSys, FAST, NSDI) show one row per cycle
- **Upcoming / Past / All** toggle, **topic filter** (OS, Architecture, Storage, Networking), **search**, **dark mode**
- **Auto-updates weekly** via GitHub Actions — reads each venue's HotCRP
  `/deadlines` (authoritative) with the official CFP page as fallback

## How it works

```
index.html ─ assets/style.css ─ assets/app.js   ← the whole website (pure HTML/CSS/JS)
        │
        └─ reads → data/conferences.json          ← generated data the page renders

scripts/conferences.seed.yml    ← per-venue CFP url_template + curated fallback
scripts/update_deadlines.py      ← researches CFPs → writes data/conferences.json
.github/workflows/update-deadlines.yml ← runs it weekly and commits any changes
```

Each week, for every venue, the updater:

1. **Finds the current CFP.** The CFP URL only changes by year between editions
   (`osdi26`→`osdi27`, `fast27`→`fast28`, `asplos2027`→`asplos2028`, …), so it
   fills the year into the venue's `url_template`, probes the next couple of
   years, and keeps the newest edition whose page is live.
2. **Scrapes the submission deadlines** (abstract / paper / registration, with
   time and timezone) and the **conference dates and location** (best-effort:
   the latest multi-day date range on the page, plus a nearby "City, Country"),
   while ignoring notification and camera-ready dates.
3. **Converts to UTC** so the browser only counts down to an absolute instant.

For the **precise** submission deadline it also reads the conference's
**HotCRP `/deadlines`** page — the authoritative submission system, found from
HotCRP links on the CFP and from per-venue URL templates in the seed. HotCRP
values win where present; the CFP fills the rest (some editions, e.g. a
just-announced OSDI '27, are on the CFP but not yet on HotCRP). Deadlines that
fall exactly on an AoE day boundary are shown in AoE; others keep the timezone
HotCRP reports (e.g. NSDI's 8:59 PM PDT).

If a venue can't be fetched or yields nothing plausible, it keeps the curated
`fallback` from the seed, so the site is **never overwritten with bad data**.
Every run writes `data/update_log.txt` (and prints it in the Actions log) so you
can see exactly what each venue resolved to.

## Deploy on GitHub Pages (one time)

1. Push this repo to GitHub (it already points at `yulistic/sysconfdeadlines`).
2. **Settings → Pages → Source: _Deploy from a branch_**, choose **`main`** /
   **`/ (root)`**, save.
3. Live at **https://yulistic.github.io/sysconfdeadlines/** within a minute.
4. **Settings → Actions → General → Workflow permissions → _Read and write_**
   (lets the weekly job commit refreshed dates).

> This is a **project site** served under `/sysconfdeadlines/`. It does **not**
> conflict with your user site at `https://yulistic.github.io/` (a separate
> repo served at the root) — a GitHub account can host one user site plus many
> project sites at once.

## Add or fix a venue

Everything lives in **`scripts/conferences.seed.yml`**. To add a venue, give it
a CFP `url_template` (use `{yy}` for a 2-digit year or `{yyyy}` for 4-digit) and
a `fallback` used until/unless scraping succeeds:

```yaml
  MyConf:
    full_name: My Great Systems Conference
    tags: [OS]
    url_template: "https://myconf.org/{yyyy}/cfp.html"
    timezone: AoE
    # biennial_odd: true        # uncomment for odd-year-only venues (like HotOS)
    fallback:
      year: 2027
      edition: "'27"
      place: "Seoul, South Korea"
      date: "October 12-15, 2027"
      link: "https://myconf.org/2027/cfp.html"
      timezone: AoE
      deadlines:
        - { label: Abstract, datetime: "2027-04-01 23:59:59" }
        - { label: Paper,    datetime: "2027-04-08 23:59:59" }
```

Pushing an edit to the seed re-runs the Action, which regenerates the JSON and
redeploys.

## Local preview

```bash
pip install -r scripts/requirements.txt
python scripts/update_deadlines.py --selftest   # timezone + scraper unit tests
python scripts/update_deadlines.py --offline    # build from fallbacks (no network)
python scripts/update_deadlines.py              # research live CFPs
python -m http.server 8000                       # open http://localhost:8000
```

> Serve over `http://` (e.g. `python -m http.server`). Opening `index.html` as a
> `file://` won't load the JSON because browsers block `fetch` of local files.

## Notes on the data

- Deadlines are read from the official CFP pages; the curated fallbacks were
  taken from those same pages in June 2026. **Always confirm against the
  official CFP** before relying on a date.
- Scraping is heuristic. When a venue's page changes layout the updater simply
  keeps the previous good data rather than guessing — check `data/update_log.txt`
  if a venue looks stale and, if needed, pin it via its `fallback`.
- HotStorage's CFP URL varies year to year (often co-located / hosted ad hoc),
  so it most often falls back to the curated entry.
- Hosting alternatives: the exact same files work on **Cloudflare Pages** or
  **Netlify** — point them at this repo. GitHub Pages needs the least setup.
