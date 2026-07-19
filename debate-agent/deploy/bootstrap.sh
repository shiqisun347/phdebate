#!/usr/bin/env bash
set -euo pipefail

ROOT="${DEBATE_AGENT_ROOT:-/opt/debate-agent}"
IP_ADDRESS="${DEBATE_AGENT_IP:-117.50.218.251}"
COMPOSE=(docker compose -f "$ROOT/docker-compose.yml")

if [[ ! -f "$ROOT/.env" ]]; then
  echo "missing $ROOT/.env" >&2
  exit 1
fi

"${COMPOSE[@]}" up -d --build postgres redis api web

if ! "${COMPOSE[@]}" run --rm --entrypoint sh certbot -c "test -f /etc/letsencrypt/live/$IP_ADDRESS/fullchain.pem"; then
  email_args=(--register-unsafely-without-email)
  if [[ -n "${ACME_EMAIL:-}" ]]; then
    email_args=(--email "$ACME_EMAIL" --no-eff-email)
  fi
  "${COMPOSE[@]}" run --rm -p 80:80 certbot certonly \
    --standalone --non-interactive --agree-tos \
    "${email_args[@]}" \
    --preferred-profile shortlived \
    --ip-address "$IP_ADDRESS" \
    --cert-name "$IP_ADDRESS"
fi

"${COMPOSE[@]}" up -d nginx
"${COMPOSE[@]}" ps
