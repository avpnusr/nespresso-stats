# AGENTS.md

Guidance for agents/models working in this repo.

## What this is

Self-hosted Nespresso Vertuo capsule inventory dashboard. Python 3 stdlib
only — no pip installs, no build step, no framework. Keep it that way.

## Run / test

```bash
python3 server.py            # http://127.0.0.1:8787
python3 server.py --selftest # must pass
```

Env: `HOST`, `PORT`, `NESPRESSO_DB` (default `./data/nespresso.db`),
`CATALOGUE_URL` (new-capsule pull, empty disables), the
optional vision backend `VISION_BASE_URL` / `VISION_MODEL` / `VISION_API_KEY`,
`PUBLIC_HOST` (the dashboard's browsed-by-name host — `/api/*` through any other
name is refused, so set it when a proxy serves a hostname; IP/localhost access
works without; `X-Forwarded-Host` is client-supplied and only honoured when
`TRUST_PROXY` lists the proxy's IP/CIDR, so a rebinding page cannot forge it)
and `AUTH_TOKEN` (empty = open LAN-trust; set = every `/api/*` route requires
`Authorization: Bearer <token>`, and the page prompts for it once and keeps it in
localStorage — `#token=…` hands it over in one go).

## Layout

- `server.py` — HTTP server, SQLite storage, brew-matching + identify logic.
- `static/index.html` — the entire frontend (vanilla HTML/CSS/JS).
- `static/i18n/<code>.json` — UI translations. English (`en.json`) is the fallback;
  each file self-labels with a `language` field and is auto-discovered by the picker.
- `capsules.json` — starter catalogue of capsules the owner owns.
- `static/capsules/*.png` — official capsule photos referenced by the catalogue.
- `data/nespresso.db` — SQLite database (gitignored; seeds from `capsules.json`).

## Invariants / gotchas

- **Stdlib only.** If a change seems to need a dependency, it almost certainly
  doesn't; prefer `http.server`, `sqlite3`, `json`, `urllib`.
- **One machine, one capsule system.** `machine_line` (`vertuo` | `original`)
  scopes `current_state` (tiles, `known` picker), the cup sizes, identify and
  `brew_detected` — everything line-scoped goes through `machine_line(conn)` /
  `valid_line`. A capsule is stamped with the line in use when it is created; a
  family the selected line doesn't have means "unknown brew", not out-of-stock.
  `FAMILIES` is keyed by line; `normalize_family` needs the line because a bare
  *Lungo* is an Original cup size while Vertuo's long cup is a Gran Lungo.
- **Seeding is one-shot.** `init_db` only seeds `capsules.json` when the
  `capsules` table is empty. After editing the catalogue, delete
  `data/nespresso.db` (or `NESPRESSO_DB`) to re-seed. `*.db` is gitignored.
  New catalogue entries *do* reach an existing install through the known-capsule
  picker, which reads `capsules.json` directly.
- **First run is a stock-take.** A fresh seed ships every capsule at `count 0`
  with `setup_done` unset; the frontend then opens the setup dialog, whose
  `POST /api/setup` (`{counts: {id: count}}`) applies the counts, deletes rows
  left at 0 (unless a brew references them) and sets `setup_done`. An install
  that already has stock/brews, or a restored backup, is marked set up
  automatically so the dialog never appears on upgrade. The same dialog is
  re-openable from the Machine dialog ("Count every capsule"), so the endpoint
  stays writable after setup — a second device's stale dialog overwriting the
  counts is accepted on purpose.
- **Static assets are served at `/static/…`** by `Handler._serve_static`
  (with a path-traversal guard). New images go under `static/` and the
  catalogue `image` field holds the URL path, e.g.
  `/static/capsules/melozio.png`.
- **The selftest hardcodes capsule names.** `selftest()` needs `Fortado`,
  `Melozio` and `Intenso`, and also `Peru Organic` (with sku `7012.20`) plus
  both lines' `Colombia` / `Kahawa Ya Congo`; update it whenever the catalogue
  changes or `--selftest` breaks. It seeds the *whole* catalogue, so queries
  about "all capsules" need a `line` filter when they mean the Vertuo ones.
- **Colours feed identify.** `capsules.json` `color` is used by
  `rank_by_color`; sample it from the real capsule photo, don't guess. The
  Original Line entries are estimates from product photos (that line ships no
  capsule images yet) — replace them with sampled values when photos land.
- **`FAMILY_ALIASES` maps names** and the cloud `lastCoffeeFamilyID` (`1`–`7`, a
  Vertuo value) to `FAMILIES[line]`. A family with no in-stock capsules logs
  `out-of-stock`; several candidates queue a pending brew; a family the line
  doesn't have (or none at all) queues a familyless pending.
- **Frontend has no build.** Edit `static/index.html` directly; it fetches
  `/api/state` and posts to the `/api/*` endpoints.
- **A colour palette lives in three places.** One flat `html[data-theme="<id>"]`
  block in `static/index.html`, a matching `<option value="<id>">` in
  `#theme-select`, and a `theme.<id>` string in every `static/i18n/*.json`;
  `--selftest` fails when the three disagree. `auto` resolves to `light`/`dark`
  by `prefers-color-scheme`, and the choice is per browser (`localStorage`).
- **Schema changes need a migration.** `CREATE TABLE IF NOT EXISTS` never
  touches an existing table, so new columns must be added in `migrate()`
  (`price`, `threshold`, brew `delta`, capsule `sku` live there). It runs on
  every start.
- **Capsules are identified by `sku` first, name second.** `current_state`
  treats a catalogue entry as owned when its sku is in the inventory (or, for a
  hand-added row with no sku, its name). The store renames capsules, so never
  compare catalogue and inventory on name alone. `CATALOGUE_RENAMES` is the
  one-time old→new map `backfill_skus()` applies to pre-sku databases; add to it
  rather than editing existing rows when another rename lands.
- **Scanner pull.** `CATALOGUE_URL` (defaults to nespresso-scanner's
  `catalogue.json`, empty disables) is fetched by `scanner_daemon()` into
  memory and merged in by the pure `merge_catalogue()` — sku-first diff,
  `available: false` filtered out, line/family normalised, scoped to the
  machine's line. Selftest covers the merge with fixtures and a dead host;
  **selftest never touches the network**. A pull that finds genuinely new
  capsules for the machine's line also pushes them once via ntfy
  (`notify_new_capsules`; the first ever check baselines the current diff
  silently). `/api/state` may add `store_price` to a capsule when the store's
  current price (sku match, same line) moved away from what was paid — the
  frontend renders it as a ↓/↑ badge (`card.priceChange` in the i18n files).
  `/api/state` then carries
  `new_capsules` (the banner in `static/index.html`, dismissed per browser via
  `localStorage`).
- **Events, not just brews.** The `brews` table logs both brews (`delta < 0`)
  and restocks (`delta > 0`); `undo_brew` reverses a row's delta. `dec()` clamps
  `count` at 0 and returns the change it actually applied — log that, or undoing
  a brew made against an empty capsule credits stock that never existed. Each row also carries
  `line` (the capsule's line, else the machine's at log time), so a queued brew stays on
  its own line across a machine switch; a pre-column row has `line = ''` and counts for
  the line in use. Resolving or reassigning a brew to the other line is refused.
- Commit messages are conventional-commit style.

## API surface

`GET /api/state`, `/api/stats`, `/api/languages`, `/api/export`; `POST /api/capsules`, `/api/brew`,
`/api/brew-detected`, `/api/brews` (reassign), `/api/resolve`,
`/api/identify`, `/api/dismiss`, `/api/settings`, `/api/order` (tile order),
`/api/setup` (first-run stock-take),
`/api/maintenance` (log cleaning/descaling), `/api/import` (restore backup),
`/api/notify-test` (ntfy push);
`DELETE /api/capsules?id=N`, `/api/brews?id=N` (undo). `/api/state` carries
`machine_line`, `lines`, the line's `families` and only that line's `capsules`.
See README for payloads.
