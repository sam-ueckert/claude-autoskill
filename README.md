# claude-autoskill

Automatic skill extraction for [Claude Code](https://claude.ai/code). Watches your sessions via hooks, archives every conversation turn to SQLite, and uses Claude to distill reusable `SKILL.md` files — the kind of non-obvious, multi-step procedures worth teaching future sessions.

Based on the [MUSE-AutoSkill](https://arxiv.org/abs/2605.27366) lifecycle: **creation → memory → management → evaluation → refinement**.

---

## How it works

Four Claude Code hooks fire `autoskill.py --capture` on lifecycle events. The script:

1. Archives each turn (user + assistant) to `~/.claude/autoskill/data/archive.db` (SQLite)
2. Checks whether enough new turns have accumulated since the last extraction
3. If so, calls `claude -p` with the archived turns and a list of already-installed skills
4. Parses the JSON response and writes any new skills to `~/.claude/skills/as-<name>/SKILL.md`

Skills are immediately available as `/as-<name>` slash commands in any Claude Code session.

---

## Installation

### 1. Clone and copy files

```bash
git clone https://github.com/wwt/claude-autoskill.git
mkdir -p ~/.claude/autoskill ~/.claude/skills/autoskill
cp claude-autoskill/autoskill.py ~/.claude/autoskill/
cp claude-autoskill/config.json  ~/.claude/autoskill/
cp claude-autoskill/SKILL.md     ~/.claude/skills/autoskill/
```

### 2. Wire up the hooks

Add the following to `~/.claude/settings.json` (merge into any existing `hooks` block):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/autoskill/autoskill.py --capture",
            "async": true
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/autoskill/autoskill.py --capture",
            "async": true
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/autoskill/autoskill.py --capture",
            "async": true
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/autoskill/autoskill.py --capture",
            "async": true
          }
        ]
      }
    ]
  }
}
```

All hooks run `async: true` so they never block Claude Code.

### 3. Verify

```bash
python3 ~/.claude/autoskill/autoskill.py --status
```

Expected output includes `Sessions archived`, `Skills installed`, and recent autoskills. If the DB is empty, the hooks haven't fired yet — start a new Claude Code session and run one prompt to seed the archive.

### 4. (Optional) Auto-update

Keep a machine's deployed engine current with this repo automatically, instead of re-running the `cp` steps by hand. Requires a local checkout at `~/repos/claude-autoskill`:

```bash
git clone git@github.com:wwt/claude-autoskill.git ~/repos/claude-autoskill
cp ~/repos/claude-autoskill/selfupdate.sh ~/.claude/autoskill/
chmod +x ~/.claude/autoskill/selfupdate.sh
```

`selfupdate.sh` runs `git pull --rebase --autostash` on the checkout, then deploys `autoskill.py`, `SKILL.md`, **and itself** to `~/.claude/...` whenever they differ from the checkout (idempotent; atomic `cp`+`mv`, so it can safely overwrite itself while running). It **never** copies `config.json` — that file is machine-local and may hold a personal `repo_sync_dir`. All output is routed to `autoskill.log` so the hook can't inject it into a session transcript.

Wire it to run on every session start with a `SessionStart` hook:

```json
"SessionStart": [
  {
    "matcher": "",
    "hooks": [
      { "type": "command", "command": "bash ~/.claude/autoskill/selfupdate.sh >> ~/.claude/autoskill/autoskill.log 2>&1", "async": true }
    ]
  }
]
```

(Or schedule it from cron/launchd, e.g. daily.) **Workflow note:** with auto-update enabled the repo is canonical — edit `autoskill.py` in `~/repos/claude-autoskill` (or push to the repo), **not** in `~/.claude/autoskill/`, since the next run overwrites the deployed copy.

---

## The `/autoskill` skill

`SKILL.md` ships alongside `autoskill.py`. Installing it to `~/.claude/skills/autoskill/SKILL.md` registers `/autoskill` as a slash command in Claude Code. From any session you can then type:

```
/autoskill status
/autoskill import
/autoskill extract-all
/autoskill refine
```

The skill follows the standard Claude Code SKILL.md schema:

```
---
name: autoskill
description: <one-liner that triggers the skill>
---
# Title
## When to use
## Commands / Steps
## Configuration
```

---

## Hook behavior

Each hook event archives a different part of the conversation:

| Hook event | What fires it | What autoskill does |
|---|---|---|
| `UserPromptSubmit` | User sends a message | Archives the user turn to SQLite |
| `Stop` | Claude finishes responding | Archives the assistant turn; runs extraction if turn threshold reached |
| `SessionEnd` | Session closes | Final extraction pass for any unextracted turns |
| `PreCompact` | Context window auto-compacts | Extraction pass before context is summarized (prevents losing patterns) |

The `PreCompact` hook is particularly important: without it, a long session's early content — where the most reusable patterns often live — can be lost before extraction runs.

---

## Prospective skill creation

**Prospective** means skills extracted from sessions as they happen — the default, zero-config mode once hooks are wired.

Every time Claude stops responding (`Stop` event), the hook archives the turn and checks the delta since the last extraction. When the delta crosses `extract_every_turns` (default 8), extraction runs inline. Because the hook is already `async: true`, this never blocks you.

Extraction sends the archived turns plus a list of already-installed skills to `claude -p`. The LLM evaluates against a quality bar and returns a JSON array of skills. New skills land in `~/.claude/skills/as-<name>/SKILL.md` and are live immediately.

### Quality bar

The extractor explicitly includes:

- Non-obvious multi-step workflows (not "run git status")
- Recurring procedures for a class of problems
- Specific tool combinations, flag sets, or config tricks
- Domain-specific debugging or investigation methods
- Learned knowledge that saves time on repeat encounters

And explicitly excludes:

- Single-command answers
- One-off project-specific patches
- Common knowledge any developer already knows
- Near-duplicates of existing skills

---

## Retrospective skill creation

**Retrospective** means mining skills from sessions that existed before AutoSkill was installed, or sessions where hooks weren't running.

### Option A — interactive search (recommended)

```bash
python3 ~/.claude/autoskill/autoskill.py --search <query>
python3 ~/.claude/autoskill/autoskill.py --search          # no query = show all
```

Scans all transcripts and shows a numbered table filtered by the query (case-insensitive substring match against title and project directory):

```
Found 5 session(s) matching "docker":

  #   Title                                             Project                 Date        Turns  Status
  ─── ────────────────────────────────────────────────  ──────────────────────  ──────────  ─────  ────────────────
    1  Rancher Desktop containerd debug                 foreman                 2026-05-20     38  new
    2  Docker image transfer between machines           hackathon-tm7           2026-05-13     42  extracted (1 skills)
    3  Dockerfile SSH BuildKit setup                    openclaw-ops            2026-05-18     15  new

Select sessions to import+extract
  Numbers / ranges / 'all' / Enter to cancel  →  e.g.  1 3 5   2-4   all
> 1 3
```

**Selection syntax:**

| Input | Meaning |
|---|---|
| `1 3 5` | Sessions 1, 3, and 5 |
| `2-4` | Sessions 2 through 4 |
| `1 3-5 8` | Mixed |
| `all` | Every result |
| Enter | Cancel |

After selection, a confirmation prompt runs before any import or extraction happens. Sessions already in the archive skip the import step and go straight to extraction.

The **Status** column shows:
- `new` — not yet in the archive
- `imported` — in archive, no skills extracted yet
- `extracted (N skills)` — already processed

### Option B — bulk import everything

```bash
python3 ~/.claude/autoskill/autoskill.py --import
```

Reads every `.jsonl` file under `~/.claude/projects/`, parses user and assistant turns, and loads them into the archive. Sessions already in the archive are skipped. Prints a summary of imported sessions and turn counts.

### Option C — extract all unprocessed sessions

```bash
python3 ~/.claude/autoskill/autoskill.py --extract-all
```

Iterates every archived session that has more turns than its last recorded extraction and runs extraction on each. Processes highest-turn-count sessions first. Typically run after `--import`.

### Target a single session

```bash
python3 ~/.claude/autoskill/autoskill.py --extract <session-id>
```

The session ID is the UUID stem of the `.jsonl` filename in `~/.claude/projects/*/`.

---

## Refinement

The refinement pass (MUSE lifecycle step 5) reviews all installed autoskills as a batch:

```bash
python3 ~/.claude/autoskill/autoskill.py --refine
```

For each skill the LLM assesses:

- Is the description precise enough to match the right user request?
- Are the instructions clear and actionable?
- Should any two skills be merged?

Actions applied: `keep`, `update` (rewrites description + content), `merge` (consolidates two into one), `delete` (removes the skill directory). Run refinement periodically after accumulating a batch of new skills or after changing domains of work.

---

## Quick reference

| Command | What it does |
|---|---|
| `--status` | Archive stats + 10 most recent skills |
| `--search [query]` | Interactive table — pick sessions to import+extract |
| `--import` | Bulk-import all transcripts into the archive |
| `--extract-all` | Extract skills from every unprocessed archived session |
| `--extract <id>` | Extract from one specific session |
| `--refine` | Review and improve all autoskills as a batch |

```bash
python3 ~/.claude/autoskill/autoskill.py --status
```

Shows current config (model, thresholds, prefix), archive counts (sessions, turns, extractions, skills generated), and the 10 most recently written autoskills with descriptions.

---

## Configuration

Edit `~/.claude/autoskill/config.json`:

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Master on/off switch |
| `extract_every_turns` | `8` | Run extraction after this many new archived turns |
| `min_turns_extraction` | `4` | Skip extraction if session has fewer turns than this |
| `max_skills_per_run` | `3` | Cap on skills generated per extraction pass |
| `model` | `claude-sonnet-4-6` | Model used for extraction and refinement |
| `skill_prefix` | `as-` | Directory prefix for autoskill-generated skills |
| `max_content_chars` | `2000` | Truncation limit per turn before sending to LLM |
| `max_turns_context` | `40` | Maximum turns sent to the extraction LLM |
| `log_level` | `info` | `debug` \| `info` \| `error` |

---

## File layout

```
~/.claude/autoskill/
├── autoskill.py       # main script (this repo)
├── config.json        # configuration (this repo; machine-local, not auto-updated)
├── selfupdate.sh      # optional auto-updater (this repo)
├── autoskill.log      # operation log (gitignored)
└── data/
    └── archive.db     # SQLite: turns + extraction history (gitignored)

~/.claude/skills/
├── autoskill/
│   └── SKILL.md       # /autoskill slash command (this repo)
└── as-<name>/
    └── SKILL.md       # auto-generated skills
```

---

## Troubleshooting

**No skills appearing after many sessions**
Check `~/.claude/autoskill/autoskill.log`. Set `log_level: "debug"` in `config.json` for verbose output. Confirm `claude -p` works from your shell — the extractor relies on Claude Code's auth environment being inherited by the hook subprocess.

**`claude -p` not found in hook subprocess**
The hook runs with a minimal environment. Add the Claude Code binary to a PATH that hook subprocesses inherit, or use the full path: `command: "/full/path/to/claude -p ..."`.

**Duplicate or low-quality skills**
Run `--refine`. Also consider lowering `max_skills_per_run` or raising `min_turns_extraction` to be more selective.

**Extraction running too frequently / not frequently enough**
Adjust `extract_every_turns`. Lower = more frequent (higher API cost). Higher = coarser but cheaper.

**JSON parse errors in log**
The LLM occasionally adds commentary before the JSON array. The script strips markdown fences but may miss other preamble. These sessions are logged as 0 skills and skipped — they don't corrupt the archive.
