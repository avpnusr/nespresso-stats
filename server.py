#!/usr/bin/env python3
"""Nespresso capsule inventory dashboard (Vertuo and Original Line).

Stdlib only: http.server + sqlite3 + json. No dependencies, no build step.

Run:  python3 server.py           → http://127.0.0.1:8787
Test: python3 server.py --selftest
"""
from __future__ import annotations

import difflib
import ast
import json
import mimetypes
import os
import re
import sqlite3
import sys
import tempfile
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

mimetypes.add_type("application/manifest+json", ".webmanifest")  # Alpine's mime db lacks it

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("NESPRESSO_DB", ROOT / "data" / "nespresso.db"))
CATALOG = ROOT / "capsules.json"
INDEX = ROOT / "static" / "index.html"

# The store renamed its Original Line capsules in 2026 ("Ispirazione Firenze Arpeggio" is now
# just "Arpeggio"). One migration renames the inventory rows to match; capsules the store
# delisted are not in here and simply keep the name the owner already has.
CATALOGUE_RENAMES = {
    "Ispirazione Ristretto Italiano": "Ristretto",
    "Ispirazione Ristretto Decaffeinato": "Ristretto Decaffeinato",
    "Ispirazione Palermo Kazaar": "Kazaar",
    "Ispirazione Firenze Arpeggio": "Arpeggio",
    "Ispirazione Genova Livanto": "Livanto",
    "Decaffeinato Volluto": "Volluto Decaffeinato",
    "Vienna Linizio Lungo": "Vienna Lungo",
    "Tokyo Vivalto Lungo": "Tokyo Lungo",
    "Stockholm Fortissio Lungo": "Stockholm Lungo",
    "Cape Town Envivo Lungo": "Cape Town Lungo",
    "Master Origins Colombia": "Colombia",
    "Master Origins India": "India",
    "Master Origins Indonesia": "Indonesia",
    "Master Origins Nicaragua": "Nicaragua",
    "Master Origins Kahawa ya Congo": "Kahawa Ya Congo",
}
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8787"))

# Capsule systems ("lines"). The machine is configured as exactly one of them, which decides
# the cup sizes it brews, the capsules the dashboard shows, and what the hardware can report:
# a Vertuo machine scans the pod's barcode, an Original Line machine cannot.
FAMILIES = {
    "vertuo": ["Espresso", "Double Espresso", "Gran Lungo", "Mug", "Alto", "Carafe", "Alto XL"],
    "original": ["Ristretto", "Espresso", "Lungo"],
}
LINE_LABELS = {"vertuo": "Vertuo", "original": "Original"}
DEFAULT_LINE = "vertuo"

# Every pod prints its cup size, so those words can never identify one on their own (see the
# single-word fallback in capsule_names_in): "ESPRESSO" alone is not the Festive Espresso.
FAMILY_WORDS = {w for fams in FAMILIES.values() for f in fams for w in f.lower().split()}

# Recurring upkeep: task -> (label, default interval in days). Cleaning every 10 days,
# descaling roughly every 3 months. The interval is editable in the Machine dialog and
# stored as a `<task>_days` setting; these values are the fallback when none is set.
MAINTENANCE = {"clean": ("Cleaning", 10), "descale": ("Descaling", 90)}

# Optional vision backend (any OpenAI-compatible endpoint). When unset, identification
# falls back to colour ranking only.
VISION_BASE_URL = os.environ.get("VISION_BASE_URL", "").rstrip("/")
VISION_MODEL = os.environ.get("VISION_MODEL", "")
VISION_API_KEY = os.environ.get("VISION_API_KEY", "ollama")
FAMILY_ALIASES = {
    "espresso": "Espresso", "espresso lungo": "Espresso",
    "double espresso": "Double Espresso", "doppio": "Double Espresso",
    "gran lungo": "Gran Lungo", "lungo": "Gran Lungo",
    "mug": "Mug", "coffee": "Mug",
    "alto": "Alto", "alto xl": "Alto XL",
    "carafe": "Carafe", "pour over": "Carafe",
    # Nespresso cloud lastCoffeeFamilyID values
    "1": "Espresso", "2": "Double Espresso", "3": "Gran Lungo",
    "4": "Mug", "5": "Alto", "6": "Carafe", "7": "Alto XL",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# A bare "Lungo" is the Original Line's long cup (110 ml); Vertuo's long cup is a Gran Lungo,
# which is what the shared alias resolves to. Without the override an Original machine would
# report a cup size its line does not have.
FAMILY_ALIASES_BY_LINE = {
    "original": {"lungo": "Lungo", "espresso lungo": "Lungo",
                 "café": "Espresso", "caffe": "Espresso"},
}


def valid_line(value) -> str:
    return value if value in FAMILIES else DEFAULT_LINE


def machine_line(conn) -> str:
    """The configured capsule system. Every line-scoped decision reads it from here."""
    return valid_line(get_setting(conn, "machine_line", DEFAULT_LINE))


def entry_line(entry: dict) -> str:
    """A catalogue entry's line; the Vertuo entries predate the field and have none."""
    return valid_line(entry.get("line"))


def normalize_family(value, line: str = DEFAULT_LINE) -> str:
    if value is None:
        return "Unknown"
    key = str(value).strip().lower()
    return (FAMILY_ALIASES_BY_LINE.get(line, {}).get(key) or FAMILY_ALIASES.get(key)
            or str(value).strip().title())


def connect(path=DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # Threads of ThreadingHTTPServer share the db file; wait instead of erroring on
    # a writer, and let readers proceed while a write commits.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection, seed_path: Path = CATALOG) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS capsules (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT NOT NULL,
            sku       TEXT NOT NULL DEFAULT '',
            family    TEXT NOT NULL,
            color     TEXT NOT NULL DEFAULT '#6b4f3a',
            image     TEXT NOT NULL DEFAULT '',
            count     INTEGER NOT NULL DEFAULT 0,
            notes     TEXT NOT NULL DEFAULT '',
            price     REAL NOT NULL DEFAULT 0,
            threshold INTEGER NOT NULL DEFAULT 2,
            intensity INTEGER NOT NULL DEFAULT 0,
            line      TEXT NOT NULL DEFAULT 'vertuo'
        );
        CREATE TABLE IF NOT EXISTS brews (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            capsule_id INTEGER,
            family     TEXT,
            ts         TEXT NOT NULL,
            source     TEXT NOT NULL,
            delta      INTEGER NOT NULL DEFAULT -1
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    migrate(conn)
    for key, value in (("machine_name", "My Vertuo"), ("machine_model", "Vertuo"),
                       ("machine_line", DEFAULT_LINE),
                       ("machine_image", ""), ("sleeve_size", "10")):
        conn.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (key, value))
    if conn.execute("SELECT COUNT(*) FROM capsules").fetchone()[0] == 0 and seed_path.exists():
        seed = json.loads(seed_path.read_text())
        conn.executemany(
            "INSERT INTO capsules (name, sku, family, color, image, intensity, special, line)"
            " VALUES (:name, :sku, :family, :color, :image, :intensity, :special, :line)",
            [{**c, "image": c.get("image", ""), "sku": c.get("sku", ""),
              "intensity": c.get("intensity", 0),
              "special": 1 if c.get("special") else 0, "line": entry_line(c)} for c in seed],
        )
    # A fresh seed (every count 0, no brews) gets the first-run stock-take; an install that
    # already has stock or history is marked set up so the dialog never appears on upgrade.
    if not conn.execute("SELECT 1 FROM settings WHERE key = 'setup_done'").fetchone() and (
        conn.execute("SELECT 1 FROM capsules WHERE count > 0 LIMIT 1").fetchone()
        or conn.execute("SELECT 1 FROM brews LIMIT 1").fetchone()
    ):
        conn.execute("INSERT INTO settings VALUES ('setup_done', '1')")
    conn.commit()


def migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release (old DBs)."""
    capsule_cols = {r[1] for r in conn.execute("PRAGMA table_info(capsules)")}
    if "line" not in capsule_cols:
        # Everything an older install holds was Vertuo (the only line back then) → default is
        # the correct backfill; only the catalogue's new Original entries carry a line.
        conn.execute("ALTER TABLE capsules ADD COLUMN line TEXT NOT NULL DEFAULT 'vertuo'")
    if "price" not in capsule_cols:
        conn.execute("ALTER TABLE capsules ADD COLUMN price REAL NOT NULL DEFAULT 0")
    if "threshold" not in capsule_cols:
        conn.execute("ALTER TABLE capsules ADD COLUMN threshold INTEGER NOT NULL DEFAULT 2")
    if "intensity" not in capsule_cols:
        conn.execute("ALTER TABLE capsules ADD COLUMN intensity INTEGER NOT NULL DEFAULT 0")
        # Backfill once, so an existing install gets the catalogue's intensities instead of
        # needing a destructive re-seed. Rows the owner later sets to 0 are left alone.
        if CATALOG.exists():
            for c in json.loads(CATALOG.read_text()):
                if c.get("intensity"):
                    conn.execute(
                        "UPDATE capsules SET intensity = ? WHERE name = ? AND intensity = 0",
                        (c["intensity"], c["name"]),
                    )
    if "special" not in capsule_cols:
        conn.execute("ALTER TABLE capsules ADD COLUMN special INTEGER NOT NULL DEFAULT 0")
    if "sku" not in capsule_cols:
        conn.execute("ALTER TABLE capsules ADD COLUMN sku TEXT NOT NULL DEFAULT ''")
        backfill_skus(conn)
    elif CATALOG.exists():
        # The catalogue picked up skus after this install launched (capsules the store had
        # delisted when the owner added them) — stamp them onto the still-blank rows.
        fill_empty_skus(conn)
    brew_cols = {r[1] for r in conn.execute("PRAGMA table_info(brews)")}
    if "delta" not in brew_cols:
        conn.execute("ALTER TABLE brews ADD COLUMN delta INTEGER NOT NULL DEFAULT -1")
    conn.commit()


def _catalogue_skus() -> dict[tuple[str, str], str]:
    """(name, line) -> sku from the catalogue. Keyed by line as well as name: Colombia and
    Kahawa Ya Congo exist on both lines, so a name-only match would stamp one line's capsule
    with the other line's sku."""
    if not CATALOG.exists():
        return {}
    return {(c["name"], entry_line(c)): c["sku"] for c in json.loads(CATALOG.read_text())
            if c.get("sku")}


def backfill_skus(conn: sqlite3.Connection) -> None:
    """One-time for DBs that predate the sku column: stamp each row with the catalogue sku it
    belongs to and rename the rows whose store name changed. A row only gains a sku while it is
    blank, and a rename never lands on a name the owner already uses (the catalogue is the
    source of both the new name and its sku)."""
    if not CATALOG.exists():
        return
    # name -> sku, last entry winning: two rename targets are shared with the Vertuo line
    # (Colombia, Kahawa Ya Congo) and the Original Line entry is the one that comes last.
    sku_by_name = {c["name"]: c["sku"] for c in json.loads(CATALOG.read_text()) if c.get("sku")}
    for old, new in CATALOGUE_RENAMES.items():
        sku = sku_by_name.get(new)
        if sku:
            conn.execute(
                "UPDATE capsules SET name = ?, sku = ? WHERE name = ? AND sku = ''"
                " AND NOT EXISTS (SELECT 1 FROM capsules WHERE name = ?)",
                (new, sku, old, new),
            )
    fill_empty_skus(conn)


def fill_empty_skus(conn: sqlite3.Connection) -> None:
    """Stamp a row that still has no sku, whenever the catalogue learned it (e.g. a capsule
    the store had delisted when the owner added it). Never overwrites a set sku."""
    for (name, line), sku in _catalogue_skus().items():
        conn.execute("UPDATE capsules SET sku = ? WHERE name = ? AND line = ? AND sku = ''",
                     (sku, name, line))
    conn.commit()


# --------------------------------------------------------------------------- logic

def dec(conn, capsule_id: int, delta: int = -1) -> None:
    conn.execute(
        "UPDATE capsules SET count = MAX(0, count + ?) WHERE id = ?", (delta, capsule_id)
    )


def log_brew(conn, capsule_id, family, source, ts=None, delta=-1) -> int:
    cur = conn.execute(
        "INSERT INTO brews (capsule_id, family, ts, source, delta) VALUES (?, ?, ?, ?, ?)",
        (capsule_id, family, ts or now_iso(), source, delta),
    )
    return cur.lastrowid


def brew_detected(conn, family, source="auto", capsule=None, line=None) -> dict:
    """A machine (or a Stream Deck key) reported a brew. An exact capsule name wins — a key
    knows which pod it was — otherwise fall back to `family`: decrement if unambiguous, else
    queue it. The name is matched like a photo read, so 'Intenso' or a typo both work.
    A family the machine's line doesn't have (an Original Line machine reports no cup size at
    all) queues a pending without one, so the dashboard can offer the line's whole stock."""
    line = valid_line(line or machine_line(conn))
    if capsule:
        hit = match_capsule(conn, capsule, line)
        if hit:  # known capsule name, no family guessing needed
            dec(conn, hit["id"])
            log_brew(conn, hit["id"], hit["family"], source)
            conn.commit()
            return {"action": "decremented", "family": hit["family"], "capsule": hit["name"]}
    fam = normalize_family(family, line)
    if fam not in FAMILIES[line]:
        # No usable cup size — the machine (or the caller) told us nothing this line can brew.
        return _queue_unknown(conn, line, source)
    rows = conn.execute(
        "SELECT id, name, count FROM capsules WHERE line = ? AND lower(family) = lower(?)"
        " AND count > 0 ORDER BY name", (line, fam)
    ).fetchall()
    if len(rows) == 1:
        dec(conn, rows[0]["id"])
        log_brew(conn, rows[0]["id"], fam, source)
        conn.commit()
        return {"action": "decremented", "family": fam, "capsule": rows[0]["name"]}
    log_brew(conn, None, fam, source)
    conn.commit()
    if not rows:
        in_stock = conn.execute(
            "SELECT COUNT(*) FROM capsules WHERE line = ? AND lower(family) = lower(?)", (line, fam)
        ).fetchone()[0]
        return {"action": "unknown" if not in_stock else "out-of-stock", "family": fam}
    return {"action": "pending", "family": fam, "candidates": [r["name"] for r in rows]}


def _queue_unknown(conn, line: str, source: str) -> dict:
    """A brew whose cup size we don't know (Original Line, or a family this line doesn't have).
    Queue it without a family: the owner picks from everything in stock on that line."""
    rows = conn.execute(
        "SELECT name FROM capsules WHERE line = ? AND count > 0 ORDER BY name", (line,)
    ).fetchall()
    if not rows:
        return {"action": "unknown", "family": None}
    log_brew(conn, None, None, source)
    conn.commit()
    return {"action": "pending", "family": None, "candidates": [r["name"] for r in rows]}


def resolve_pending(conn, capsule_id: int, brew_id: int | None = None) -> dict:
    row = conn.execute("SELECT id, family FROM capsules WHERE id = ?", (capsule_id,)).fetchone()
    if row is None:
        raise ValueError("capsule not found")
    if brew_id is None:
        pend = conn.execute(
            "SELECT id FROM brews WHERE capsule_id IS NULL AND delta < 0 ORDER BY id LIMIT 1"
        ).fetchone()
        brew_id = pend["id"] if pend else None
    else:
        # a client-supplied id must point at a genuinely pending brew — resolving an
        # already-assigned one would double-decrement the capsule
        row = conn.execute("SELECT capsule_id, delta FROM brews WHERE id = ?", (brew_id,)).fetchone()
        if row is None or row["capsule_id"] is not None or row["delta"] >= 0:
            raise ValueError("not a pending brew")
    if brew_id is None:
        raise ValueError("no pending brew")
    dec(conn, capsule_id)
    conn.execute("UPDATE brews SET capsule_id = ? WHERE id = ?", (capsule_id, brew_id))
    conn.commit()
    return {"action": "resolved", "capsule": row["family"]}


def dismiss_pending(conn, brew_id: int) -> dict:
    conn.execute("DELETE FROM brews WHERE id = ? AND capsule_id IS NULL", (brew_id,))
    conn.commit()
    return {"action": "dismissed"}


def undo_brew(conn, brew_id: int) -> dict:
    """Reverse a logged brew/restock and remove it from history."""
    row = conn.execute("SELECT capsule_id, delta FROM brews WHERE id = ?", (brew_id,)).fetchone()
    if row is None:
        raise ValueError("brew not found")
    if row["capsule_id"] is not None:
        dec(conn, row["capsule_id"], -row["delta"])
    conn.execute("DELETE FROM brews WHERE id = ?", (brew_id,))
    conn.commit()
    return {"action": "undone"}


def set_brew_capsule(conn, brew_id: int, capsule_id: int) -> dict:
    """Reassign (or resolve) a brew to a different capsule, fixing the counts."""
    row = conn.execute("SELECT capsule_id, delta FROM brews WHERE id = ?", (brew_id,)).fetchone()
    target = conn.execute("SELECT family FROM capsules WHERE id = ?", (capsule_id,)).fetchone()
    if row is None:
        raise ValueError("brew not found")
    if target is None:
        raise ValueError("capsule not found")
    if row["delta"] >= 0:
        raise ValueError("only brews can be reassigned")
    if row["capsule_id"] is not None and row["capsule_id"] != capsule_id:
        dec(conn, row["capsule_id"], -row["delta"])
    if row["capsule_id"] != capsule_id:
        dec(conn, capsule_id, row["delta"])
    conn.execute(
        "UPDATE brews SET capsule_id = ?, family = ? WHERE id = ?",
        (capsule_id, target["family"], brew_id),
    )
    conn.commit()
    return {"action": "reassigned"}


def delete_capsule(conn, capsule_id: int) -> dict:
    """Deleting a capsule that has brew history would orphan those rows: the activity list
    loses the name and the spend KPI the price. Same rule as the stock-take — a capsule you
    have used is retired by setting its count to 0."""
    if not conn.execute("SELECT 1 FROM capsules WHERE id = ?", (capsule_id,)).fetchone():
        raise ValueError("capsule not found")
    if conn.execute("SELECT 1 FROM brews WHERE capsule_id = ? LIMIT 1", (capsule_id,)).fetchone():
        raise ValueError("capsule has brew history: set its count to 0 instead of deleting it")
    conn.execute("DELETE FROM capsules WHERE id = ?", (capsule_id,))
    conn.commit()
    return {"ok": True}


def brew_stats(conn) -> dict:
    """Raw consumption events for the client to aggregate (timezone-local), scoped to the
    machine's line — the other line's history must not feed this machine's days-left or
    average. A brew that never named a capsule (unknown brew) counts for the line in use;
    one orphaned by a pre-2026 deletion has no line at all and stays out."""
    rows = conn.execute(
        """SELECT b.ts, b.capsule_id, b.delta FROM brews b
           LEFT JOIN capsules c ON c.id = b.capsule_id
           WHERE b.capsule_id IS NULL OR c.line = ?
           ORDER BY b.id DESC LIMIT 5000""", (machine_line(conn),))
    return {"brews": [dict(r) for r in rows]}


# ------------------------------------------------------------------- identification

def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = str(value or "").lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (128, 128, 128)


def color_distance(a: str, b: str) -> float:
    """Redmean colour distance — cheap and better than plain RGB for skin/coffee tones."""
    r1, g1, b1 = hex_to_rgb(a)
    r2, g2, b2 = hex_to_rgb(b)
    rbar = (r1 + r2) / 2
    dr, dg, db = r1 - r2, g1 - g2, b1 - b2
    return ((2 + rbar / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rbar) / 256) * db * db) ** 0.5


def rank_by_color(conn, color: str, limit: int = 8, line: str | None = None) -> list[dict]:
    """Closest capsule colours first. `line` (the machine's capsule system) keeps a Vertuo pod
    out of an Original Line shortlist; None ranks across every line."""
    rows = [dict(r) for r in conn.execute(
        "SELECT id, name, family, color, image, count FROM capsules"
        + (" WHERE line = ?" if line else ""), (line,) if line else ()
    )]
    for row in rows:
        row["distance"] = round(color_distance(color, row["color"]))
    rows.sort(key=lambda r: r["distance"])
    return rows[:limit]


def capsule_names_in(conn, text: str, rows=None, line: str | None = None) -> list[dict]:
    """Capsules whose full name is readable in a transcription, longest name first. A partial
    read matches nothing — 'DOUBLE ESPRESSO' fits three flavours, 'DOUBLE ESPRESSO CHIARO' one.
    Tokens count wherever they appear, since a pod prints its name around the base twice and a
    transcription happily reads them out of order. Matching the transcription (not the model's
    answer) is what stops a shortlisted name being handed back as a 'read'. Pass `rows` to match
    against the catalogue instead of the inventory, `line` to stay in one capsule system."""
    words = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    if rows is None:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, name, family, color, image, count FROM capsules"
            + (" WHERE line = ?" if line else ""), (line,) if line else ())]
    hits = []
    for row in rows:
        tokens = set(re.findall(r"[a-z0-9]+", row["name"].lower()))
        if tokens and tokens <= words:
            hits.append(row)
    hits.sort(key=lambda r: -len(r["name"]))
    # Two equally specific names (e.g. a box listing several capsules) is not an identification.
    if len(hits) > 1 and len(hits[0]["name"]) == len(hits[1]["name"]):
        return []
    if hits or not words:
        return hits
    # No full name, but a one-word misread still resolves ('VOLTESO' → Voltesso) — only when
    # every resolvable word points at the same capsule, and never on a cup-size word.
    found = {}
    for word in words - FAMILY_WORDS:
        r = match_capsule(conn, word, line)
        if r:
            found[r["id"]] = r
    return list(found.values()) if len(found) == 1 else []


def match_capsule(conn, name: str, line: str | None = None) -> dict | None:
    """Resolve a name read off a photo to a capsule. Small models misspell what they read and
    often only catch part of it ('CHIARO' for Double Espresso Chiaro), and a bottom-of-pod photo
    is silver, which shortlists the wrong capsules entirely — so match the printed name against
    the whole catalogue. `line` restricts the search to the machine's system, so a Vertuo name
    is never matched on an Original Line machine. ponytail: difflib + a unique-token rule, no
    fuzzy scoring of our own."""
    rows = [dict(r) for r in conn.execute(
        "SELECT id, name, family, color, image, count FROM capsules"
        + (" WHERE line = ?" if line else ""), (line,) if line else ()
    )]
    keys = {re.sub(r"[^a-z0-9]", "", r["name"].lower()): r for r in rows}
    names = {r["id"]: re.findall(r"[a-z0-9]+", r["name"].lower()) for r in rows}
    needle = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    tokens = re.findall(r"[a-z0-9]+", (name or "").lower())
    if not tokens or set(tokens) <= FAMILY_WORDS:
        # a bare cup size ("espresso", "double espresso") names no capsule: every pod prints one
        return None
    if needle in keys:
        return keys[needle]
    # A partial read is only usable when exactly one name ends with it: 'CHIARO' → Double
    # Espresso Chiaro, while 'DOUBLE ESPRESSO' fits several flavours and stays a colour question.
    tails = [r for r in rows if names[r["id"]][-len(tokens):] == tokens]
    if len(tails) == 1:
        return tails[0]
    # Typo fallback ('VOLTESO'), refusing to guess when two names fit equally well.
    scored = sorted(((difflib.SequenceMatcher(None, needle, k).ratio(), k) for k in keys), reverse=True)
    if scored and scored[0][0] >= 0.8 and (len(scored) == 1 or scored[1][0] < scored[0][0]):
        return keys[scored[0][1]]
    return None


def vision_pick(image_data_url: str, candidates: list[dict], line: str = DEFAULT_LINE) -> dict | None:
    """Ask an OpenAI-compatible vision model. Exact when the photo shows a printed name — on a
    capsule the name sits next to the cup size (Vertuo: around the aluminium base; Original: on
    the top foil) or on a sleeve/box. On a top-down photo of the bare dome it only refines colour."""
    if not (VISION_BASE_URL and VISION_MODEL) or not image_data_url:
        return None
    names = [c["name"] for c in candidates]
    prompt = (
        f"This is a photo of a Nespresso {LINE_LABELS[line]} capsule or its packaging. The "
        "capsule name is printed on the pod itself, next to the cup size, and on sleeves and "
        "boxes. Transcribe every word you can actually read into 'text' — never "
        "complete a word you cannot read, and never copy a word from the list below. Then set "
        "'name' to the capsule name exactly as it appears in 'text'; only if the image shows no "
        "legible text at all, set 'name' to the closest colour match from this list (name one): " + ", ".join(names) +
        '. Reply with compact JSON only, reason under 10 words: '
        '{"text": <transcribed text or empty>, "name": <name or null>, "reason": <short>}'
    )
    body = json.dumps({
        "model": VISION_MODEL, "reasoning_effort": "none", "temperature": 0, "max_tokens": 500,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ]}],
    }).encode()
    req = urllib.request.Request(
        f"{VISION_BASE_URL}/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {VISION_API_KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            content = json.load(resp)["choices"][0]["message"].get("content", "")
    except Exception as err:  # noqa: BLE001 - vision is best-effort
        return {"error": str(err)}
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        return {"error": "unparseable", "raw": content[:200]}
    try:
        picked = json.loads(match.group(0))
    except json.JSONDecodeError:
        # Small models wrap JSON in fences and quote with single quotes. ponytail: swap the
        # JSON literals and hand it to literal_eval, rather than reach for a repair library.
        try:
            picked = ast.literal_eval(re.sub(
                r"\b(null|true|false)\b",
                lambda m: {"null": "None", "true": "True", "false": "False"}[m.group(1)],
                match.group(0)))
        except (ValueError, SyntaxError):
            return {"error": "unparseable", "raw": content[:200]}
    if not isinstance(picked, dict):
        return {"error": "unparseable", "raw": content[:200]}
    return {"name": picked.get("name"), "reason": picked.get("reason", ""),
            "text": picked.get("text", "")}


def identify(conn, color: str, image_data_url: str | None = None, line: str | None = None) -> dict:
    """Closest capsules to a photo. Everything here is scoped to the machine's capsule system:
    an Original Line machine must never be offered a Vertuo pod."""
    line = valid_line(line or machine_line(conn))
    candidates = rank_by_color(conn, color, line=line)
    result = {"color": color, "candidates": candidates, "vision": None,
              "vision_enabled": bool(VISION_BASE_URL and VISION_MODEL),
              "line": line}
    if image_data_url:
        result["vision"] = vision_pick(image_data_url, candidates, line)
    vis = result["vision"]
    if vis and not vis.get("error"):
        text = (vis.get("text") or "").strip()
        hits = capsule_names_in(conn, text, line=line) if text else []
        if hits:  # a name read off the pod outranks the colour shortlist
            vis["name"] = hits[0]["name"]
            result["candidates"] = [hits[0]] + [c for c in candidates if c["id"] != hits[0]["id"]]
        elif text:
            # Legible, but no capsule by that name is in the inventory. If the catalogue knows it,
            # say so — the add dialog can prefill it from there — rather than fall back to colour.
            cat = capsule_names_in(conn, text, rows=[c for c in known_catalogue()
                                                     if entry_line(c) == line])
            vis["name"] = cat[0]["name"] if cat else None
            vis["not_owned"] = bool(cat)
        elif vis.get("name"):
            # No text at all: the model's colour opinion is worth less than the colour ranking
            # the browser already did, so report it without reordering the candidates.
            hit = match_capsule(conn, vis["name"], line)
            if hit:
                vis["name"] = hit["name"]
    return result


def set_capsule_order(conn, ids: list[int]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings VALUES ('capsule_order', ?)",
        (",".join(str(int(i)) for i in ids),),
    )
    conn.commit()


def save_setup(conn: sqlite3.Connection, counts: dict) -> dict:
    """First-run stock-take: apply the counts the owner entered and drop the seed rows they
    left at 0 (a row with brew history is kept). Ids not sent are left untouched."""
    if not isinstance(counts, dict) or not counts:
        raise ValueError("counts must be a non-empty object")
    line = machine_line(conn)
    own = {r["id"] for r in conn.execute("SELECT id FROM capsules WHERE line = ?", (line,))}
    updated = removed = 0
    for key, value in counts.items():
        try:
            cid, count = int(key), max(0, int(value or 0))
        except (TypeError, ValueError):
            continue
        if cid not in own:
            continue  # the dialog only lists the machine's own line
        conn.execute("UPDATE capsules SET count = ? WHERE id = ?", (count, cid))
        updated += 1
        if count == 0 and not conn.execute(
            "SELECT 1 FROM brews WHERE capsule_id = ? LIMIT 1", (cid,)
        ).fetchone():
            conn.execute("DELETE FROM capsules WHERE id = ?", (cid,))
            removed += 1
    conn.execute("INSERT OR REPLACE INTO settings VALUES ('setup_done', '1')")
    conn.commit()
    return {"ok": True, "updated": updated, "removed": removed}


def log_maintenance(conn, task: str) -> dict:
    """Record a cleaning/descaling. Logged as an event (delta 0) so it shows in activity."""
    if task not in MAINTENANCE:
        raise ValueError("unknown maintenance task")
    log_brew(conn, None, None, task, delta=0)
    conn.commit()
    return {"action": "logged", "task": task}


def interval_days(conn, task: str, default: int) -> int:
    """Configured interval for a task, falling back to the default if unset or invalid."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (task + "_days",)).fetchone()
    if not row:
        return default
    try:
        n = int(row["value"])
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def maintenance_state(conn) -> list[dict]:
    """Last-done / next-due for each recurring upkeep task."""
    now = datetime.now(timezone.utc)
    out = []
    for task, (label, default_days) in MAINTENANCE.items():
        days = interval_days(conn, task, default_days)
        row = conn.execute(
            "SELECT ts FROM brews WHERE source = ? ORDER BY id DESC LIMIT 1", (task,)
        ).fetchone()
        last = row["ts"] if row else None
        nxt = datetime.fromisoformat(last) + timedelta(days=days) if last else None
        out.append({
            "task": task, "label": label, "every_days": days, "last": last,
            "next": nxt.isoformat(timespec="seconds") if nxt else None,
            "due": nxt is None or now >= nxt,
        })
    return out


# ------------------------------------------------------------------ notifications

def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def ntfy_host_ok(url: str) -> None:
    """A client-chosen test target must stay on an ntfy host. Without this, a client
    could make the server POST to arbitrary LAN hosts — a server-driven port scan."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if p.scheme == "https" and (host == "ntfy.sh" or host.endswith(".ntfy.sh")):
        return
    if host in ("localhost", "127.0.0.1"):
        return
    raise ValueError("test target must be ntfy.sh or localhost")


def ntfy_url_ok(url: str) -> str:
    """A *stored* topic URL must be plain http(s) without credentials. A file:/ or other scheme
    makes the server read a local path on every state change, and `user:pass@` would put a
    password into the push. Private hosts stay allowed on purpose — a self-hosted ntfy on the
    LAN is the normal case, and the owner configures this, unlike a client-chosen test target."""
    p = urlparse((url or "").strip())
    # Non-ASCII cannot go into a request line (urllib fails), and the masked value /api/state
    # hands out must never be saved back as a "topic" — an old cached page would do that.
    if not url.isascii() or any(c.isspace() for c in url):
        raise ValueError("notify URL must be an http(s) topic URL")
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise ValueError("notify URL must be an http(s) topic URL")
    return url.strip()


def mask_ntfy(url: str) -> str:
    """What /api/state hands out: the host is enough to recognise the topic, the topic itself
    is a bearer secret — anyone who knows it can publish to the owner's phone (and read the
    pushes). /api/export keeps the real URL, a backup has to restore."""
    p = urlparse(url or "")
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}/•••" if p.path.strip("/") else f"{p.scheme}://{p.netloc}"


def ntfy_send(url: str, message: str, title: str = "Nespresso") -> str | None:
    """POST to an ntfy topic (ntfy.sh or self-hosted). Returns an error string, or None."""
    if urlparse(url).scheme not in ("http", "https"):
        return "notify target must be http(s)"  # never let a stored URL read a local file
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=message.encode(), headers={"Title": title}),
            timeout=10,
        ):
            return None
    except Exception as err:  # noqa: BLE001 - notifications are best-effort
        return str(err)


def notify_check(conn: sqlite3.Connection, send=None) -> None:
    """After any state change: push one reminder per capsule that just ran low and per
    overdue upkeep task — at most once per episode; restocking or logging the task arms
    the next one. A queued brew is announced once per pending row. Messages are English
    (the server doesn't know the reader's UI language).
    ponytail: episodes are tracked in settings keys, not a new table."""
    url = get_setting(conn, "ntfy_url")
    if not url:
        return  # unconfigured: don't record episodes either, so configuring it later
    send = send or ntfy_send    # immediately reports what is low / due right now
    # Only the machine's own line can run low for its owner; the other line is not in use.
    line = machine_line(conn)
    low = {r["id"] for r in conn.execute(
        "SELECT id FROM capsules WHERE count <= threshold AND line = ?", (line,))}
    # Episodes are per line: a line switch must not re-announce what was already pushed.
    notified = {int(x) for x in get_setting(conn, f"notified_low_{line}").split(",") if x.isdigit()}
    for cid in sorted(low - notified):
        r = conn.execute("SELECT name, count FROM capsules WHERE id = ?", (cid,)).fetchone()
        if r:  # a failed send still marks the episode done — a retry storm is worse
            err = send(url, f"{r['name']}: only {r['count']} left", title="Nespresso - running low")
            if err:
                print(f"notify failed: {err}", file=sys.stderr)
    conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)",
                 (f"notified_low_{line}", ",".join(str(i) for i in sorted(low)),))
    # A brew the detector could not pin to a capsule waits in the dashboard for a pick. Only
    # announce one the dashboard can actually offer a capsule for — an out-of-stock or unknown
    # brew has nothing to choose, so "which capsule?" would be a dead end.
    pending = conn.execute(
        """SELECT b.id, b.family, b.source,
                  (SELECT COUNT(*) FROM capsules c
                    WHERE c.line = ? AND c.count > 0
                      AND (b.family IS NULL OR lower(c.family) = lower(b.family))) AS picks
           FROM brews b WHERE b.capsule_id IS NULL AND b.delta < 0 ORDER BY b.id""",
        (line,)).fetchall()
    announced = {int(x) for x in get_setting(conn, "notified_pending").split(",") if x.isdigit()}
    still_pending = {p["id"] for p in pending}
    for p in pending:
        if p["id"] in announced or not p["picks"]:
            continue  # already said, or no capsule to pick — a restock will make it one later
        announced.add(p["id"])
        what = f"{p['family']} brew" if p["family"] else "brew"
        err = send(url, f"{what} from {p['source']} needs a capsule - open the dashboard to pick it",
                   title="Nespresso - which capsule?")
        if err:
            print(f"notify failed: {err}", file=sys.stderr)
    conn.execute("INSERT OR REPLACE INTO settings VALUES ('notified_pending', ?)",
                 (",".join(str(i) for i in sorted(announced & still_pending)),))
    due = [m for m in maintenance_state(conn) if m["due"]]
    notified_m = {x for x in get_setting(conn, "notified_maint").split(",") if x}
    for m in due:
        if m["task"] not in notified_m:
            err = send(url, f"{m['label']} is due (every {m['every_days']} days)",
                       title="Nespresso - upkeep due")
            if err:
                print(f"notify failed: {err}", file=sys.stderr)
    conn.execute("INSERT OR REPLACE INTO settings VALUES ('notified_maint', ?)",
                 (",".join(sorted(m["task"] for m in due)),))
    conn.commit()


_notify_lock = threading.Lock()


def notify_async() -> None:
    """Run notify_check off the request path (own connection — sqlite3 connections are
    bound to the thread that opened them). Serialized: concurrent state changes fire
    overlapping checks, and without the lock each would read the same not-yet-notified
    set and send duplicates."""
    def run():
        with _notify_lock:
            try:
                notify_check(connect())
            except Exception as err:  # noqa: BLE001 - one bad settings row must not
                print(f"notify failed: {err}", file=sys.stderr)  # kill every future check
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------- backup

def export_data(conn: sqlite3.Connection) -> dict:
    """Full dump of every table, ready to POST back to /api/import."""
    return {
        "format": "nespresso-stats-backup", "version": 1, "exported": now_iso(),
        "capsules": [dict(r) for r in conn.execute("SELECT * FROM capsules ORDER BY id")],
        "brews": [dict(r) for r in conn.execute("SELECT * FROM brews ORDER BY id")],
        "settings": [dict(r) for r in conn.execute("SELECT * FROM settings ORDER BY key")],
    }


def import_data(conn: sqlite3.Connection, data: dict) -> dict:
    """Replace all data with a /api/export backup. Deletes run only after the payload
    shape is validated; a failure mid-import rolls the transaction back, so a malformed
    upload can't half-wipe the db. Only real schema columns are inserted (values stay
    parameterized), so a hostile file can't reach SQL itself."""
    for table in ("capsules", "brews", "settings"):
        if not isinstance(data.get(table), list):
            raise ValueError(f"not a backup: missing '{table}'")
    try:
        for table in ("capsules", "brews", "settings"):
            valid = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            conn.execute(f"DELETE FROM {table}")
            for row in data[table]:
                cols = {k: v for k, v in row.items() if k in valid}
                if not cols:
                    raise ValueError(f"empty row in '{table}'")
                conn.execute(
                    f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + k for k in cols)})",
                    cols,
                )
        # An imported backup is an existing install — never ask it to run the first-run wizard.
        if not conn.execute("SELECT 1 FROM settings WHERE key='setup_done'").fetchone():
            conn.execute("INSERT INTO settings VALUES ('setup_done', '1')")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"ok": True, "capsules": conn.execute("SELECT COUNT(*) FROM capsules").fetchone()[0],
            "brews": conn.execute("SELECT COUNT(*) FROM brews").fetchone()[0]}


def available_languages() -> list[dict]:
    """Discover UI translations under static/i18n. Each file names itself via `language`,
    so a contributor adds one JSON file and nothing else."""
    out = []
    for path in sorted((ROOT / "static" / "i18n").glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        out.append({
            "code": path.stem,
            "label": data.get("language", path.stem),
            "flag": data.get("flag", ""),
        })
    out.sort(key=lambda x: x["code"] != "en")  # English first, then alphabetical
    return out


# ------------------------------------------------------------- scanner catalogue
# nespresso-scanner (own repo) rescans the store twice a day and publishes the
# machine-readable capsule catalogue. Its diff against capsules.json is the
# "new capsule" hint. CATALOGUE_URL empty disables the whole thing.
SCANNER_URL = os.environ.get(
    "CATALOGUE_URL",
    "https://raw.githubusercontent.com/avpnusr/nespresso-scanner/main/catalogue.json",
)
_scanner_entries: list[dict] = []  # last good remote entries; empty until the first fetch
_scanner_lock = threading.Lock()


def remote_entries() -> list[dict]:
    with _scanner_lock:
        return list(_scanner_entries)


def refresh_scanner() -> list[dict]:
    """Fetch the remote catalogue once. Keeps the last good data on failure and never
    raises — a dead host must not break the dashboard."""
    if not SCANNER_URL:
        return []
    try:
        with urllib.request.urlopen(SCANNER_URL, timeout=10) as resp:
            data = json.loads(resp.read())
        good = [e for e in data.get("capsules", []) if isinstance(e, dict)
                and (e.get("sku") or e.get("name"))]
        with _scanner_lock:
            _scanner_entries[:] = good
    except Exception as err:  # noqa: BLE001 - the remote catalogue is best-effort
        print(f"scanner catalogue: {err}", file=sys.stderr)
    return remote_entries()


def scanner_daemon() -> None:
    """Refresh at startup and every 6 h (the scanner publishes twice a day). Runs off
    the request path so /api/state is never blocked on the network."""
    tick = threading.Event()
    while True:
        refresh_scanner()
        tick.wait(6 * 3600)


def merge_catalogue(local: list[dict], remote: list[dict]) -> tuple[list[dict], list[dict]]:
    """Picker entries (local capsules.json + remote) and the remote diff ("new capsules").
    Diffs by sku first, name second — the store renames capsules, so a name-only diff
    would flag capsules already known as new. Remote entries are normalised (line,
    family) so the picker works with them."""
    local_skus = {(e.get("sku") or "").strip() for e in local}
    # line-scoped: both systems share names (Colombia), so a plain name match would hide one
    # line's capsule behind the other's
    local_names = {(entry_line(e), (e.get("name") or "").strip().lower()) for e in local}
    new: list[dict] = []
    seen: set[str] = set()
    for e in remote:
        if e.get("available") is False:
            continue  # the store delisted it — not a new capsule
        sku = (e.get("sku") or "").strip()
        name = (e.get("name") or "").strip().lower()
        if not name and not sku:
            continue
        line = valid_line(e.get("line"))
        if sku in seen or (line, name) in local_names or (sku and sku in local_skus):
            continue  # already known locally, or a duplicate inside the remote list
        seen.add(sku or "name:" + name)
        new.append({**e, "sku": sku, "line": line, "family": normalize_family(e.get("family"), line)})
    return [*local, *new], new


def local_catalogue() -> list[dict]:
    try:
        return json.loads(CATALOG.read_text())
    except (OSError, ValueError):
        return []


def known_catalogue() -> list[dict]:
    """Catalogue entries (name, family, colour, photo, intensity) — the capsules the app
    knows about: the local file plus the scanner's remote entries. Read per call so
    edits to capsules.json land without a restart.
    ponytail: no cache, the file is a few kB and /api/state is not hot."""
    return merge_catalogue(local_catalogue(), remote_entries())[0]


def current_state(conn) -> dict:
    machine = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings")}
    machine["ntfy_url"] = mask_ntfy(machine.get("ntfy_url", ""))  # topic stays server-side
    # One machine at a time: only the configured line's capsules are the dashboard's business.
    # Capsules of the other line stay in the db (switching back brings them right back).
    line = valid_line(machine.get("machine_line"))
    capsules = [dict(r) for r in conn.execute("SELECT * FROM capsules WHERE line = ?", (line,))]
    # Known-but-not-inventoried capsules of this line, for the "add capsule" picker.
    # Ownership is the sku when both sides have one (the store renames capsules), the name
    # otherwise (a hand-added capsule has no sku). `known` is the catalogue you don't own yet.
    owned_names = {r["name"].strip().lower() for r in conn.execute("SELECT name FROM capsules")}
    owned_skus = {r["sku"] for r in conn.execute("SELECT sku FROM capsules") if r["sku"]}

    def owned(c: dict) -> bool:
        sku = (c.get("sku") or "").strip()
        return (bool(sku) and sku in owned_skus) \
            or c.get("name", "").strip().lower() in owned_names

    merged, scanner_new = merge_catalogue(local_catalogue(), remote_entries())
    known = [c for c in merged if entry_line(c) == line and not owned(c)]
    # New-capsule hint: remote-only entries the owner does not have yet (line-scoped).
    new_capsules = [c for c in scanner_new if entry_line(c) == line and not owned(c)]
    # Manual drag order wins; anything unlisted (new capsule) keeps family/name order at the end.
    order = [int(x) for x in machine.get("capsule_order", "").split(",") if x]
    pos = {cid: i for i, cid in enumerate(order)}
    capsules.sort(key=lambda c: (pos.get(c["id"], len(order)), c["family"], c["name"]))
    brews = [dict(r) for r in conn.execute(
        """SELECT b.id, b.ts, b.source, b.family, b.delta, c.name AS capsule_name
           FROM brews b LEFT JOIN capsules c ON c.id = b.capsule_id
           ORDER BY b.id DESC LIMIT 12"""
    )]
    pending = [dict(r) for r in conn.execute(
        "SELECT id, family, ts, source FROM brews WHERE capsule_id IS NULL AND delta < 0 ORDER BY id"
    )]
    return {
        "capsules": capsules, "brews": brews, "pending": pending,
        "maintenance": maintenance_state(conn), "new_capsules": new_capsules,
        "machine": machine, "families": FAMILIES[line], "known": known,
        "lines": [{"key": k, "label": v} for k, v in LINE_LABELS.items()],
    }


# --------------------------------------------------------------------------- http

def origin_allowed(origin: str | None, host: str | None, forwarded_host: str | None = "") -> bool:
    """A browser always sends Origin on a mutating request, so a cross-site one is refused
    here — otherwise any page the owner visits could POST /api/import and wipe the db.
    Non-browser callers (the deck plugin, curl) send none and are not the threat model.
    X-Forwarded-Host covers a proxy that rewrites Host."""
    if not origin:
        return True
    return urlparse(origin).netloc.lower() in {h.lower() for h in (host, forwarded_host) if h}


class Handler(BaseHTTPRequestHandler):
    server_version = "NespressoStats/1.0"

    def log_message(self, *args):  # keep the console clean
        pass

    def _send(self, code, payload=None, ctype="application/json", cache=None):
        if payload is None:
            body = b""
        elif isinstance(payload, bytes):
            body = payload
        elif isinstance(payload, str):
            body = payload.encode()
        else:
            body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The dashboard must not be frameable: a hostile page could otherwise overlay it and
        # borrow the owner's clicks — the Origin guard does not help there, a click inside the
        # iframe is same-origin. No script-src/style-src: the frontend uses inline handlers, so
        # a strict policy would need a rewrite; these four cost nothing and cover the rest.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "frame-ancestors 'none'; base-uri 'none'; object-src 'none'; "
                         "form-action 'self'")
        if cache:
            self.send_header("Cache-Control", cache)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, {"error": "invalid Content-Length"})
            raise ConnectionError("invalid Content-Length")
        if length > 10 * 1024 * 1024:  # photos come in at ~1024px; anything bigger is abuse
            self._send(413, {"error": "body too large"})
            raise ConnectionError("body too large")
        data = json.loads(self.rfile.read(length) or b"{}")
        return data if isinstance(data, dict) else {}  # a non-dict body is just a bad payload

    def _conn(self) -> sqlite3.Connection:
        return connect()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            return
        if url.path.startswith("/static/"):
            self._serve_static(url.path)
            return
        if url.path == "/api/state":
            self._send(200, current_state(self._conn()))
            return
        if url.path == "/api/stats":
            self._send(200, brew_stats(self._conn()))
            return
        if url.path == "/api/languages":
            self._send(200, available_languages())
            return
        if url.path == "/api/export":
            self._send(200, export_data(self._conn()))
            return
        self._send(404, {"error": "not found"})

    def _serve_static(self, path: str):
        static = (ROOT / "static").resolve()
        target = (static / path[len("/static/"):]).resolve()
        try:
            target.relative_to(static)
        except ValueError:
            self._send(403, {"error": "forbidden"})
            return
        if not target.is_file():
            self._send(404, {"error": "not found"})
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        # Photos and translation files change only with a deploy — let the browser hold them.
        self._send(200, target.read_bytes(), ctype, cache="public, max-age=3600")

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        forwarded = (self.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
        if origin_allowed(origin, self.headers.get("Host"), forwarded):
            return True
        # Kept out of the access log (log_message is silenced), so name it here: a proxy
        # that rewrites Host without X-Forwarded-Host would otherwise 403 silently.
        print(f"cross-origin rejected: origin={origin!r} host={self.headers.get('Host')!r}",
              file=sys.stderr)
        self._send(403, {"error": "cross-origin request rejected"})
        return False

    def do_POST(self):
        if not self._same_origin():
            return
        url = urlparse(self.path)
        try:
            payload = self._body()
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid json"})
            return
        except ConnectionError:
            return
        conn = self._conn()
        try:
            if url.path == "/api/capsules":
                self._upsert_capsule(conn, payload)
            elif url.path == "/api/brew":
                self._brew(conn, payload)
            elif url.path == "/api/brew-detected":
                result = brew_detected(conn, payload.get("family"), payload.get("source", "auto"),
                                       payload.get("capsule"))
                self._send(200, result)
            elif url.path == "/api/resolve":
                self._send(200, resolve_pending(
                    conn, int(payload["capsule_id"]), payload.get("brew_id")
                ))
            elif url.path == "/api/identify":
                self._send(200, identify(conn, payload.get("color", ""), payload.get("image")))
            elif url.path == "/api/dismiss":
                self._send(200, dismiss_pending(conn, int(payload["brew_id"])))
            elif url.path == "/api/brews":
                self._send(200, set_brew_capsule(
                    conn, int(payload["brew_id"]), int(payload["capsule_id"])
                ))
            elif url.path == "/api/order":
                set_capsule_order(conn, payload["ids"])
                self._send(200, {"ok": True})
            elif url.path == "/api/setup":
                self._send(200, save_setup(conn, payload.get("counts")))
            elif url.path == "/api/maintenance":
                self._send(200, log_maintenance(conn, payload.get("task")))
            elif url.path == "/api/import":
                self._send(200, import_data(conn, payload))
            elif url.path == "/api/notify-test":
                stored = get_setting(conn, "ntfy_url")
                target = payload.get("url") or stored
                if not target:
                    raise ValueError("no ntfy topic URL configured")
                if target != stored:
                    ntfy_host_ok(target)  # a client-chosen target is restricted
                err = ntfy_send(target, "Test notification — your Nespresso dashboard is wired up.",
                                title="Nespresso - test")
                self._send(200, {"ok": err is None, "error": err})
            elif url.path == "/api/settings":
                if payload.get("ntfy_url"):  # empty clears the topic; anything else is checked
                    payload["ntfy_url"] = ntfy_url_ok(str(payload["ntfy_url"]))
                for key in ("machine_name", "machine_model", "machine_image", "sleeve_size",
                            "clean_days", "descale_days", "ntfy_url"):
                    if key in payload:
                        conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, payload[key]))
                if "machine_line" in payload:  # an unknown line would empty the dashboard
                    conn.execute("INSERT OR REPLACE INTO settings VALUES ('machine_line', ?)",
                                 (valid_line(payload["machine_line"]),))
                conn.commit()
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})
            if url.path not in ("/api/identify", "/api/notify-test"):
                notify_async()  # a state change may have crossed a reminder threshold
        except (ValueError, KeyError, TypeError, sqlite3.Error) as err:  # TypeError: wrong payload types
            self._send(400, {"error": str(err)})

    def _upsert_capsule(self, conn, payload):
        fields = {k: payload.get(k, "") for k in ("name", "family", "color", "image", "notes")}
        fields["sku"] = (payload.get("sku") or "").strip()
        if not fields["name"] or not fields["family"]:
            raise ValueError("name and family are required")
        count = max(0, int(payload.get("count", 0) or 0))
        price = max(0.0, float(payload.get("price") or 0))
        # `or 2` would swallow an explicit 0 ("never warn"), so test for None instead
        threshold = payload.get("threshold")
        threshold = max(0, int(threshold)) if threshold is not None else 2
        intensity = max(0, min(13, int(payload.get("intensity") or 0)))
        special = 1 if payload.get("special") else 0
        if payload.get("id"):
            conn.execute(
                """UPDATE capsules SET name=:name, sku=:sku, family=:family, color=:color,
                   image=:image, notes=:notes, count=:count, price=:price,
                   threshold=:threshold, intensity=:intensity, special=:special WHERE id=:id""",
                {**fields, "count": count, "price": price, "threshold": threshold,
                 "intensity": intensity, "special": special, "id": payload["id"]},
            )
        else:
            # A new capsule belongs to the machine in use, so the dashboard keeps showing it.
            conn.execute(
                """INSERT INTO capsules (name, sku, family, color, image, notes, count, price, threshold, intensity, special, line)
                   VALUES (:name, :sku, :family, :color, :image, :notes, :count, :price, :threshold, :intensity, :special, :line)""",
                {**fields, "count": count, "price": price, "threshold": threshold,
                 "intensity": intensity, "special": special, "line": machine_line(conn)},
            )
        conn.commit()
        self._send(200, {"ok": True})

    def _brew(self, conn, payload):
        capsule_id = int(payload["capsule_id"])
        delta = int(payload.get("delta", -1))
        row = conn.execute("SELECT name, family FROM capsules WHERE id=?", (capsule_id,)).fetchone()
        if row is None:
            raise ValueError("capsule not found")
        dec(conn, capsule_id, delta)
        if payload.get("log"):
            log_brew(conn, capsule_id, row["family"], payload.get("source", "manual"), delta=delta)
        conn.commit()
        self._send(200, {"ok": True, "capsule": row["name"]})

    def do_DELETE(self):
        if not self._same_origin():
            return
        url = urlparse(self.path)
        id_ = parse_qs(url.query).get("id", [None])[0]
        if not id_ or not id_.isdigit():
            self._send(400, {"error": "id required"})
            return
        conn = self._conn()
        if url.path == "/api/capsules":
            try:
                self._send(200, delete_capsule(conn, int(id_)))
            except ValueError as err:
                self._send(409, {"error": str(err)})
                return
        elif url.path == "/api/brews":
            result = undo_brew(conn, int(id_))
            notify_async()
            self._send(200, result)
            return
        else:
            self._send(404, {"error": "not found"})
            return
        notify_async()  # undo/delete can end or start a low-stock episode
        self._send(200, {"ok": True})


def selftest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn = connect(Path(tmp) / "t.db")
        init_db(conn, CATALOG)
        assert conn.execute("SELECT COUNT(*) FROM capsules").fetchone()[0] > 0
        # a fresh seed is not "set up": the first-run stock-take dialog must open
        assert not conn.execute("SELECT 1 FROM settings WHERE key='setup_done'").fetchone()
        # a catalogue entry without a sku falls back to name matching — that is the delisted-
        # capsule blind spot, so every seed entry must carry one. Duplicates would make one
        # capsule look owned twice (sku is the identity), so catch those here too.
        seed = local_catalogue()
        assert all(c.get("sku") for c in seed)
        skus = [c["sku"] for c in seed]
        assert len(skus) == len(set(skus)), "duplicate sku in capsules.json"
        # every catalogue photo the dashboard points at actually ships with the repo
        for entry in known_catalogue():
            image = entry.get("image", "")
            if image.startswith("/static/"):
                assert (ROOT / image.lstrip("/")).exists(), f"missing catalogue image {image}"

        # known-capsule picker: a fully seeded inventory has nothing left to offer
        assert current_state(conn)["known"] == []
        other = conn.execute(
            "SELECT id, name, color FROM capsules WHERE line='vertuo' AND name NOT IN"
            " ('Fortado','Melozio','Intenso') LIMIT 1"
        ).fetchone()
        conn.execute("DELETE FROM capsules WHERE id=?", (other["id"],))
        known = current_state(conn)["known"]
        assert [k["name"] for k in known] == [other["name"]], known
        assert known[0]["color"] == other["color"] and known[0]["image"], known

        # sku identity: a store rename must not make a capsule the owner already has look new
        sku_entry = next(c for c in known_catalogue()
                         if c.get("sku") and entry_line(c) == "vertuo" and c["name"] != other["name"])
        conn.execute(
            "INSERT INTO capsules (name, sku, family, color, image, intensity, special, line)"
            " VALUES (?, ?, 'Mug', '#000000', '', 0, 0, 'vertuo')",
            ("Renamed " + sku_entry["name"], sku_entry["sku"]),
        )
        assert all(k.get("sku") != sku_entry["sku"] for k in current_state(conn)["known"])

        # the one-time backfill renames a pre-sku row and stamps its sku
        old_name = next(iter(CATALOGUE_RENAMES))
        new_name = CATALOGUE_RENAMES[old_name]
        conn.execute("DELETE FROM capsules WHERE name = ?", (new_name,))
        conn.execute(
            "INSERT INTO capsules (name, family, color, image, intensity, special, line, sku)"
            " VALUES (?, 'Espresso', '#000000', '', 0, 0, 'original', '')",
            (old_name,),
        )
        backfill_skus(conn)
        assert conn.execute("SELECT sku FROM capsules WHERE name = ?", (new_name,)).fetchone()["sku"], \
            "rename backfill did not stamp a sku"
        assert not conn.execute("SELECT 1 FROM capsules WHERE name = ?", (old_name,)).fetchone()

        # the model misspells or half-reads what it prints, and a read name beats the shortlist
        if conn.execute("SELECT 1 FROM capsules WHERE name='Voltesso'").fetchone():
            assert match_capsule(conn, "VOLTESO")["name"] == "Voltesso"
            assert match_capsule(conn, "voltesso")["name"] == "Voltesso"
        if conn.execute("SELECT 1 FROM capsules WHERE name='Double Espresso Chiaro'").fetchone():
            assert match_capsule(conn, "CHIARO")["name"] == "Double Espresso Chiaro"
            assert match_capsule(conn, "Double Espresso") is None  # fits three capsules
        assert match_capsule(conn, "Definitely Not A Capsule") is None
        assert match_capsule(conn, "") is None
        # a transcription only identifies a capsule when the whole name is in it
        assert capsule_names_in(conn, "DOUBLE ESPRESSO 80 ML RECYCLE ME") == []
        if conn.execute("SELECT 1 FROM capsules WHERE name='Double Espresso Chiaro'").fetchone():
            assert capsule_names_in(conn, "DOUBLE ESPRESSO CHIARO RECYCLE ME 80 ML")[0]["name"] \
                == "Double Espresso Chiaro"
            # the pod prints its name twice, so a transcription can come back reordered
            assert capsule_names_in(conn, "CHIARO DOUBLE ESPRESSO RECYCLE ME")[0]["name"] \
                == "Double Espresso Chiaro"
        if conn.execute("SELECT 1 FROM capsules WHERE name='Voltesso'").fetchone():
            # the model drops a letter but the pod is still identifiable
            assert capsule_names_in(conn, "VOLTESO ESPRESSO RECYCLE ME")[0]["name"] == "Voltesso"
        if conn.execute("SELECT 1 FROM capsules WHERE name='Melozio Go'").fetchone():
            assert capsule_names_in(conn, "MELOZIO GO RECYCLE ME")[0]["name"] == "Melozio Go"

        # single capsule of a family → auto decrement (Fortado is the only Gran Lungo)
        conn.execute("UPDATE capsules SET count = 3 WHERE name = 'Fortado'")
        conn.execute("UPDATE capsules SET count = 0 WHERE family != 'Gran Lungo'")
        res = brew_detected(conn, "3")  # cloud family id for Gran Lungo
        assert res["action"] == "decremented" and res["capsule"] == "Fortado", res
        assert conn.execute("SELECT count FROM capsules WHERE name='Fortado'").fetchone()[0] == 2

        # an exact capsule name (Stream Deck key) decrements without a family guess
        conn.execute("UPDATE capsules SET count = 2 WHERE name = 'Intenso'")
        res = brew_detected(conn, None, capsule="Intenso")
        assert res["action"] == "decremented" and res["capsule"] == "Intenso", res
        assert conn.execute("SELECT count FROM capsules WHERE name='Intenso'").fetchone()[0] == 1
        # an unmatchable name falls back to the family rule (Fortado is the only Gran Lungo)
        assert brew_detected(conn, "Gran Lungo", capsule="Nope")["action"] == "decremented"

        # multiple candidates → pending, then resolve
        conn.execute("UPDATE capsules SET count = 2 WHERE name IN ('Melozio','Intenso')")
        res = brew_detected(conn, "Mug")
        assert res["action"] == "pending" and "Melozio" in res["candidates"], res
        melozio = conn.execute("SELECT id FROM capsules WHERE name='Melozio'").fetchone()[0]
        resolve_pending(conn, melozio)
        assert conn.execute("SELECT count FROM capsules WHERE name='Melozio'").fetchone()[0] == 1

        # count never goes below zero, and a multi-capsule family stays pending
        conn.execute("UPDATE capsules SET count = 0")
        conn.execute("UPDATE capsules SET count = 1 WHERE name IN ('Melozio','Intenso')")
        for _ in range(3):
            res = brew_detected(conn, "Mug")
            assert res["action"] == "pending", res
        assert conn.execute("SELECT count FROM capsules WHERE name='Melozio'").fetchone()[0] == 1

        # out of stock family is not treated as a candidate
        conn.execute("UPDATE capsules SET count = 0")
        assert brew_detected(conn, "Gran Lungo")["action"] == "out-of-stock"

        # colour ranking: an exact colour match ranks first
        conn.execute("UPDATE capsules SET color='#26415e' WHERE name='Melozio'")
        conn.execute("UPDATE capsules SET color='#ff0000' WHERE name='Intenso'")
        ranked = rank_by_color(conn, "#26415e")
        assert ranked[0]["name"] == "Melozio", ranked[:3]
        assert [r["name"] for r in rank_by_color(conn, "#ff0000")][0] == "Intenso"
        assert identify(conn, "#26415e")["candidates"][0]["name"] == "Melozio"
        assert hex_to_rgb("#abc") == (170, 187, 204)
        assert color_distance("#000000", "#ffffff") > color_distance("#000000", "#111111")

        # restock logs an event; undo reverses the count and removes it
        conn.execute("UPDATE capsules SET count = 0")
        melozio = conn.execute("SELECT id FROM capsules WHERE name='Melozio'").fetchone()[0]
        log_brew(conn, melozio, "Mug", "manual", delta=10)
        dec(conn, melozio, 10)
        conn.commit()
        assert conn.execute("SELECT count FROM capsules WHERE id=?", (melozio,)).fetchone()[0] == 10
        rid = conn.execute("SELECT id FROM brews ORDER BY id DESC LIMIT 1").fetchone()[0]
        undo_brew(conn, rid)
        assert conn.execute("SELECT count FROM capsules WHERE id=?", (melozio,)).fetchone()[0] == 0

        # reassigning a brew moves the decrement to the corrected capsule
        intenso = conn.execute("SELECT id FROM capsules WHERE name='Intenso'").fetchone()[0]
        conn.execute("UPDATE capsules SET count = 5")
        bid = log_brew(conn, melozio, "Mug", "manual")
        dec(conn, melozio)
        conn.commit()
        assert conn.execute("SELECT count FROM capsules WHERE id=?", (melozio,)).fetchone()[0] == 4
        set_brew_capsule(conn, bid, intenso)
        assert conn.execute("SELECT count FROM capsules WHERE id=?", (melozio,)).fetchone()[0] == 5
        assert conn.execute("SELECT count FROM capsules WHERE id=?", (intenso,)).fetchone()[0] == 4
        assert conn.execute("SELECT capsule_id FROM brews WHERE id=?", (bid,)).fetchone()[0] == intenso
        assert len(brew_stats(conn)["brews"]) >= 1

        # stats are line-scoped: the other line's history must not feed this machine's numbers
        other_line = conn.execute(
            "SELECT id FROM capsules WHERE line='original' LIMIT 1").fetchone()[0]
        log_brew(conn, other_line, "Espresso", "manual")
        seen = {b["capsule_id"] for b in brew_stats(conn)["brews"]}
        assert intenso in seen and other_line not in seen, seen
        conn.execute("INSERT OR REPLACE INTO settings VALUES ('machine_line', 'original')")
        assert other_line in {b["capsule_id"] for b in brew_stats(conn)["brews"]}
        conn.execute("INSERT OR REPLACE INTO settings VALUES ('machine_line', 'vertuo')")

        # an unknown brew (no capsule) counts for the line in use; a brew orphaned by a
        # pre-2026 capsule deletion has no line and must not be counted for either
        probe = conn.execute(
            "INSERT INTO brews (capsule_id, family, ts, source, delta)"
            " VALUES (99999, 'Mug', ?, 'manual', -1)", (now_iso(),)).lastrowid
        unknown = conn.execute(
            "INSERT INTO brews (capsule_id, family, ts, source, delta)"
            " VALUES (NULL, NULL, ?, 'auto', -1)", (now_iso(),)).lastrowid
        seen = {b["capsule_id"] for b in brew_stats(conn)["brews"]}
        assert None in seen and 99999 not in seen, seen
        conn.execute("DELETE FROM brews WHERE id IN (?, ?)", (probe, unknown))

        # deleting a capsule with brew history would orphan its rows, so it is refused;
        # a capsule that was never used still deletes
        try:
            delete_capsule(conn, intenso)
            raise AssertionError("expected ValueError for a capsule with brew history")
        except ValueError:
            pass
        assert conn.execute("SELECT 1 FROM capsules WHERE id=?", (intenso,)).fetchone()
        conn.execute("INSERT INTO capsules (name, sku, family, color, line)"
                     " VALUES ('Never used', '', 'Mug', '#000000', 'vertuo')")
        unused = conn.execute("SELECT id FROM capsules WHERE name='Never used'").fetchone()[0]
        assert delete_capsule(conn, unused)["ok"]
        assert not conn.execute("SELECT 1 FROM capsules WHERE id=?", (unused,)).fetchone()

        # a mutating request from another site is refused; native clients send no Origin
        assert origin_allowed(None, "ncs.khmls.net")
        assert origin_allowed("https://ncs.khmls.net", "ncs.khmls.net")
        assert origin_allowed("https://ncs.khmls.net", "127.0.0.1:8787", "ncs.khmls.net")
        assert not origin_allowed("https://evil.example", "ncs.khmls.net")
        assert not origin_allowed("null", "ncs.khmls.net")  # sandboxed / file:// page

        # resolving only works on genuinely pending brews — an already-assigned id is refused
        try:
            resolve_pending(conn, intenso, bid)
            raise AssertionError("expected ValueError for non-pending brew")
        except ValueError:
            pass

        # translation files are discovered for the language picker
        langs = available_languages()
        assert [l["code"] for l in langs][:2] == ["en", "de"], langs
        assert {l["code"]: l["label"] for l in langs}["de"] == "Deutsch"
        assert {l["code"]: l["flag"] for l in langs}["de"] == "🇩🇪"

        # upkeep: logged as an event, kept out of pending brews, and drives the reminder
        assert all(m["due"] for m in current_state(conn)["maintenance"])  # nothing recorded yet
        log_maintenance(conn, "clean")
        ms = {m["task"]: m for m in current_state(conn)["maintenance"]}
        assert not ms["clean"]["due"] and ms["descale"]["due"], ms
        assert all(p["family"] for p in current_state(conn)["pending"]), "maintenance leaked into pending"
        conn.execute("UPDATE brews SET ts = ? WHERE source = 'clean'", ("2000-01-01T00:00:00+00:00",))
        conn.commit()
        assert {m["task"]: m for m in current_state(conn)["maintenance"]}["clean"]["due"]
        # the interval is configurable, defaulting when unset or invalid
        conn.execute("INSERT OR REPLACE INTO settings VALUES ('clean_days', '30')")
        assert {m["task"]: m for m in current_state(conn)["maintenance"]}["clean"]["every_days"] == 30
        conn.execute("INSERT OR REPLACE INTO settings VALUES ('clean_days', 'lots')")
        assert {m["task"]: m for m in current_state(conn)["maintenance"]}["clean"]["every_days"] == 10
        try:
            log_maintenance(conn, "nonsense")
            raise AssertionError("expected ValueError for unknown task")
        except ValueError:
            pass

        # intensity seeds from the catalogue
        assert conn.execute("SELECT intensity FROM capsules WHERE name='Melozio'").fetchone()[0] == 6

        # manual drag order persists and wins over family/name ordering
        ids = [r[0] for r in conn.execute("SELECT id FROM capsules WHERE line='vertuo'")]
        set_capsule_order(conn, list(reversed(ids)))
        assert [c["id"] for c in current_state(conn)["capsules"]] == list(reversed(ids))

        # migration is idempotent on an already-migrated db
        migrate(conn)

        # first-run stock-take: entered counts land, seed rows left at 0 are dropped (a row
        # with brew history is kept), and the install is marked set up
        setup_conn = connect(Path(tmp) / "setup.db")
        init_db(setup_conn, CATALOG)
        assert not setup_conn.execute("SELECT 1 FROM settings WHERE key='setup_done'").fetchone()
        ids = [r[0] for r in setup_conn.execute("SELECT id FROM capsules WHERE line='vertuo'")]
        keep, brewed = ids[0], ids[1]
        log_brew(setup_conn, brewed, "Mug", "manual")
        setup_conn.commit()
        res = save_setup(setup_conn, {str(keep): 7, **{str(i): 0 for i in ids[1:]}})
        assert res["updated"] == len(ids) and res["removed"] == len(ids) - 2, res
        assert setup_conn.execute("SELECT count FROM capsules WHERE id=?", (keep,)).fetchone()[0] == 7
        assert setup_conn.execute("SELECT 1 FROM capsules WHERE id=?", (brewed,)).fetchone()
        assert setup_conn.execute("SELECT value FROM settings WHERE key='setup_done'").fetchone()[0] == "1"
        try:
            save_setup(setup_conn, {})  # an empty payload must not mark an untouched seed done
            raise AssertionError("expected ValueError for empty counts")
        except ValueError:
            pass
        # an upgrade with stock already entered is set up on start instead of being asked
        upgrade = connect(Path(tmp) / "upgrade.db")
        init_db(upgrade, CATALOG)
        upgrade.execute("DELETE FROM settings WHERE key='setup_done'")
        upgrade.execute("UPDATE capsules SET count = 3 WHERE id = (SELECT MIN(id) FROM capsules)")
        upgrade.commit()
        init_db(upgrade, CATALOG)
        assert upgrade.execute("SELECT value FROM settings WHERE key='setup_done'").fetchone()[0] == "1"

        # a sku the catalogue added after the install launched lands on a blank row at startup
        conn.execute("UPDATE capsules SET sku = '' WHERE name = 'Peru Organic'")
        conn.commit()
        migrate(conn)
        assert conn.execute("SELECT sku FROM capsules WHERE name = 'Peru Organic'").fetchone()['sku'] == '7012.20'
        # …and a name both lines share gets each line's own sku, never the other's
        names = "('Colombia', 'Kahawa Ya Congo')"
        conn.execute(f"UPDATE capsules SET sku = '' WHERE name IN {names}")
        conn.commit()
        fill_empty_skus(conn)
        got = {(r['name'], r['line']): r['sku'] for r in conn.execute(
            f"SELECT name, line, sku FROM capsules WHERE name IN {names}")}
        assert len(got) >= 4 and all(got.values()), got  # both lines present, all stamped
        assert got[('Colombia', 'vertuo')] != got[('Colombia', 'original')], got
        assert got[('Kahawa Ya Congo', 'vertuo')] != got[('Kahawa Ya Congo', 'original')], got

        # backup: export → wipe → import restores everything; a bad upload rolls back
        backup = export_data(conn)
        assert backup["capsules"] and backup["brews"], backup.keys()
        conn.executescript("DELETE FROM capsules; DELETE FROM brews; DELETE FROM settings")
        assert conn.execute("SELECT COUNT(*) FROM capsules").fetchone()[0] == 0
        assert import_data(conn, backup)["capsules"] == len(backup["capsules"])
        assert conn.execute("SELECT COUNT(*) FROM brews").fetchone()[0] == len(backup["brews"])
        assert conn.execute("SELECT value FROM settings WHERE key='sleeve_size'").fetchone()[0] == "10"
        # restoring a pre-feature backup must not trigger the first-run wizard either
        assert conn.execute("SELECT value FROM settings WHERE key='setup_done'").fetchone()[0] == "1"
        try:
            import_data(conn, {"capsules": []})  # not a backup (no brews/settings)
            raise AssertionError("expected ValueError for non-backup")
        except ValueError:
            pass
        try:
            import_data(conn, {**backup, "brews": [{"no_such_column": 1}]})
            raise AssertionError("expected import failure")
        except (ValueError, sqlite3.Error):
            pass
        assert conn.execute("SELECT COUNT(*) FROM capsules").fetchone()[0] == len(backup["capsules"]), \
            "failed import must not wipe the db"

        # garbage Content-Length is a 400, not a server-side crash
        h2 = Handler.__new__(Handler)
        codes = []
        h2._send = lambda code, *a, **k: codes.append(code)
        h2.headers = {"Content-Length": "abc"}
        try:
            h2._body()
            raise AssertionError("expected ConnectionError")
        except ConnectionError:
            pass
        assert codes == [400], codes

        # a client-chosen test target must stay on an ntfy host
        assert ntfy_host_ok("https://ntfy.sh/topic") is None
        assert ntfy_host_ok("https://sub.ntfy.sh/topic") is None
        assert ntfy_host_ok("http://localhost:8080/topic") is None
        for bad in ("http://192.168.0.1:443/", "https://evil.example/ntfy.sh", "http://localthost"):
            try:
                ntfy_host_ok(bad)
                raise AssertionError(f"expected ValueError for {bad}")
            except ValueError:
                pass

        # ntfy reminders: nothing without a topic, one push per low-stock / upkeep
        # episode, deduped across checks, re-armed by restocking
        conn.execute("UPDATE capsules SET count = 1 WHERE name = 'Intenso'")
        conn.commit()
        sent = []
        record = lambda url, msg, title="": sent.append(msg)
        notify_check(conn, send=record)
        assert not sent, sent  # no topic configured yet
        conn.execute("INSERT OR REPLACE INTO settings VALUES ('ntfy_url', 'https://ntfy.sh/t')")
        conn.commit()
        notify_check(conn, send=record)
        assert any("Intenso" in m and "1 left" in m for m in sent), sent
        assert sum("is due" in m for m in sent) == 2, sent  # cleaning + descaling
        n = len(sent)
        notify_check(conn, send=record)
        assert len(sent) == n, sent  # no duplicates on re-check
        conn.execute("UPDATE capsules SET count = 9 WHERE name = 'Intenso'")
        conn.commit()
        notify_check(conn, send=record)  # restock ends the episode
        conn.execute("UPDATE capsules SET count = 1 WHERE name = 'Intenso'")
        conn.commit()
        notify_check(conn, send=record)
        assert any("Intenso" in m for m in sent[n:]), sent  # …and re-arms it
        assert {r["key"] for r in conn.execute("SELECT key FROM settings")} >= {
            "notified_low_vertuo"}, "low-stock episodes must be tracked per line"
        # a line switch keeps the vertuo episode: coming back must not re-announce Intenso
        conn.execute("INSERT OR REPLACE INTO settings VALUES ('machine_line', 'original')")
        notify_check(conn, send=record)
        conn.execute("INSERT OR REPLACE INTO settings VALUES ('machine_line', 'vertuo')")
        n = len(sent)
        notify_check(conn, send=record)
        assert len(sent) == n, sent

        # a brew the detector could not pin to a capsule is pushed once, so the count cannot
        # silently drift; resolving or ignoring it clears the row and with it the episode
        conn.execute("INSERT INTO brews (capsule_id, family, ts, source, delta)"
                     " VALUES (NULL, 'Mug', ?, 'streamdeck', -1)", (now_iso(),))
        notify_check(conn, send=record)
        assert len(sent) == n + 1 and "needs a capsule" in sent[-1], sent[n:]
        notify_check(conn, send=record)
        assert len(sent) == n + 1, sent  # no repeat for the same pending brew
        conn.execute("DELETE FROM brews WHERE capsule_id IS NULL AND delta < 0"
                     " AND source = 'streamdeck'")
        # nothing to choose from = no push: the queue can hold a family this line cannot brew
        # (a pending left over from the other machine), and "which capsule?" would be a dead end
        conn.execute("INSERT INTO brews (capsule_id, family, ts, source, delta)"
                     " VALUES (NULL, 'Ristretto', ?, 'streamdeck', -1)", (now_iso(),))
        n = len(sent)
        notify_check(conn, send=record)
        assert not any("needs a capsule" in m for m in sent[n:]), sent[n:]
        assert "notified_pending" in {r["key"] for r in conn.execute("SELECT key FROM settings")}
        conn.execute("DELETE FROM brews WHERE capsule_id IS NULL AND delta < 0"
                     " AND source = 'streamdeck'")

        # the ntfy topic is a bearer secret: /api/state hands out a mask, a backup keeps the
        # real URL, and a stored URL must not be able to make the server read local files
        conn.execute("INSERT OR REPLACE INTO settings VALUES"
                     " ('ntfy_url', 'https://ntfy.sh/s3cret')")
        conn.commit()
        assert current_state(conn)["machine"]["ntfy_url"] == "https://ntfy.sh/•••"
        exported = {r["key"]: r["value"] for r in export_data(conn)["settings"]}
        assert exported["ntfy_url"] == "https://ntfy.sh/s3cret", exported
        assert mask_ntfy("") == "" and mask_ntfy("https://ntfy.sh") == "https://ntfy.sh"
        assert ntfy_url_ok("https://ntfy.sh/topic") == "https://ntfy.sh/topic"
        assert ntfy_url_ok("http://192.168.70.5:8080/topic")  # self-hosted ntfy stays usable
        for bad in ("file:///etc/passwd", "ftp://ntfy.sh/x", "https://user:pw@ntfy.sh/x", "",
                    "https://ntfy.sh/•••",  # the mask /api/state hands out — never save it back
                    "https://ntfy.sh/my topic"):
            try:
                ntfy_url_ok(bad)
                raise AssertionError(f"expected ValueError for {bad!r}")
            except ValueError:
                pass
        assert ntfy_send("file:///etc/passwd", "x"), "a file: topic must be refused, not read"
        assert ntfy_send("http://127.0.0.1:9/topic", "x"), "a refused connection reports an error"

        # every response carries the anti-framing headers: a click inside an iframe is
        # same-origin, so the Origin guard does not stop a hostile page from overlaying the UI
        h3 = Handler.__new__(Handler)
        sent_headers = []
        h3.send_response = lambda code, *a, **k: None
        h3.send_header = lambda k, v: sent_headers.append((k, v))
        h3.end_headers = lambda: None
        h3.wfile = type("W", (), {"write": lambda self, body: None})()
        h3._send(200, {"ok": True})
        sent_headers = dict(sent_headers)
        assert sent_headers["X-Frame-Options"] == "DENY", sent_headers
        assert sent_headers["X-Content-Type-Options"] == "nosniff", sent_headers
        assert sent_headers["Referrer-Policy"] == "no-referrer", sent_headers
        assert "frame-ancestors 'none'" in sent_headers["Content-Security-Policy"], sent_headers

        # a pre-intensity db gains the column and backfills values from the catalogue
        old = connect(Path(tmp) / "old.db")
        old.executescript("""
            CREATE TABLE capsules (id INTEGER PRIMARY KEY, name TEXT, family TEXT, color TEXT,
                                   image TEXT, count INTEGER, notes TEXT, price REAL, threshold INTEGER);
            CREATE TABLE brews (id INTEGER PRIMARY KEY, capsule_id INTEGER, family TEXT,
                                ts TEXT, source TEXT, delta INTEGER);
        """)
        old.execute("INSERT INTO capsules (name, family, color, image, count, notes, price, threshold)"
                    " VALUES ('Melozio','Mug','#a16730','',5,'',0,2)")
        old.commit()
        migrate(old)
        assert old.execute("SELECT intensity FROM capsules WHERE name='Melozio'").fetchone()[0] == 6
        assert old.execute("SELECT special FROM capsules WHERE name='Melozio'").fetchone()[0] == 0
        # an original-line db gains the column with the line it was
        assert old.execute("SELECT line FROM capsules WHERE name='Melozio'").fetchone()[0] == "vertuo"

        # Original Line: the machine reads no barcode, so a brew arrives without a cup size and
        # is queued for the owner. Families, capsules, picker and identify follow the set line.
        conn.execute("INSERT OR REPLACE INTO settings VALUES ('machine_line', 'original')")
        st = current_state(conn)
        assert st["families"] == ["Ristretto", "Espresso", "Lungo"], st["families"]
        assert [l["key"] for l in st["lines"]] == ["vertuo", "original"], st["lines"]
        assert st["capsules"] and all(c["line"] == "original" for c in st["capsules"])
        # the picker offers this line's missing capsules only — an un-owned Vertuo capsule from the
        # catalogue must not appear next to them
        victim = conn.execute(
            "SELECT name FROM capsules WHERE line='original' ORDER BY name LIMIT 1").fetchone()
        other_line = conn.execute(
            "SELECT name FROM capsules WHERE line='vertuo' ORDER BY name LIMIT 1").fetchone()
        conn.execute("DELETE FROM capsules WHERE name = ?", (victim["name"],))
        conn.execute("DELETE FROM capsules WHERE name = ?", (other_line["name"],))
        assert [k["name"] for k in current_state(conn)["known"]] == [victim["name"]], "picker offered another line"
        # 'Lungo' is this line's long cup — not Vertuo's Gran Lungo, and neither is a Vertuo pod here
        assert normalize_family("Lungo", "original") == "Lungo"
        assert normalize_family("lungo", "vertuo") == "Gran Lungo"
        assert match_capsule(conn, "Melozio", "original") is None
        assert match_capsule(conn, "Melozio", "vertuo")["name"] == "Melozio"
        # "ESPRESSO" is what every pod prints as its cup size, never a capsule name of its own
        assert match_capsule(conn, "Espresso", "original") is None
        assert capsule_names_in(conn, "ESPRESSO 40 ML", line="original") == []
        assert capsule_names_in(conn, "FESTIVE ESPRESSO 40 ML", line="original")[0]["name"] == "Festive Espresso"

        # nothing in stock → unknown, and no pointless banner
        conn.execute("UPDATE capsules SET count = 0 WHERE line = 'original'")
        assert brew_detected(conn, None)["action"] == "unknown"
        first = conn.execute(
            "SELECT id, name FROM capsules WHERE line='original' ORDER BY name LIMIT 1").fetchone()
        conn.execute("UPDATE capsules SET count = 5, color = '#123456' WHERE id = ?", (first["id"],))
        res = brew_detected(conn, None)
        assert res["action"] == "pending" and res["family"] is None, res
        assert res["candidates"] == [first["name"]], res
        assert current_state(conn)["pending"][-1]["family"] is None
        resolve_pending(conn, first["id"])
        assert conn.execute("SELECT count FROM capsules WHERE id=?", (first["id"],)).fetchone()[0] == 4

        # a family this line does not have (a Vertuo cup size) is queued, not out-of-stock
        res = brew_detected(conn, "Gran Lungo")
        assert res["action"] == "pending" and res["family"] is None, res
        dismiss_pending(conn, current_state(conn)["pending"][-1]["id"])
        # identify only ever proposes capsules of the machine's line
        ranked = identify(conn, "#123456")
        assert ranked["line"] == "original" and ranked["candidates"][0]["id"] == first["id"], ranked

        # a new capsule is stamped with the line in use, so it stays visible
        h = Handler.__new__(Handler)  # only the db half of the request handler is under test
        h._send = lambda *a, **k: None
        h._upsert_capsule(conn, {"name": "Test Kapsel", "family": "Lungo", "count": 1})
        assert conn.execute("SELECT line FROM capsules WHERE name='Test Kapsel'").fetchone()[0] \
            == "original"

        # scanner catalogue: the diff is sku-first (store renames stay known), the hint is
        # line-scoped, and the picker carries the remote entries
        local = [{"name": "Arpeggio", "sku": "100", "line": "original", "family": "Espresso"}]
        merged, new = merge_catalogue(local, [
            {"name": "Arpeggio", "sku": "100", "line": "original", "family": "Espresso"},
            {"name": "Arpeggio Extra", "sku": "100", "line": "original", "family": "Espresso"},
            {"name": "Fresh Pod", "sku": "200", "line": "vertuo", "family": "mug"},
            {"name": "Fresh Pod", "sku": "200", "line": "vertuo", "family": "Mug"},
            {"name": "Gone Pod", "sku": "300", "line": "original", "family": "Espresso", "available": False},
            {"name": "Arpeggio", "line": "original", "family": "lungo"},
        ])
        assert [e["name"] for e in merged] == ["Arpeggio", "Fresh Pod"], merged
        assert [e["name"] for e in new] == ["Fresh Pod"] and new[0]["sku"] == "200"
        # the two systems share capsule names: a same-name capsule of the other line is new
        _m, new2 = merge_catalogue(
            [{"name": "Colombia", "sku": "1", "line": "vertuo", "family": "Mug"}],
            [{"name": "Colombia", "sku": "2", "line": "original", "family": "Espresso"}])
        assert [e["name"] for e in new2] == ["Colombia"] and new2[0]["sku"] == "2", new2
        assert new[0]["family"] == "Mug" and new[0]["line"] == "vertuo"  # normalised for the picker

        _scanner_entries[:] = [
            {"name": "Vertuo New", "sku": "v1", "line": "vertuo", "family": "Mug"},
            {"name": "Original New", "sku": "o1", "line": "original", "family": "lungo"},
        ]
        st = current_state(conn)
        assert [c["name"] for c in st["new_capsules"]] == ["Original New"], st["new_capsules"]
        assert "Original New" in [k["name"] for k in st["known"]]
        # a dead host keeps the last good data and raises nothing
        global SCANNER_URL
        old_url, SCANNER_URL = SCANNER_URL, "http://127.0.0.1:9/"
        assert refresh_scanner()[0]["name"] == "Vertuo New"
        SCANNER_URL = old_url
        _scanner_entries.clear()
        assert current_state(conn)["new_capsules"] == []
    print("selftest ok")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind calls socket.getfqdn() for the server name, which does a
        # reverse-DNS lookup and can hang for minutes on machines without working DNS.
        TCPServer.server_bind(self)
        self.server_name = HOST
        self.server_port = self.server_address[1]


def main() -> None:
    if "--selftest" in sys.argv:
        selftest()
        return
    conn = connect()
    init_db(conn)
    notify_async()  # catch up on reminders that came due while the server was down
    if SCANNER_URL:
        threading.Thread(target=scanner_daemon, daemon=True).start()
    server = Server((HOST, PORT), Handler)
    print(f"Nespresso stats → http://{HOST}:{PORT}  (db: {DB_PATH})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
