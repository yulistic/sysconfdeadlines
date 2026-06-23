# ⏰ Systems Conference Deadlines

A tiny, static webpage that shows live submission-deadline countdowns for top
systems venues — **SOSP, OSDI, ASPLOS, USENIX ATC, EuroSys, FAST, NSDI, HotOS,
HotStorage** — and keeps itself up to date automatically. No server, no
database, no build step in the browser.

- **Live countdowns** in AoE (Anywhere on Earth), shown in your local time too
- **Upcoming / Past / All** toggle and **topic filter** (OS, Architecture, Storage, Networking)
- **Search** by venue or location, **dark mode**
- **Auto-updates daily** via GitHub Actions (pulls fresh dates from the
  community-maintained [ccfddl](https://github.com/ccfddl/ccf-deadlines) dataset)

## How it works

```
index.html ─ assets/style.css ─ assets/app.js   ← the whole website (pure HTML/CSS/JS)
        │
        └─ reads → data/conferences.json          ← generated data the page renders

scripts/conferences.seed.yml   ← human-edited source of truth (deadlines you curate)
scripts/update_deadlines.py     ← converts seed (+ ccfddl) → data/conferences.json,
                                   turning each wall-clock deadline into a UTC instant
.github/workflows/update-deadlines.yml ← runs the script on a daily schedule and commits
```

The browser never parses timezones: the build step pre-computes each deadline as
an absolute UTC time, so the page just counts down to it.

## Deploy on GitHub Pages (one time)

1. Push this repo to GitHub (it already points at `yulistic/sysconfdeadlines`).
2. **Settings → Pages → Build and deployment → Source: _Deploy from a branch_**,
   then choose **`main`** / **`/ (root)`** and save.
3. After a minute your site is live at
   **https://yulistic.github.io/sysconfdeadlines/**
4. **Settings → Actions → General → Workflow permissions → _Read and write
   permissions_** (lets the daily job commit updated dates).

That's it. The daily Action regenerates `data/conferences.json`; Pages
re-publishes whenever `main` changes.

## Add or fix a conference

Edit **`scripts/conferences.seed.yml`** — for example:

```yaml
  MyConf:
    full_name: My Great Systems Conference
    tags: [OS]
    ccfddl: null            # or e.g. SC/osdi.yml to also pull from ccfddl
    editions:
      - edition: "'27"
        place: "Seoul, South Korea"
        date: "October 12-15, 2027"
        link: "https://example.org/cfp"
        timezone: AoE        # AoE | UTC+9 | EST | PT | …
        deadlines:
          - { label: Abstract, datetime: "2027-04-01 23:59:59" }
          - { label: Paper,    datetime: "2027-04-08 23:59:59" }
```

Commit the change. Pushing an edit to the seed re-runs the Action, which
regenerates the JSON and redeploys. To preview locally:

```bash
pip install -r scripts/requirements.txt
python scripts/update_deadlines.py --offline   # build from seed only
python scripts/update_deadlines.py --selftest  # check timezone conversions
python -m http.server 8000                      # then open http://localhost:8000
```

> Serve over `http://` (e.g. `python -m http.server`). Opening `index.html`
> directly as a `file://` won't load the JSON because browsers block `fetch`
> of local files.

## Notes on the data

- Deadlines are seeded from each venue's official Call for Papers (June 2026)
  and refreshed from ccfddl. **Always confirm against the official CFP** before
  relying on a date.
- HotOS and HotStorage are workshops not tracked by ccfddl, so they are
  maintained only in the seed file.
- Hosting alternatives: the exact same files work on **Cloudflare Pages** or
  **Netlify** — just point them at this repo. GitHub Pages is the least setup
  since the code already lives on GitHub.
