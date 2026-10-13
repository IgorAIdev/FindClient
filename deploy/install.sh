#!/usr/bin/env bash
# Install or update FindClient on a server. Safe to run repeatedly; touches only APP_DIR and,
# when INSTALL_CRON=1, one marked crontab line of the current user. Other projects are not changed.
#   APP_DIR       where the code lives             (default: $HOME/FindClient)
#   REPO_URL      git URL to clone/fetch           (default: the public GitHub repo)
#   BRANCH        branch to deploy                 (default: main)
#   INSTALL_CRON  1 = add a weekly cron job        (default: 0)
#   PRINT_CRON    1 = only print the cron line and exit
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/FindClient}"
REPO_URL="${REPO_URL:-https://github.com/IgorAIdev/FindClient.git}"
BRANCH="${BRANCH:-main}"
MARK="# findclient-managed"
CRON_LINE="0 8 * * 1 cd \"$APP_DIR\" && . ./.env && /usr/bin/flock -n /tmp/findclient.lock sh -c 'python3 findclient.py prom --config campaign.json --limit 20 && python3 findclient.py audit --limit 30 && python3 findclient.py seo campaign.json --limit 20; python3 findclient.py report report.html' >> data/run.log 2>&1 $MARK"

if [ "${PRINT_CRON:-0}" = 1 ]; then echo "$CRON_LINE"; exit 0; fi

command -v git >/dev/null || { echo "git is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "Python 3.10+ is required" >&2; exit 1; }

if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
  git -C "$APP_DIR" checkout --quiet "$BRANCH"
  git -C "$APP_DIR" reset --quiet --hard "origin/$BRANCH"   # data/ and .env are untracked and stay
else
  git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

mkdir -p "$APP_DIR/data"
if [ ! -f "$APP_DIR/.env" ]; then
  (umask 077; printf '%s\n' '# Brave Search API key (never commit this file)' 'export BRAVE_SEARCH_API_KEY=' > "$APP_DIR/.env")
  echo "Created $APP_DIR/.env: put BRAVE_SEARCH_API_KEY there."
fi

(cd "$APP_DIR" && python3 -m unittest discover -s tests 2>&1 | tail -3)

if [ "${INSTALL_CRON:-0}" = 1 ]; then
  command -v crontab >/dev/null || { echo "crontab not found" >&2; exit 1; }
  { crontab -l 2>/dev/null | grep -v -F "$MARK" || true; echo "$CRON_LINE"; } | crontab -
  echo "Cron installed (Mondays 08:00 server time)."
fi
echo "FindClient deployed to $APP_DIR at $(git -C "$APP_DIR" rev-parse --short HEAD)"
