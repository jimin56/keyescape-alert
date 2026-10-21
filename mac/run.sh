#!/bin/bash
# One pass from a local machine, sharing state.json with CI through git.
# Heavy sources are left to CI (SKIP_HEAVY=1) so they are not fetched twice.
cd "$(dirname "$0")/.." || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
mkdir .lock 2>/dev/null || exit 0
trap 'rmdir .lock' EXIT
git pull -q --rebase origin main 2>/dev/null || { git rebase --abort 2>/dev/null; git reset -q --hard origin/main; }
SKIP_HEAVY=1 python3 check.py
if ! git diff --quiet -- state.json; then
  git add state.json && git commit -qm state && {
    git push -q origin main 2>/dev/null || { git pull -q --rebase origin main && git push -q origin main; }
  }
fi
