#!/bin/bash
# Run from Git Bash INSIDE this extracted omixa-backend 
#   bash update-repo.sh ~/Desktop/omixa-backend
# Copies thee files over your repo, commits and pushes.....
set -e
TARGET="${1:-$HOME/Desktop/omixa-backend}"
[ -d "$TARGET/.git" ] || { echo "No git repo at $TARGET (pass your repo path as the argument)"; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"
[ "$SRC" = "$(cd "$TARGET" && pwd)" ] && { echo "Run this from the EXTRACTED zip folder, not your repo."; exit 1; }
cp -r "$SRC"/. "$TARGET"/
rm -f "$TARGET/update-repo.sh"
cd "$TARGET"
git add -A
git status --short | head -15
git commit -m "Omixa v6: responsive pass, Google login fix, Pro upsell" || { echo "Still nothing to commit - files did not change."; exit 1; }
git push origin main
echo "PUSHED. Check Railway > Deployments."
