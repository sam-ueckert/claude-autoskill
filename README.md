# claude-autoskill

Automatic skill extraction for [Claude Code](https://claude.ai/code). Watches your sessions via hooks, archives every conversation turn to SQLite, and uses Claude to distill reusable `SKILL.md` files — the kind of non-obvious, multi-step procedures worth teaching future sessions.

Based on the [MUSE-AutoSkill](https://arxiv.org/abs/2605.27366) lifecycle: **creation → memory → management → evaluation → refinement**.

---

## How it works

Claude Code hooks fire `autoskill.py --capture` on every user prompt, assistant stop, session end, and pre-compact event. The script:

1. Archives the turn to `~/.claude/autoskill/data/archive.db` (SQLite)
2. Checks whether the turn threshold has been reached (`extract_every_turns`, default 8)
3. If so, calls `claude -p` with the archived turns and a list of already-installed skills
4. Parses the JSON response and writes any new skills to `~/.claude/skills/as-<name>/SKILL.md`

Skills are immediately available as `/as-<name>` slash commands in any Claude Code session.

---

## Installation

### 1. Copy files

```bash
mkdir -p ~/.claude/autoskill
cp autoskill.py config.json ~/.claude/autoskill/
chmod +x ~/.claude/autoskill/autoskill.py
```

### 2. Wire up hooks in `~/.claude/settings.json`

```json
{
  "hooks": {
    "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "python3 ~/.claude/autoskill/autoskill.py --capture", "async": true}]}],
    "Stop":             [{"matcher": "", "hooks": [{"type": "command", "command": "python3 ~/.claude/autoskill/autoskill.py --capture", "async": true}]}],
    "SessionEnd":       [{"matcher": "", "hooks": [{"type": "command", "command": "python3 ~/.claude/autoskill/autoskill.py --capture", "async": true}]}],
    "PreCompact":       [{"matcher": "", "hooks": [{"type": "command", "command": "python3 ~/.claude/autoskill/autoskill.py --capture", "async": true}]}]
  }
}
```

All hooks run `async: true` so they never block Claude Code.

### 3. Verify

```bash
python3 ~/.claude/autoskill/autoskill.py --status
```

---

## Configuration

Edit `~/.claude/autoskill/config.json`:

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Master on/off switch |
| `extract_every_turns` | `8` | Run extraction after this many new archived turns |
| `min_turns_extraction` | `4` | Don't extract if session has fewer turns than this |
| `max_skills_per_run` | `3` | Cap on skills generated per extraction pass |
| `model` | `claude-sonnet-4-6` | Model used for extraction |
| `skill_prefix` | `as-` | Directory prefix for autoskill-generated skills |
| `max_content_chars` | `2000` | Truncation limit per turn before sending to LLM |
| `max_turns_context` | `40` | Maximum turns sent to the extraction LLM |
| `log_level` | `info` | `debug` \| `info` \| `error` |

---

## Prospective skill creation

**Prospective** means skills extracted from sessions as they happen — the default mode.

The hook fires on every turn. When the delta since the last extraction crosses `extract_every_turns`, the extractor runs inline (the hook is async, so this doesn't block you). Extraction also runs at `SessionEnd` and `PreCompact` to ensure nothing is lost at context boundaries.

The LLM evaluates the conversation against a strict quality bar:

- **Extract:** non-obvious multi-step workflows, recurring procedures, specific tool/flag combinations, domain debugging methods
- **Skip:** single commands, one-off patches, common knowledge, near-duplicates of existing skills

New skills land in `~/.claude/skills/as-<name>/SKILL.md` and are live immediately.

---

## Retrospective skill creation

**Retrospective** means mining skills from sessions that happened before AutoSkill was installed.

### Step 1 — import historical transcripts

```bash
python3 ~/.claude/autoskill/autoskill.py --import
```

Reads every `.jsonl` file under `~/.claude/projects/`, parses user and assistant turns, and loads them into the archive. Sessions already in the archive are skipped.

### Step 2 — extract from all unprocessed sessions

```bash
python3 ~/.claude/autoskill/autoskill.py --extract-all
```

Iterates every archived session that has unprocessed turns (more turns than the last extraction recorded) and runs extraction on each. Processes highest-turn-count sessions first.

### Extract a single session

If you want to target one specific session:

```bash
python3 ~/.claude/autoskill/autoskill.py --extract <session-id>
```

The session ID is the stem of the `.jsonl` filename (the UUID portion).

---

## Refinement

The refinement pass (MUSE lifecycle step 5) reviews all installed autoskills as a batch and suggests improvements:

```bash
python3 ~/.claude/autoskill/autoskill.py --refine
```

For each skill the LLM assesses:
- Is the description precise enough to match the right user request?
- Are the instructions clear and actionable?
- Should any two skills be merged?

Actions applied: `keep`, `update` (rewrites description + content), `merge` (consolidates into one), `delete` (removes the skill directory).

Run refinement periodically after accumulating a batch of new skills, or after changing domains of work.

---

## Status

```bash
python3 ~/.claude/autoskill/autoskill.py --status
```

Shows archive stats (sessions, turns, extractions, skills generated), current config, and the 10 most recently written autoskills.

---

## File layout

```
~/.claude/autoskill/
├── autoskill.py       # main script
├── config.json        # configuration
├── autoskill.log      # operation log
└── data/
    └── archive.db     # SQLite: turns + extraction history

~/.claude/skills/
└── as-<name>/
    └── SKILL.md       # generated skill (slash command)
```

The `data/` directory and log are gitignored in this repo — they are local to each machine.

---

## What makes a good skill

The extractor uses this bar internally; it helps to understand it when reviewing output:

- Captures a **non-obvious multi-step workflow** (not "run git status")
- Encodes a **recurring procedure** for a class of problems
- Documents a **specific tool combination**, flag set, or config trick
- Describes a **domain-specific debugging or investigation method**
- Represents **learned knowledge** that saves time on repeat encounters

Single-command answers, one-off patches, and common knowledge are explicitly excluded.

---

## Troubleshooting

**No skills appearing after many sessions**
Check `~/.claude/autoskill/autoskill.log`. Set `log_level: "debug"` in `config.json` for verbose output. Confirm `claude -p` works from your shell — the extractor relies on Claude Code's auth environment.

**Duplicate or low-quality skills**
Run `--refine`. Also consider lowering `max_skills_per_run` or raising `min_turns_extraction` to be more selective.

**Extraction running too frequently / not frequently enough**
Adjust `extract_every_turns` in `config.json`. Lower = more frequent (higher API cost). Higher = coarser but cheaper.
