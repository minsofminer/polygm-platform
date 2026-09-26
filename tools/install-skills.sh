#!/usr/bin/env bash
# Install the vendored skills into the workspace's skills sections, idempotently.
#
# Why this script exists rather than a one-off `ln -s` session: this workspace has been reset more than half a
# dozen times, and each reset takes `/home/user/skills`, `~/.claude/skills` and `~/.agents/skills` with it — the
# vendored copies live in the repo (and therefore survive, because they are committed), so the mirrors are
# reproducible in one command instead of being an archaeology exercise. `skills/VENDOR.json` is the provenance;
# this is the projection of it onto the three places a skill runner looks.
#
# Usage: tools/install-skills.sh            (dry run: says what it would link)
#        tools/install-skills.sh --apply
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

TARGETS=("$HOME/skills" "$HOME/.claude/skills" "$HOME/.agents/skills")
count=0

for target in "${TARGETS[@]}"; do
  echo "+ $target"
  [ "$APPLY" = "1" ] && mkdir -p "$target"
  # One entry per skill, named for the skill directory, pointing at the vendored source. A symlink rather than a
  # copy on purpose: a copy is a second version of a document that will be edited in one place only.
  while IFS= read -r skill; do
    name="$(basename "$(dirname "$skill")")"
    rel="${skill#"$ROOT"/}"
    [ "$APPLY" = "1" ] && ln -sfn "$ROOT/$rel" "$target/$name"
    count=$((count + 1))
  done < <(find "$ROOT/skills" -name SKILL.md -not -path "*/node_modules/*" | sort)
done

echo "skills: $count per target across ${#TARGETS[@]} targets$([ "$APPLY" = "1" ] && echo "" || echo " (dry run — pass --apply)")"
