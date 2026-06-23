#!/bin/bash
# selfupdate.sh — pull the canonical autoskill engine from claude-autoskill and
# deploy it locally. Code + skill doc are canonical; config.json is NOT copied
# (it holds this machine's personal repo_sync_dir). All output goes to the log
# so it can never be injected into a session transcript by the hook system.
set -u
REPO="$HOME/repos/claude-autoskill"
LOG="$HOME/.claude/autoskill/autoskill.log"
ts() { date +%Y-%m-%dT%H:%M:%S; }
[ -d "$REPO/.git" ] || { echo "[$(ts)] selfupdate: no checkout at $REPO" >>"$LOG"; exit 0; }

git -C "$REPO" pull --rebase --autostash >>"$LOG" 2>&1 || {
  git -C "$REPO" rebase --abort >/dev/null 2>&1
  echo "[$(ts)] selfupdate: pull failed, skipping" >>"$LOG"; exit 0
}

# Deploy whenever the checkout differs from the deployed copy — covers both
# remote pulls and local commits, and is idempotent. config.json is NEVER
# copied (it holds this machine's personal repo_sync_dir).
head=$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null)
deploy() {  # src dst — atomic (cp to .tmp + mv) so a running script can update itself safely
  [ -f "$1" ] || return 0
  cmp -s "$1" "$2" 2>/dev/null && return 0
  cp "$1" "$2.tmp" && mv "$2.tmp" "$2" && echo "[$(ts)] selfupdate: deployed $(basename "$2") (repo @ $head)" >>"$LOG"
}
deploy "$REPO/autoskill.py"  "$HOME/.claude/autoskill/autoskill.py"
deploy "$REPO/SKILL.md"      "$HOME/.claude/skills/autoskill/SKILL.md"
deploy "$REPO/selfupdate.sh" "$HOME/.claude/autoskill/selfupdate.sh"
chmod +x "$HOME/.claude/autoskill/selfupdate.sh" 2>/dev/null
exit 0
