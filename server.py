#!/usr/bin/env python3
"""Nespresso Vertuo capsule inventory dashboard.

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

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("NESPRESSO_DB", ROOT / "data" / "nespresso.db"))
CATALOG = ROOT / "capsules.json"
INDEX = ROOT / "static" / "index.html"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8787"))

FAMILIES = ["Espresso", "Double Espresso", "Gran Lungo", "Mug", "Alto", "Carafe", "Alto XL"]

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


def normalize_family(value) -> str:
    if value is None:
        return "Unknown"
    return FAMILY_ALIASES.get(str(value).strip().lower(), str(value).strip().title())


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
            family    TEXT NOT NULL,
            color     TEXT NOT NULL DEFAULT '#6b4f3a',
            image     TEXT NOT NULL DEFAULT '',
            count     INTEGER NOT NULL DEFAULT 0,
            notes     TEXT NOT NULL DEFAULT '',
            price     REAL NOT NULL DEFAULT 0,
            threshold INTEGER NOT NULL DEFAULT 2,
            intensity INTEGER NOT NULL DEFAULT 0
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
                       ("machine_image", ""), ("sleeve_size", "10")):
        conn.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (key, value))
    if conn.execute("SELECT COUNT(*) FROM capsules").fetchone()[0] == 0 and seed_path.exists():
        seed = json.loads(seed_path.read_text())
        conn.executemany(
            "INSERT INTO capsules (name, family, color, image, intensity)"
            " VALUES (:name, :family, :color, :image, :intensity)",
            [{**c, "image": c.get("image", ""), "intensity": c.get("intensity", 0)} for c in seed],
        )
    conn.commit()


def migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release (old DBs)."""
    capsule_cols = {r[1] for r in conn.execute("PRAGMA table_info(capsules)")}
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
    brew_cols = {r[1] for r in conn.execute("PRAGMA table_info(brews)")}
    if "delta" not in brew_cols:
        conn.execute("ALTER TABLE brews ADD COLUMN delta INTEGER NOT NULL DEFAULT -1")
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


def brew_detected(conn, family, source="auto") -> dict:
    """A machine reported a brew of `family`. Decrement if unambiguous, else queue it."""
    fam = normalize_family(family)
    rows = conn.execute(
        "SELECT id, name, count FROM capsules WHERE lower(family) = lower(?) AND count > 0 ORDER BY name", (fam,)
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
            "SELECT COUNT(*) FROM capsules WHERE lower(family) = lower(?)", (fam,)
        ).fetchone()[0]
        return {"action": "unknown" if not in_stock else "out-of-stock", "family": fam}
    return {"action": "pending", "family": fam, "candidates": [r["name"] for r in rows]}


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


def brew_stats(conn) -> dict:
    """Raw consumption events for the client to aggregate (timezone-local)."""
    rows = conn.execute("SELECT ts, capsule_id, delta FROM brews ORDER BY id DESC LIMIT 5000")
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


def rank_by_color(conn, color: str, limit: int = 8) -> list[dict]:
    rows = [dict(r) for r in conn.execute(
        "SELECT id, name, family, color, image, count FROM capsules"
    )]
    for row in rows:
        row["distance"] = round(color_distance(color, row["color"]))
    rows.sort(key=lambda r: r["distance"])
    return rows[:limit]


def capsule_names_in(conn, text: str, rows=None) -> list[dict]:
    """Capsules whose full name is readable in a transcription, longest name first. A partial
    read matches nothing — 'DOUBLE ESPRESSO' fits three flavours, 'DOUBLE ESPRESSO CHIARO' one.
    Tokens count wherever they appear, since a pod prints its name around the base twice and a
    transcription happily reads them out of order. Matching the transcription (not the model's
    answer) is what stops a shortlisted name being handed back as a 'read'. Pass `rows` to match
    against the catalogue instead of the inventory."""
    words = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    if rows is None:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, name, family, color, image, count FROM capsules")]
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
    # every resolvable word points at the same capsule.
    found = {}
    for word in words:
        r = match_capsule(conn, word)
        if r:
            found[r["id"]] = r
    return list(found.values()) if len(found) == 1 else []


def match_capsule(conn, name: str) -> dict | None:
    """Resolve a name read off a photo to a capsule. Small models misspell what they read and
    often only catch part of it ('CHIARO' for Double Espresso Chiaro), and a bottom-of-pod photo
    is silver, which shortlists the wrong capsules entirely — so match the printed name against
    the whole catalogue. ponytail: difflib + a unique-token rule, no fuzzy scoring of our own."""
    rows = [dict(r) for r in conn.execute(
        "SELECT id, name, family, color, image, count FROM capsules"
    )]
    keys = {re.sub(r"[^a-z0-9]", "", r["name"].lower()): r for r in rows}
    names = {r["id"]: re.findall(r"[a-z0-9]+", r["name"].lower()) for r in rows}
    needle = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if not needle:
        return None
    if needle in keys:
        return keys[needle]
    # A partial read is only usable when exactly one name ends with it: 'CHIARO' → Double
    # Espresso Chiaro, while 'DOUBLE ESPRESSO' fits three flavours and stays a colour question.
    tokens = re.findall(r"[a-z0-9]+", (name or "").lower())
    tails = [r for r in rows if names[r["id"]][-len(tokens):] == tokens]
    if len(tails) == 1:
        return tails[0]
    # Typo fallback ('VOLTESO'), refusing to guess when two names fit equally well.
    scored = sorted(((difflib.SequenceMatcher(None, needle, k).ratio(), k) for k in keys), reverse=True)
    if scored and scored[0][0] >= 0.8 and (len(scored) == 1 or scored[1][0] < scored[0][0]):
        return keys[scored[0][1]]
    return None


def vision_pick(image_data_url: str, candidates: list[dict]) -> dict | None:
    """Ask an OpenAI-compatible vision model. Exact when the photo shows a printed name —
    the capsule's underside (upside-down pod: name around the aluminium base next to the cup
    size) or a sleeve/box. On a top-down photo of the bare dome it only refines colour."""
    if not (VISION_BASE_URL and VISION_MODEL) or not image_data_url:
        return None
    names = [c["name"] for c in candidates]
    prompt = (
        "This is a photo of a Nespresso Vertuo capsule or its packaging. An upside-down pod "
        "prints the capsule name around the aluminium base next to the cup size, and sleeves and "
        "boxes print it too. Transcribe every word you can actually read into 'text' — never "
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


def identify(conn, color: str, image_data_url: str | None = None) -> dict:
    candidates = rank_by_color(conn, color)
    result = {"color": color, "candidates": candidates, "vision": None,
              "vision_enabled": bool(VISION_BASE_URL and VISION_MODEL)}
    if image_data_url:
        result["vision"] = vision_pick(image_data_url, candidates)
    vis = result["vision"]
    if vis and not vis.get("error"):
        text = (vis.get("text") or "").strip()
        hits = capsule_names_in(conn, text) if text else []
        if hits:  # a name read off the pod outranks the colour shortlist
            vis["name"] = hits[0]["name"]
            result["candidates"] = [hits[0]] + [c for c in candidates if c["id"] != hits[0]["id"]]
        elif text:
            # Legible, but no capsule by that name is in the inventory. If the catalogue knows it,
            # say so — the add dialog can prefill it from there — rather than fall back to colour.
            cat = capsule_names_in(conn, text, rows=known_catalogue())
            vis["name"] = cat[0]["name"] if cat else None
            vis["not_owned"] = bool(cat)
        elif vis.get("name"):
            # No text at all: the model's colour opinion is worth less than the colour ranking
            # the browser already did, so report it without reordering the candidates.
            hit = match_capsule(conn, vis["name"])
            if hit:
                vis["name"] = hit["name"]
    return result


def set_capsule_order(conn, ids: list[int]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings VALUES ('capsule_order', ?)",
        (",".join(str(int(i)) for i in ids),),
    )
    conn.commit()


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


def ntfy_send(url: str, message: str, title: str = "Nespresso") -> str | None:
    """POST to an ntfy topic (ntfy.sh or self-hosted). Returns an error string, or None."""
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
    the next one. Messages are English (the server doesn't know the reader's UI language).
    ponytail: episodes are tracked in two settings keys, not a new table."""
    url = get_setting(conn, "ntfy_url")
    if not url:
        return  # unconfigured: don't record episodes either, so configuring it later
    send = send or ntfy_send    # immediately reports what is low / due right now
    low = {r["id"] for r in conn.execute("SELECT id FROM capsules WHERE count <= threshold")}
    notified = {int(x) for x in get_setting(conn, "notified_low").split(",") if x.isdigit()}
    for cid in sorted(low - notified):
        r = conn.execute("SELECT name, count FROM capsules WHERE id = ?", (cid,)).fetchone()
        if r:  # a failed send still marks the episode done — a retry storm is worse
            err = send(url, f"{r['name']}: only {r['count']} left", title="Nespresso - running low")
            if err:
                print(f"notify failed: {err}", file=sys.stderr)
    conn.execute("INSERT OR REPLACE INTO settings VALUES ('notified_low', ?)",
                 (",".join(str(i) for i in sorted(low)),))
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


def known_catalogue(path=CATALOG) -> list[dict]:
    """Catalogue entries (name, family, colour, photo, intensity) — the capsules the app
    knows about. Read per call so edits to capsules.json land without a restart.
    ponytail: no cache, the file is a few kB and /api/state is not hot."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return []


def current_state(conn) -> dict:
    machine = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings")}
    capsules = [dict(r) for r in conn.execute("SELECT * FROM capsules")]
    # Known-but-not-inventoried capsules, for the "add capsule" picker.
    owned = {c["name"].strip().lower() for c in capsules}
    known = [c for c in known_catalogue() if c.get("name", "").strip().lower() not in owned]
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
        "maintenance": maintenance_state(conn),
        "machine": machine, "families": FAMILIES, "known": known,
    }


# --------------------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "NespressoStats/1.0"

    def log_message(self, *args):  # keep the console clean
        pass

    def _send(self, code, payload=None, ctype="application/json"):
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
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
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
        self._send(200, target.read_bytes(), ctype)

    def do_POST(self):
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
                result = brew_detected(conn, payload.get("family"), payload.get("source", "auto"))
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
            elif url.path == "/api/maintenance":
                self._send(200, log_maintenance(conn, payload.get("task")))
            elif url.path == "/api/import":
                self._send(200, import_data(conn, payload))
            elif url.path == "/api/notify-test":
                target = payload.get("url") or get_setting(conn, "ntfy_url")
                if not target:
                    raise ValueError("no ntfy topic URL configured")
                err = ntfy_send(target, "Test notification — your Nespresso dashboard is wired up.",
                                title="Nespresso - test")
                self._send(200, {"ok": err is None, "error": err})
            elif url.path == "/api/settings":
                for key in ("machine_name", "machine_model", "machine_image", "sleeve_size",
                            "clean_days", "descale_days", "ntfy_url"):
                    if key in payload:
                        conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, payload[key]))
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
                """UPDATE capsules SET name=:name, family=:family, color=:color,
                   image=:image, notes=:notes, count=:count, price=:price,
                   threshold=:threshold, intensity=:intensity, special=:special WHERE id=:id""",
                {**fields, "count": count, "price": price, "threshold": threshold,
                 "intensity": intensity, "special": special, "id": payload["id"]},
            )
        else:
            conn.execute(
                """INSERT INTO capsules (name, family, color, image, notes, count, price, threshold, intensity, special)
                   VALUES (:name, :family, :color, :image, :notes, :count, :price, :threshold, :intensity, :special)""",
                {**fields, "count": count, "price": price, "threshold": threshold,
                 "intensity": intensity, "special": special},
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
        url = urlparse(self.path)
        id_ = parse_qs(url.query).get("id", [None])[0]
        if not id_:
            self._send(400, {"error": "id required"})
            return
        conn = self._conn()
        if url.path == "/api/capsules":
            conn.execute("DELETE FROM capsules WHERE id=?", (id_,))
            conn.commit()
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

        # known-capsule picker: a fully seeded inventory has nothing left to offer
        assert current_state(conn)["known"] == []
        other = conn.execute(
            "SELECT id, name, color FROM capsules WHERE name NOT IN ('Fortado','Melozio','Intenso') LIMIT 1"
        ).fetchone()
        conn.execute("DELETE FROM capsules WHERE id=?", (other["id"],))
        known = current_state(conn)["known"]
        assert [k["name"] for k in known] == [other["name"]], known
        assert known[0]["color"] == other["color"] and known[0]["image"], known

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
        ids = [r[0] for r in conn.execute("SELECT id FROM capsules")]
        set_capsule_order(conn, list(reversed(ids)))
        assert [c["id"] for c in current_state(conn)["capsules"]] == list(reversed(ids))

        # migration is idempotent on an already-migrated db
        migrate(conn)

        # backup: export → wipe → import restores everything; a bad upload rolls back
        backup = export_data(conn)
        assert backup["capsules"] and backup["brews"], backup.keys()
        conn.executescript("DELETE FROM capsules; DELETE FROM brews; DELETE FROM settings")
        assert conn.execute("SELECT COUNT(*) FROM capsules").fetchone()[0] == 0
        assert import_data(conn, backup)["capsules"] == len(backup["capsules"])
        assert conn.execute("SELECT COUNT(*) FROM brews").fetchone()[0] == len(backup["brews"])
        assert conn.execute("SELECT value FROM settings WHERE key='sleeve_size'").fetchone()[0] == "10"
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
    server = Server((HOST, PORT), Handler)
    print(f"Nespresso stats → http://{HOST}:{PORT}  (db: {DB_PATH})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
