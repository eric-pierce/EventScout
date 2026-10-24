#!/bin/sh
# Create ./secrets with one file per sensitive setting (an empty file means "not used").
#   scripts/init-secrets.sh             create any missing files (existing ones are left alone)
#   scripts/init-secrets.sh --from-env  also move values that are currently in .env into the files
#                                       and blank them in .env
# Values are never printed.
set -eu
cd "$(dirname "$0")/.."
umask 077
mkdir -p secrets
chmod 700 secrets

for pair in anthropic_api_key:ANTHROPIC_API_KEY smtp_password:SMTP_PASSWORD web_password:WEB_PASSWORD \
            feed_token:EVENTSCOUT_FEED_TOKEN ticketmaster_api_key:TICKETMASTER_API_KEY; do
  file="secrets/${pair%%:*}"
  var="${pair#*:}"
  [ -e "$file" ] || : > "$file"
  if [ "${1:-}" = "--from-env" ] && [ -f .env ]; then
    value=$(sed -n "s/^${var}=//p" .env | tail -n 1)
    value=${value#\"}; value=${value%\"}; value=${value#\'}; value=${value%\'}
    if [ -n "$value" ]; then
      if [ -s "$file" ]; then
        echo "kept $file (already has a value); remove ${var} from .env yourself if it is stale"
      else
        printf '%s' "$value" > "$file"
        sed "s/^${var}=.*/${var}=/" .env > .env.tmp && cat .env.tmp > .env && rm -f .env.tmp
        echo "moved ${var} from .env to $file"
      fi
    fi
  fi
  chmod 600 "$file"
done

if [ "$(uname -s)" = "Linux" ] && [ "$(id -u)" != "1000" ]; then
  echo "note: the container runs as uid 1000 and must be able to read these files:"
  echo "      sudo chown 1000 secrets/*"
fi
echo "secrets/ ready. Put each value in its file, e.g.:  printf '%s' 'sk-ant-...' > secrets/anthropic_api_key"
