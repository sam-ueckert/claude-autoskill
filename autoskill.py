#!/usr/bin/env python3
"""
AutoSkill for Claude Code
Watches conversations via hooks, archives turns to SQLite,
and extracts reusable SKILL.md files using the Claude API.

Based on MUSE-AutoSkill (arxiv 2605.27366):
  creation → memory → management → evaluation → refinement lifecycle.

Usage:
  autoskill.py --capture          # Called by hooks (reads JSON from stdin)
  autoskill.py --extract <sid>    # Extract skills for one session
  autoskill.py --import           # Batch import all historical transcripts
  autoskill.py --extract-all      # Extract from all unprocessed sessions
  autoskill.py --search [query]   # Interactive search + select sessions to add
  autoskill.py --status           # Show counts and recent skills
  autoskill.py --refine           # Re-evaluate and improve existing skills
  autoskill.py --sync-repo        # Bulk sync all as-* skills to repo_sync_dir
"""

import json
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────

AUTOSKILL_DIR = Path.home() / ".claude" / "autoskill"
DB_PATH       = AUTOSKILL_DIR / "data" / "archive.db"
CONFIG_PATH   = AUTOSKILL_DIR / "config.json"
LOG_PATH      = AUTOSKILL_DIR / "autoskill.log"
SKILLS_DIR    = Path.home() / ".claude" / "skills"

# ── Config ─────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "enabled":               True,
    "extract_every_turns":   8,      # extract after every N archived turns
    "min_turns_extraction":  4,      # don't extract if fewer turns than this
    "max_skills_per_run":    3,      # max skills generated per extraction
    "model":                 "claude-sonnet-4-6",
    "skill_prefix":          "as-",  # prefix on autoskill dir names
    "max_content_chars":     2000,   # truncate each turn to this length
    "max_turns_context":     40,     # max turns sent to extraction LLM
    "log_level":             "info", # info | debug | error
}

def load_config() -> dict:
    try:
        if CONFIG_PATH.exists():
            return {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text())}
    except Exception:
        pass
    return DEFAULT_CONFIG.copy()

# ── Logging ────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "info"):
    cfg = load_config()
    levels = {"debug": 0, "info": 1, "error": 2}
    if levels.get(level, 1) < levels.get(cfg.get("log_level", "info"), 1):
        return
    ts = datetime.now().isoformat(timespec="seconds")
    line = f"[{ts}] [{level.upper():5}] {msg}\n"
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line)
    except Exception:
        pass

# ── Database ───────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS turns (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT    NOT NULL,
            role        TEXT    NOT NULL,   -- user | assistant
            content     TEXT    NOT NULL,
            cwd         TEXT    DEFAULT '',
            ts          TEXT    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS extractions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      TEXT    NOT NULL,
            turn_count      INTEGER NOT NULL,
            skills_created  INTEGER NOT NULL DEFAULT 0,
            ts              TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_turns_session     ON turns(session_id);
        CREATE INDEX IF NOT EXISTS idx_extractions_sess  ON extractions(session_id);
    """)
    conn.commit()
    return conn

def save_turn(conn, session_id: str, role: str, content: str, cwd: str = ""):
    conn.execute(
        "INSERT INTO turns (session_id, role, content, cwd, ts) VALUES (?,?,?,?,?)",
        (session_id, role, content.strip(), cwd, datetime.now().isoformat())
    )
    conn.commit()

def get_turn_count(conn, session_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM turns WHERE session_id=?", (session_id,)
    ).fetchone()
    return row[0] if row else 0

def get_last_extracted_count(conn, session_id: str) -> int:
    row = conn.execute(
        "SELECT turn_count FROM extractions WHERE session_id=? ORDER BY id DESC LIMIT 1",
        (session_id,)
    ).fetchone()
    return row[0] if row else 0

def get_session_turns(conn, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM turns WHERE session_id=? ORDER BY id",
        (session_id,)
    ).fetchall()
    return [{"role": r, "content": c} for r, c in rows]

def session_already_has_turn(conn, session_id: str, role: str, content_prefix: str) -> bool:
    """Rough dedup check — avoids double-archiving on repeated hook fires."""
    row = conn.execute(
        "SELECT id FROM turns WHERE session_id=? AND role=? AND content LIKE ? ORDER BY id DESC LIMIT 1",
        (session_id, role, content_prefix[:80] + "%")
    ).fetchone()
    return row is not None

# ── Transcript Reading ─────────────────────────────────────────────────────

def read_last_assistant_from_transcript(transcript_path: str) -> str | None:
    """
    Scan transcript JSONL backwards for the most recent assistant text.
    Combines multiple assistant blocks (thinking + text + tool_use tags).
    """
    try:
        lines = Path(transcript_path).read_text(errors="replace").splitlines()
    except Exception as e:
        log(f"Cannot read transcript {transcript_path}: {e}", "error")
        return None

    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except Exception:
            continue

        if entry.get("type") != "assistant":
            continue

        msg     = entry.get("message", {})
        content = msg.get("content", [])

        if isinstance(content, str) and content.strip():
            return content.strip()

        if isinstance(content, list):
            parts = []
            for block in content:
                btype = block.get("type", "")
                if btype == "text":
                    t = block.get("text", "").strip()
                    if t:
                        parts.append(t)
                elif btype == "tool_use":
                    name  = block.get("name", "")
                    inp   = json.dumps(block.get("input", {}))[:200]
                    parts.append(f"[Tool: {name}({inp})]")
            if parts:
                return "\n".join(parts)

    return None

# ── Existing Skills ────────────────────────────────────────────────────────

def get_existing_skills() -> list[dict]:
    """Return name + description for every installed skill."""
    skills = []
    if not SKILLS_DIR.exists():
        return skills
    for skill_dir in SKILLS_DIR.iterdir():
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            text = skill_md.read_text()
            name = skill_dir.name
            desc = ""
            if text.startswith("---"):
                parts = text.split("---", 2)
                if len(parts) >= 3:
                    for fmline in parts[1].splitlines():
                        if fmline.startswith("name:"):
                            name = fmline.split(":", 1)[1].strip().strip('"')
                        elif fmline.startswith("description:"):
                            desc = fmline.split(":", 1)[1].strip().strip('"')
            skills.append({"name": name, "description": desc})
        except Exception:
            pass
    return skills

# ── Extraction Prompt ──────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are AutoSkill, an expert analyst embedded in Claude Code (Anthropic's AI coding assistant).

Your job: analyze a Claude Code conversation and extract 0-{max_skills} reusable SKILLS worth teaching \
to future Claude Code sessions.

━━ WHAT MAKES A GOOD SKILL ━━
A skill is worth extracting when it:
  • Captures a non-obvious multi-step workflow (not "run git status")
  • Encodes a recurring procedure for a class of problems
  • Documents a specific tool combination, flag set, or config trick
  • Describes a domain-specific debugging or investigation method
  • Represents learned knowledge that saves time on repeat encounters

DO NOT extract:
  • Single-command answers (git add, npm install, etc.)
  • One-off project-specific patches
  • Common knowledge any developer already knows
  • Skills already in the existing skills list (exact or near duplicate)

━━ OUTPUT FORMAT ━━
Return ONLY a JSON array (no markdown fences, no commentary):
[
  {{
    "name":        "kebab-case-name-max-5-words",
    "description": "Crisp one-liner. Written to MATCH the keywords a user types when needing this skill.",
    "content":     "Full skill body in markdown (no frontmatter). Must include:\\n## When to use\\n## Steps\\n## Notes (optional)"
  }}
]

Return [] if nothing is worth extracting.
"""

def build_extraction_prompt(turns: list[dict], config: dict, existing: list[dict]) -> tuple[str, str]:
    max_skills = config["max_skills_per_run"]
    max_chars  = config["max_content_chars"]
    max_turns  = config["max_turns_context"]

    system = SYSTEM_PROMPT.format(max_skills=max_skills)

    existing_str = (
        "\n".join(f"  - {s['name']}: {s['description']}" for s in existing)
        if existing else "  (none yet)"
    )

    conv_lines = []
    for t in turns[-max_turns:]:
        role    = t["role"].upper()
        content = t["content"][:max_chars]
        if len(t["content"]) > max_chars:
            content += " …[truncated]"
        conv_lines.append(f"[{role}]\n{content}")

    user_msg = f"""\
EXISTING SKILLS (avoid duplicating):
{existing_str}

CONVERSATION:
{'=' * 60}
{chr(10).join(conv_lines)}
{'=' * 60}

Extract up to {max_skills} reusable skills. Return JSON array only."""

    return system, user_msg

# ── Skill Writer ───────────────────────────────────────────────────────────

def sanitize_name(raw: str) -> str:
    name = raw.strip().lower()
    name = re.sub(r"[^a-z0-9]+", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    return name[:60]

def write_skill(skill: dict, prefix: str = "as-") -> bool:
    raw_name = skill.get("name", "").strip()
    if not raw_name:
        return False

    name      = sanitize_name(raw_name)
    # Strip prefix if LLM already included it in the generated name
    if prefix and name.startswith(prefix.rstrip("-")):
        name = name[len(prefix):]
    dir_name  = f"{prefix}{name}"
    skill_dir = SKILLS_DIR / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)

    description = skill.get("description", "").strip()
    content     = skill.get("content", "").strip()

    if not content:
        return False

    # Derive a readable title from the name
    title = " ".join(w.capitalize() for w in name.replace("-", " ").split())

    skill_md = f"""---
name: {dir_name}
description: {description}
---

# {title}

{content}
"""
    (skill_dir / "SKILL.md").write_text(skill_md)
    log(f"Wrote skill: {dir_name}")
    _sync_skill_to_repo(dir_name)
    return True


def _git(repo_dir, *args, timeout=60):
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _git_sync_push(repo_dir) -> bool:
    """Pull-before-push so concurrent pushes from other machines (this repo is
    shared across hosts) self-heal instead of silently diverging. Pulls with
    --rebase --autostash, then pushes any local commits (including backlog).
    On a genuine conflict it aborts the rebase rather than leaving a half-done
    state, and defers the push for manual resolution. Returns True iff pushed."""
    try:
        pull = _git(repo_dir, "pull", "--rebase", "--autostash", timeout=90)
        if pull.returncode != 0:
            _git(repo_dir, "rebase", "--abort", timeout=30)
            log(f"sync: pull --rebase failed, push deferred (resolve manually): "
                f"{pull.stderr.strip()[:200]}", "error")
            return False
        push = _git(repo_dir, "push", timeout=90)
        if push.returncode != 0:
            log(f"sync: push failed: {push.stderr.strip()[:200]}", "error")
            return False
        return True
    except Exception as e:
        log(f"sync: git pull/push error: {e}", "error")
        return False


def _sync_skill_to_repo(dir_name: str):
    """Copy one skill to repo_sync_dir and git commit, then pull-before-push.
    Silent if not configured."""
    config = load_config()
    repo_dir_str = config.get("repo_sync_dir", "")
    if not repo_dir_str:
        return
    repo_dir = Path(repo_dir_str).expanduser()
    if not (repo_dir / ".git").exists():
        log(f"sync: repo not found at {repo_dir}", "debug")
        return
    src = SKILLS_DIR / dir_name
    if not src.exists() or not (src / "SKILL.md").exists():
        return
    try:
        dst = repo_dir / "skills" / dir_name
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / "SKILL.md", dst / "SKILL.md")
        subprocess.run(
            ["git", "-C", str(repo_dir), "add", f"skills/{dir_name}/SKILL.md"],
            capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "-C", str(repo_dir), "commit", "-m", f"autoskill: update {dir_name}"],
            capture_output=True, text=True, timeout=30,
        )
        # Always reconcile + push (even if this commit was a no-op) so any
        # backlog from earlier failed pushes drains automatically.
        _git_sync_push(repo_dir)
        log(f"sync: synced {dir_name} to {repo_dir.name}")
    except Exception as e:
        log(f"sync: error for {dir_name}: {e}", "error")

# ── Core Extraction ────────────────────────────────────────────────────────

def run_extraction(session_id: str, config: dict, conn: sqlite3.Connection) -> int:
    turns = get_session_turns(conn, session_id)
    min_t = config["min_turns_extraction"]

    if len(turns) < min_t:
        log(f"Session {session_id[:8]}: only {len(turns)} turns (<{min_t}), skipping")
        return 0

    existing = get_existing_skills()
    system, user_msg = build_extraction_prompt(turns, config, existing)

    log(f"Extracting from session {session_id[:8]} ({len(turns)} turns, {len(existing)} existing skills)")

    # Combine system + user into one prompt for `claude -p` (which has Claude Code auth)
    full_prompt = f"{system}\n\n{'─' * 60}\n\n{user_msg}"

    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )
        raw = result.stdout.strip()
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[:300])
    except Exception as e:
        log(f"claude -p error for {session_id[:8]}: {e}", "error")
        conn.execute(
            "INSERT INTO extractions (session_id, turn_count, skills_created, ts) VALUES (?,?,?,?)",
            (session_id, len(turns), 0, datetime.now().isoformat())
        )
        conn.commit()
        return 0

    # Strip accidental markdown fences
    raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\n?```$", "", raw, flags=re.MULTILINE)

    try:
        skills = json.loads(raw)
        if not isinstance(skills, list):
            raise ValueError("expected list")
    except Exception as e:
        log(f"JSON parse error for {session_id[:8]}: {e} | raw={raw[:200]}", "error")
        skills = []

    count = 0
    for skill in skills:
        if write_skill(skill, config["skill_prefix"]):
            count += 1

    conn.execute(
        "INSERT INTO extractions (session_id, turn_count, skills_created, ts) VALUES (?,?,?,?)",
        (session_id, len(turns), count, datetime.now().isoformat())
    )
    conn.commit()
    log(f"Session {session_id[:8]}: extracted {count} skill(s)")
    return count

# ── Hook Handlers ──────────────────────────────────────────────────────────

def maybe_run_extraction(session_id: str, config: dict, conn: sqlite3.Connection):
    """Run extraction inline if turn threshold exceeded.

    The hook process already runs asynchronously (async: true in settings.json),
    so blocking here is fine — Claude Code has already moved on.
    We intentionally avoid spawning a sub-subprocess because that child would
    not inherit Claude Code's auth environment (needed for `claude -p`).
    """
    current = get_turn_count(conn, session_id)
    last    = get_last_extracted_count(conn, session_id)
    delta   = current - last

    log(f"Session {session_id[:8]}: {current} turns, {delta} since last extraction", "debug")

    if delta >= config["extract_every_turns"]:
        log(f"Threshold reached ({delta} turns), running extraction inline")
        run_extraction(session_id, config, conn)

def handle_stop(payload: dict, config: dict, conn: sqlite3.Connection):
    session_id      = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")
    cwd             = payload.get("cwd", "")

    assistant_text = read_last_assistant_from_transcript(transcript_path)
    if not assistant_text:
        log(f"Stop: no assistant text found in transcript", "debug")
        return

    if not session_already_has_turn(conn, session_id, "assistant", assistant_text):
        save_turn(conn, session_id, "assistant", assistant_text, cwd)
        log(f"Stop: archived assistant turn ({len(assistant_text)} chars)", "debug")
    else:
        log(f"Stop: duplicate assistant turn, skipping", "debug")
        return

    maybe_run_extraction(session_id, config, conn)

def handle_user_prompt(payload: dict, _config: dict, conn: sqlite3.Connection):
    session_id = payload.get("session_id", "")
    prompt     = payload.get("prompt", "").strip()
    cwd        = payload.get("cwd", "")

    if not prompt:
        return

    if not session_already_has_turn(conn, session_id, "user", prompt):
        save_turn(conn, session_id, "user", prompt, cwd)
        log(f"UserPromptSubmit: archived user turn ({len(prompt)} chars)", "debug")

def handle_session_end(payload: dict, config: dict, conn: sqlite3.Connection):
    session_id = payload.get("session_id", "")
    current    = get_turn_count(conn, session_id)
    last       = get_last_extracted_count(conn, session_id)

    if current > last + 1:
        log(f"SessionEnd: {current - last} unextracted turns, running extraction")
        run_extraction(session_id, config, conn)

def handle_pre_compact(payload: dict, config: dict, conn: sqlite3.Connection):
    """Extract before context window is compacted so no context is lost."""
    session_id = payload.get("session_id", "")
    current    = get_turn_count(conn, session_id)
    last       = get_last_extracted_count(conn, session_id)

    if current > last + 1:
        log(f"PreCompact: {current - last} unextracted turns, running extraction")
        run_extraction(session_id, config, conn)

# ── Transcript Helpers ────────────────────────────────────────────────────

def _import_transcript_file(tp: Path, session_id: str, cwd: str, conn) -> int:
    """Parse one .jsonl transcript and archive its turns. Returns turns added."""
    turns_added = 0
    for raw_line in tp.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(raw_line)
        except Exception:
            continue

        etype = entry.get("type")
        msg   = entry.get("message", {})

        if etype == "user":
            content = msg.get("content", [])
            text = ""
            if isinstance(content, list):
                text = " ".join(
                    b.get("text", "") for b in content if b.get("type") == "text"
                )
            elif isinstance(content, str):
                text = content
            if text.strip():
                save_turn(conn, session_id, "user", text.strip(), cwd)
                turns_added += 1

        elif etype == "assistant":
            content = msg.get("content", [])
            parts = []
            if isinstance(content, list):
                for b in content:
                    if b.get("type") == "text" and b.get("text", "").strip():
                        parts.append(b["text"])
                    elif b.get("type") == "tool_use":
                        parts.append(f"[Tool: {b.get('name','')}]")
            elif isinstance(content, str) and content.strip():
                parts = [content]
            if parts:
                save_turn(conn, session_id, "assistant", "\n".join(parts).strip(), cwd)
                turns_added += 1

    return turns_added


def scan_transcripts(query: str = "", conn=None) -> list[dict]:
    """
    Return metadata for all project transcripts, optionally filtered by query.
    Query matches case-insensitively against title and project directory.
    """
    projects = Path.home() / ".claude" / "projects"
    results  = []

    for tp in sorted(projects.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        session_id  = tp.stem
        title       = ""
        cwd         = ""
        file_turns  = 0   # actual user+assistant entries counted while reading

        try:
            for raw_line in tp.read_text(errors="replace").splitlines():
                try:
                    entry = json.loads(raw_line)
                except Exception:
                    continue
                etype = entry.get("type")
                if etype == "ai-title":
                    t = entry.get("aiTitle", "")
                    if t:
                        title = t          # keep last — titles refine over the session
                elif etype in ("user", "assistant"):
                    file_turns += 1
                    if etype == "user" and not cwd:
                        cwd = entry.get("cwd", "")
        except Exception:
            continue

        if not title:
            title = f"({session_id[:8]})"

        # Derive a short display name from the actual cwd (last 2 path components)
        if cwd:
            parts   = [p for p in cwd.strip("/").split("/") if p]
            project = "/".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else tp.parent.name)
        else:
            project = tp.parent.name

        query_lower = query.lower()
        if query_lower and query_lower not in title.lower() and query_lower not in project.lower():
            continue

        # DB status
        status         = "new"
        archived_turns = 0
        skills_created = 0
        if conn:
            archived_turns = get_turn_count(conn, session_id)
            if archived_turns > 0:
                row = conn.execute(
                    "SELECT COALESCE(SUM(skills_created),0) FROM extractions WHERE session_id=?",
                    (session_id,)
                ).fetchone()
                skills_created = row[0] if row else 0
                status = "extracted" if skills_created > 0 else "imported"

        display_turns = archived_turns if archived_turns else file_turns

        results.append({
            "session_id":     session_id,
            "title":          title,
            "project":        project,
            "cwd":            cwd,
            "date":           datetime.fromtimestamp(tp.stat().st_mtime).strftime("%Y-%m-%d"),
            "turns":          display_turns,
            "status":         status,
            "skills_created": skills_created,
            "path":           tp,
        })

    return results


def parse_selection(raw: str, max_n: int) -> list[int]:
    """Parse '1 3 5', '2-4', 'all', or mixed into a sorted list of 1-based indices."""
    raw = raw.strip().lower()
    if raw == "all":
        return list(range(1, max_n + 1))
    indices: set[int] = set()
    for token in re.split(r"[\s,]+", raw):
        token = token.strip()
        if not token:
            continue
        m = re.match(r"^(\d+)-(\d+)$", token)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            indices.update(n for n in range(lo, hi + 1) if 1 <= n <= max_n)
        elif token.isdigit():
            n = int(token)
            if 1 <= n <= max_n:
                indices.add(n)
    return sorted(indices)


# ── CLI Commands ───────────────────────────────────────────────────────────

def cmd_capture():
    """Hook entry point — reads JSON payload from stdin."""
    config = load_config()
    if not config.get("enabled", True):
        return

    try:
        raw     = sys.stdin.read()
        payload = json.loads(raw)
    except Exception as e:
        log(f"Failed to parse hook payload: {e}", "error")
        return

    event = payload.get("hook_event_name", "")
    log(f"Hook: {event} | session={payload.get('session_id','')[:8]}", "debug")

    conn = init_db()
    try:
        if   event == "Stop":              handle_stop(payload, config, conn)
        elif event == "UserPromptSubmit":  handle_user_prompt(payload, config, conn)
        elif event == "SessionEnd":        handle_session_end(payload, config, conn)
        elif event == "PreCompact":        handle_pre_compact(payload, config, conn)
    finally:
        conn.close()


def cmd_extract(session_id: str):
    config = load_config()
    conn   = init_db()
    count  = run_extraction(session_id, config, conn)
    conn.close()
    print(f"Extracted {count} skill(s) from session {session_id[:8]}")


def cmd_import():
    """Batch import all historical Claude Code transcripts into the archive."""
    conn        = init_db()
    projects    = Path.home() / ".claude" / "projects"
    transcripts = sorted(projects.glob("*/*.jsonl"))

    print(f"Found {len(transcripts)} transcript(s)")
    imported = 0

    for tp in transcripts:
        session_id = tp.stem
        if get_turn_count(conn, session_id) > 0:
            continue  # already imported

        cwd = tp.parent.name.lstrip("-").replace("-", "/")
        try:
            turns_added = _import_transcript_file(tp, session_id, cwd, conn)
            if turns_added:
                print(f"  {session_id[:8]}… {turns_added} turns  ({cwd})")
                imported += 1
        except Exception as e:
            print(f"  Error {session_id[:8]}: {e}")

    conn.close()
    print(f"\nImported {imported} new session(s).")
    print("Run  autoskill.py --extract-all  to generate skills from historical data.")


def cmd_search(query: str):
    """Interactive search: list matching sessions, select which to import+extract."""
    conn     = init_db()
    sessions = scan_transcripts(query, conn)
    conn.close()

    if not sessions:
        q_msg = f' matching "{query}"' if query else ""
        print(f"No sessions found{q_msg}.")
        return

    q_msg = f' matching "{query}"' if query else ""
    print(f"\nFound {len(sessions)} session(s){q_msg}:\n")

    # Column widths
    W_TITLE   = 48
    W_PROJECT = 22
    hdr = f"  {'#':>3}  {'Title':<{W_TITLE}}  {'Project':<{W_PROJECT}}  {'Date':>10}  {'Turns':>5}  Status"
    sep = f"  {'─'*3}  {'─'*W_TITLE}  {'─'*W_PROJECT}  {'─'*10}  {'─'*5}  {'─'*16}"
    print(hdr)
    print(sep)

    for i, s in enumerate(sessions, 1):
        title   = s["title"][:W_TITLE]
        project = s["project"][:W_PROJECT]
        if s["status"] == "extracted":
            status_str = f"extracted ({s['skills_created']} skills)"
        elif s["status"] == "imported":
            status_str = "imported"
        else:
            status_str = "new"
        print(f"  {i:>3}  {title:<{W_TITLE}}  {project:<{W_PROJECT}}  {s['date']:>10}  {s['turns']:>5}  {status_str}")

    print()
    print("Select sessions to import+extract")
    print("  Numbers / ranges / 'all' / Enter to cancel  →  e.g.  1 3 5   2-4   all")
    try:
        raw = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if not raw:
        print("Cancelled.")
        return

    indices = parse_selection(raw, len(sessions))
    if not indices:
        print("No valid selection — use numbers, ranges (2-4), or 'all'.")
        return

    chosen = [sessions[i - 1] for i in indices]
    print(f"\n{len(chosen)} session(s) selected:")
    for s in chosen:
        note = f"  [{s['status']}]" if s["status"] != "new" else ""
        print(f"  • {s['title'][:65]}{note}")

    print()
    try:
        confirm = input("Import and extract? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if confirm != "y":
        print("Cancelled.")
        return

    config           = load_config()
    conn             = init_db()
    imported_count   = 0
    extracted_total  = 0

    for s in chosen:
        session_id = s["session_id"]
        print(f"\n  [{session_id[:8]}] {s['title'][:60]}")

        if s["status"] == "new":
            try:
                turns_added = _import_transcript_file(s["path"], session_id, s["cwd"], conn)
                if turns_added:
                    print(f"    imported {turns_added} turns")
                    imported_count += 1
                else:
                    print("    no turns found, skipping extraction")
                    continue
            except Exception as e:
                print(f"    import error: {e}")
                continue

        count = run_extraction(session_id, config, conn)
        extracted_total += count
        print(f"    extracted {count} skill(s)")

    conn.close()
    print(f"\nDone — {imported_count} session(s) imported, {extracted_total} skill(s) extracted.")


def cmd_extract_all():
    """Extract skills from every session that has unprocessed turns."""
    config = load_config()
    conn   = init_db()

    sessions = conn.execute("""
        SELECT t.session_id,
               COUNT(t.id)                   AS turns,
               COALESCE(MAX(e.turn_count),0) AS last_extracted
        FROM   turns t
        LEFT JOIN extractions e ON e.session_id = t.session_id
        GROUP BY t.session_id
        HAVING turns > last_extracted + 2
        ORDER BY turns DESC
    """).fetchall()

    print(f"Processing {len(sessions)} session(s)…")
    total = 0
    for session_id, turns, last in sessions:
        print(f"  {session_id[:8]}… {turns} turns ({turns - last} new)")
        total += run_extraction(session_id, config, conn)

    conn.close()
    print(f"\nTotal skills extracted: {total}")


def cmd_refine():
    """
    Refinement pass (MUSE lifecycle step 5):
    Re-evaluate existing autoskill skills for quality and merge near-duplicates.
    """
    config    = load_config()
    prefix    = config["skill_prefix"]
    batch_sz  = int(config.get("refine_batch_size", 10))
    timeout_s = int(config.get("refine_timeout", 150))
    # Name-sort so skill families (as-meross-*, as-scraper-*, as-tailscale-*)
    # land in the same batch — near-duplicates are still compared together.
    as_skills = sorted(
        (d for d in SKILLS_DIR.iterdir()
         if d.name.startswith(prefix) and (d / "SKILL.md").exists()),
        key=lambda d: d.name,
    )

    if not as_skills:
        print("No autoskill-generated skills found.")
        return

    n_batches = (len(as_skills) + batch_sz - 1) // batch_sz
    print(f"Refining {len(as_skills)} autoskill(s) in {n_batches} batch(es) of up to {batch_sz}…")

    # Process in small batches so each `claude -p` call stays fast. One batch
    # timing out is logged and skipped — it no longer aborts the whole pass.
    actions = []
    for bi in range(n_batches):
        batch = as_skills[bi * batch_sz:(bi + 1) * batch_sz]
        skill_texts = [f"### {d.name}\n{(d / 'SKILL.md').read_text()}" for d in batch]
        refine_prompt = f"""\
You are reviewing a batch of {len(batch)} auto-generated Claude Code skills
(one slice of a larger set). For each skill, assess:
1. Is the description precise enough to trigger on the right user request?
2. Are the instructions clear and actionable?
3. Should any two skills IN THIS BATCH be merged? (Only merge within this batch.)

Return ONLY a JSON array (no prose, no markdown fences):
[
  {{
    "name": "existing-dir-name",
    "action": "keep" | "update" | "merge" | "delete",
    "reason": "...",
    "new_description": "...",  // if action=update or merge
    "new_content": "..."        // if action=update or merge
  }}
]

SKILLS:
{'=' * 60}
{(chr(10) + '=' * 60 + chr(10)).join(skill_texts)}
"""
        try:
            result = subprocess.run(
                ["claude", "-p", "--output-format", "text"],
                input=refine_prompt,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip()[:200])
            raw = result.stdout.strip()
            raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.MULTILINE)
            raw = re.sub(r"\n?```$",       "", raw, flags=re.MULTILINE)
            batch_actions = json.loads(raw)
            actions.extend(batch_actions)
            print(f"  batch {bi + 1}/{n_batches}: {len(batch_actions)} action(s)")
        except Exception as e:
            print(f"  batch {bi + 1}/{n_batches} SKIPPED: {e}")
            continue

    for act in actions:
        name   = act.get("name", "")
        action = act.get("action", "keep")
        sd     = SKILLS_DIR / name
        if not sd.exists():
            continue

        if action == "delete":
            print(f"  DELETE {name}: {act.get('reason','')}")
            for f in sd.iterdir():
                f.unlink()
            sd.rmdir()

        elif action in ("update", "merge"):
            print(f"  UPDATE {name}: {act.get('reason','')}")
            new_desc    = act.get("new_description", "")
            new_content = act.get("new_content", "")
            if new_desc and new_content:
                title = " ".join(w.capitalize() for w in name.replace(prefix, "").replace("-", " ").split())
                (sd / "SKILL.md").write_text(f"""---
name: {name}
description: {new_desc}
---

# {title}

{new_content}
""")
                _sync_skill_to_repo(name)
        else:
            print(f"  KEEP   {name}")

    print("Refinement complete.")


def cmd_sync_repo():
    """Bulk sync all as-* skills to the configured repo_sync_dir."""
    config = load_config()
    repo_dir_str = config.get("repo_sync_dir", "")
    if not repo_dir_str:
        print("No repo_sync_dir set in config.json. Add: \"repo_sync_dir\": \"~/repos/claude-config\"")
        return
    repo_dir = Path(repo_dir_str).expanduser()
    if not (repo_dir / ".git").exists():
        print(f"Repo not found at {repo_dir}")
        return

    prefix = config["skill_prefix"]
    skills = sorted(
        d for d in SKILLS_DIR.iterdir()
        if d.name.startswith(prefix) and (d / "SKILL.md").exists()
    )
    if not skills:
        print("No autoskills found.")
        return

    print(f"Syncing {len(skills)} autoskill(s) to {repo_dir}…")
    for sd in skills:
        dst = repo_dir / "skills" / sd.name
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sd / "SKILL.md", dst / "SKILL.md")
        print(f"  {sd.name}")

    subprocess.run(["git", "-C", str(repo_dir), "add", "skills/"], capture_output=True)
    r = subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-m", f"autoskill: bulk sync {len(skills)} skill(s)"],
        capture_output=True, text=True,
    )
    if _git_sync_push(repo_dir):
        print(f"\nSynced and pushed {len(skills)} skill(s) to {repo_dir.name}")
    else:
        print(f"\nCopied {len(skills)} skill(s) — push deferred (see log; may be a conflict to resolve)")


def cmd_status():
    config = load_config()
    conn   = init_db()
    prefix = config["skill_prefix"]

    total_sessions   = conn.execute("SELECT COUNT(DISTINCT session_id) FROM turns").fetchone()[0]
    total_turns      = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    total_extractions= conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0]
    total_skills_made= conn.execute("SELECT COALESCE(SUM(skills_created),0) FROM extractions").fetchone()[0]

    installed = [
        d for d in SKILLS_DIR.iterdir()
        if d.name.startswith(prefix) and (d / "SKILL.md").exists()
    ] if SKILLS_DIR.exists() else []

    print(f"""
AutoSkill Status  ({datetime.now().strftime('%Y-%m-%d %H:%M')})
═══════════════════════════════════════════
Enabled:              {config['enabled']}
Model:                {config['model']}
Extract every:        {config['extract_every_turns']} turns
Max skills/run:       {config['max_skills_per_run']}
Skill prefix:         {prefix}

Archive:              {DB_PATH}
Sessions archived:    {total_sessions}
Turns archived:       {total_turns}
Extractions run:      {total_extractions}
Skills generated:     {total_skills_made}
Skills installed:     {len(installed)}

Recent autoskills:""")

    for d in sorted(installed, key=lambda x: x.stat().st_mtime, reverse=True)[:10]:
        sm = d / "SKILL.md"
        desc = ""
        try:
            text = sm.read_text()
            if text.startswith("---"):
                for ln in text.split("---", 2)[1].splitlines():
                    if ln.startswith("description:"):
                        desc = ln.split(":",1)[1].strip().strip('"')
                        break
        except Exception:
            pass
        print(f"  {d.name:<40} {desc[:55]}")

    conn.close()


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    AUTOSKILL_DIR.mkdir(parents=True, exist_ok=True)
    args = sys.argv[1:]

    if not args or args[0] == "--capture":
        cmd_capture()
    elif args[0] == "--extract" and len(args) > 1:
        cmd_extract(args[1])
    elif args[0] == "--import":
        cmd_import()
    elif args[0] == "--extract-all":
        cmd_extract_all()
    elif args[0] == "--search":
        query = args[1] if len(args) > 1 else ""
        cmd_search(query)
    elif args[0] == "--refine":
        cmd_refine()
    elif args[0] == "--status":
        cmd_status()
    elif args[0] == "--sync-repo":
        cmd_sync_repo()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
