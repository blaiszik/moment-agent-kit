#!/usr/bin/env bash
#
# install-skill.sh — install Moment's agent skills for local agents.
#
# Copies the Moment agent skills that sit next to this script (.claude/skills/ in
# the Moment repo, skills/ in the agent kit) into the global skill directories
# for Claude (~/.claude/skills) and Codex (~/.codex/skills) so they are
# available across all your projects.
#
# Skills installed: moment (context hub) and burn (the contribution loop).
#
# Usage:
#   ./install-skill.sh                  # install every Moment skill for every agent
#   ./install-skill.sh --claude         # Claude only
#   ./install-skill.sh --codex          # Codex only
#   ./install-skill.sh --skill moment   # just one skill (repeatable)
#   ./install-skill.sh --dry-run        # show what would happen, change nothing
#
# Override targets with env vars:
#   CLAUDE_SKILLS_DIR=/path ./install-skill.sh
#   CODEX_SKILLS_DIR=/path  ./install-skill.sh

set -euo pipefail

# Resolve the directory this script lives in (the repo's .claude/skills), so it works from any cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CLAUDE_SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
CODEX_SKILLS_DIR="${CODEX_SKILLS_DIR:-$HOME/.codex/skills}"

DO_CLAUDE=1
DO_CODEX=1
DRY_RUN=0
SKILLS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --claude) DO_CLAUDE=1; DO_CODEX=0 ;;
    --codex)  DO_CODEX=1; DO_CLAUDE=0 ;;
    --skill)  shift; [ $# -gt 0 ] || { echo "error: --skill needs a name" >&2; exit 2; }; SKILLS+=("$1") ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# Default skill set: the Moment API skills.
if [ "${#SKILLS[@]}" -eq 0 ]; then
  SKILLS=(moment burn)
fi

install_one() {
  agent="$1"; base="$2"; name="$3"
  src="$SCRIPT_DIR/$name"
  dest="$base/$name"

  if [ ! -d "$src" ]; then
    echo "skip: no source for '$name' at $src" >&2
    return
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] would install $agent skill '$name' -> $dest"
    return
  fi
  mkdir -p "$base"
  rm -rf "$dest"          # replace any existing copy so removed files don't linger
  cp -R "$src" "$dest"
  echo "installed $agent skill '$name' -> $dest"
}

did_something=0
for name in "${SKILLS[@]}"; do
  [ "$DO_CLAUDE" -eq 1 ] && { install_one "Claude" "$CLAUDE_SKILLS_DIR" "$name"; did_something=1; }
  [ "$DO_CODEX"  -eq 1 ] && { install_one "Codex"  "$CODEX_SKILLS_DIR"  "$name"; did_something=1; }
done

if [ "$did_something" -eq 1 ] && [ "$DRY_RUN" -eq 0 ]; then
  echo "done. Start a new agent session to pick up the installed skill(s)."
fi
