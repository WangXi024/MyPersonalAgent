#!/bin/sh
set -eu
. "$(dirname "$0")/common.sh"
case "${1:-}" in
  '') ;;
  --live-model)
    echo 'Paid probe of current learning-agent alias: at most 4 requests, 512 output tokens per request, 6000 total tokens.'
    echo 'Uses the configured restricted gateway key and supplier budget; monetary cost depends on your model prices.'
    compose run --rm --no-deps api python -m app.provider_probe --live-model
    exit $? ;;
  *) die 'Usage: ./scripts/smoke-test.sh [--live-model]' ;;
esac
compose exec -T api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=10); print('API and database: ready')"
compose exec -T api python -m app.smoke
compose exec -T litellm python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4000/health/readiness', timeout=10); print('LiteLLM: ready (provider credentials not tested)')"
compose exec -T hindsight python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8888/health', timeout=10); print('Hindsight: ready (Chinese quality not tested)')"
compose exec -T web wget -q --spider http://127.0.0.1/health/live
compose exec -T web test -s /usr/share/nginx/html/index.html
compose exec -T api python -c "import os; print('Open: ' + os.environ['APP_PUBLIC_URL'])"
echo 'Infrastructure checks passed. No paid model probe was performed.'
