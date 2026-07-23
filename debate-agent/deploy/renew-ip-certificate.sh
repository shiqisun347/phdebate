#!/usr/bin/env bash
set -euo pipefail
ROOT="${DEBATE_AGENT_ROOT:-/opt/debate-agent}"
docker compose -f "$ROOT/docker-compose.yml" run --rm certbot certonly \
  --non-interactive --agree-tos --register-unsafely-without-email \
  --preferred-profile shortlived --webroot --webroot-path /var/www/certbot \
  --ip-address 117.50.218.251 --cert-name 117.50.218.251 --keep-until-expiring
docker compose -f "$ROOT/docker-compose.yml" exec -T nginx nginx -s reload
