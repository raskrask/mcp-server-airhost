#!/usr/bin/env bash
# Deploy airhost-mcp to Cloud Run. Set env in your shell or .env and run.
#
#   PROJECT_ID=my-proj REGION=asia-northeast1 \
#     SESSION_BUCKET=my-bucket \
#     AUTH0_DOMAIN=mot-cozy-space.jp.auth0.com \
#     AUTH0_AUDIENCE=https://airhost-mcp.example.com \
#     MCP_PUBLIC_URL=https://airhost-mcp-xxxx.asia-northeast1.run.app \
#     ./scripts/deploy_cloudrun.sh
#
# Prereqs (one-time):
#   - gcloud auth login + gcloud config set project
#   - billing enabled
#   - Auth0 tenant + API created, OIDC Dynamic Application Registration enabled
#   - GCS bucket created for session storage
#   - Secret Manager secret MCP_ALLOWED_EMAILS already exists. Create it:
#       printf 'alice@example.com,bob@example.com' \
#         | gcloud secrets create MCP_ALLOWED_EMAILS --data-file=-
#     and grant the runner SA roles/secretmanager.secretAccessor on it.
#
# Optional: Auth0 DCR app cleanup (prevents too_many_entities errors)
#   Set AUTH0_MGMT_CLIENT_ID + AUTH0_MGMT_CLIENT_SECRET to enable.
#   Create an M2M application in Auth0 dashboard with Management API scopes:
#     read:clients  delete:clients
#   AUTH0_MGMT_KEEP_APPS (default: 5) controls how many recent DCR apps to keep.

set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID env var required}"
REGION="${REGION:-asia-northeast1}"
SERVICE="${SERVICE:-airhost-mcp}"
SESSION_BUCKET="${SESSION_BUCKET:?SESSION_BUCKET env var required (existing GCS bucket name)}"
AUTH0_DOMAIN="${AUTH0_DOMAIN:?AUTH0_DOMAIN env var required (e.g. tenant.region.auth0.com)}"
AUTH0_AUDIENCE="${AUTH0_AUDIENCE:?AUTH0_AUDIENCE env var required (Auth0 API identifier)}"
AUTH0_ISSUER="${AUTH0_ISSUER:-}"
MCP_PUBLIC_URL="${MCP_PUBLIC_URL:-}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-}"
AUTH0_MGMT_CLIENT_ID="${AUTH0_MGMT_CLIENT_ID:-}"
AUTH0_MGMT_CLIENT_SECRET="${AUTH0_MGMT_CLIENT_SECRET:-}"
AUTH0_MGMT_KEEP_APPS="${AUTH0_MGMT_KEEP_APPS:-2}"

# ---------------------------------------------------------------------------
# cleanup_auth0_dcr_apps: delete stale DCR-registered OAuth clients from Auth0.
#
# Auth0 imposes a per-tenant limit on public clients (those registered via DCR
# with PKCE). MCP clients such as Claude Code register themselves dynamically
# on first connect; over time old registrations accumulate and trigger
# "too_many_entities" errors that block new clients from authenticating.
#
# Identifies DCR clients by: token_endpoint_auth_method=none, grant_type
# authorization_code, is_first_party=false.  Keeps the N most recent.
# ---------------------------------------------------------------------------
cleanup_auth0_dcr_apps() {
  echo "--- Auth0 DCR app cleanup ---"
  echo "Fetching Management API token..."

  local token_json
  token_json=$(curl -sf -X POST "https://${AUTH0_DOMAIN}/oauth/token" \
    -H "Content-Type: application/json" \
    -d "{
      \"client_id\": \"${AUTH0_MGMT_CLIENT_ID}\",
      \"client_secret\": \"${AUTH0_MGMT_CLIENT_SECRET}\",
      \"audience\": \"https://${AUTH0_DOMAIN}/api/v2/\",
      \"grant_type\": \"client_credentials\"
    }") || { echo "ERROR: Failed to obtain Auth0 Management API token." >&2; return 1; }

  local mgmt_token
  mgmt_token=$(echo "${token_json}" | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")

  echo "Listing Auth0 applications..."
  local clients_json
  clients_json=$(curl -sf \
    "https://${AUTH0_DOMAIN}/api/v2/clients?per_page=100&fields=client_id,name,app_type,token_endpoint_auth_method,is_first_party,created_at,grant_types" \
    -H "Authorization: Bearer ${mgmt_token}") || { echo "ERROR: Failed to list Auth0 clients." >&2; return 1; }

  # Find DCR-like clients and collect IDs to delete (oldest first, keep N most recent).
  local to_delete
  to_delete=$(echo "${clients_json}" | python3 -c "
import json, sys
clients = json.load(sys.stdin)
dcr = [
    c for c in clients
    if c.get('token_endpoint_auth_method') == 'none'
    and 'authorization_code' in c.get('grant_types', [])
    and not c.get('is_first_party', True)
]
dcr.sort(key=lambda c: c.get('created_at', ''), reverse=True)
keep = int('${AUTH0_MGMT_KEEP_APPS}')
for c in dcr[keep:]:
    print(c['client_id'] + ' ' + c.get('name', '(no name)'))
")

  if [[ -z "${to_delete}" ]]; then
    echo "No stale DCR clients to delete (keeping up to ${AUTH0_MGMT_KEEP_APPS} most recent)."
    return 0
  fi

  local count
  count=$(echo "${to_delete}" | wc -l | tr -d ' ')
  echo "Deleting ${count} stale DCR client(s) (keeping ${AUTH0_MGMT_KEEP_APPS} most recent)..."

  while IFS=' ' read -r client_id client_name; do
    [[ -z "${client_id}" ]] && continue
    local http_status
    http_status=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE \
      "https://${AUTH0_DOMAIN}/api/v2/clients/${client_id}" \
      -H "Authorization: Bearer ${mgmt_token}")
    if [[ "${http_status}" == "204" ]]; then
      echo "  Deleted: ${client_id} (${client_name})"
    else
      echo "  WARNING: DELETE ${client_id} returned HTTP ${http_status}" >&2
    fi
  done <<< "${to_delete}"

  echo "Auth0 cleanup complete."
}

# Run cleanup before build/deploy if management credentials are provided.
if [[ -n "${AUTH0_MGMT_CLIENT_ID}" && -n "${AUTH0_MGMT_CLIENT_SECRET}" ]]; then
  cleanup_auth0_dcr_apps
else
  echo "Skipping Auth0 DCR cleanup (AUTH0_MGMT_CLIENT_ID/SECRET not set)."
  echo "To enable, create an Auth0 M2M app with read:clients + delete:clients on the Management API."
fi

echo "Building image with Cloud Build..."
gcloud builds submit --tag "gcr.io/${PROJECT_ID}/${SERVICE}:latest" --project "${PROJECT_ID}"

# Playwright + Chromium needs significantly more memory and CPU than a plain
# Python service. 2Gi / 2 vCPU is a safe starting point; tune down later.
# Gmail credential files are mounted from Secret Manager as separate file paths.
# gcloud run only allows one secret per directory, so each gets its own dir.
GMAIL_CREDS_PATH="/secrets/gmail-credentials/credentials.json"
GMAIL_TOKEN_PATH="/secrets/gmail-token/token.json"

ENV_VARS="SESSION_STORE=gcs,SESSION_GCS_BUCKET=${SESSION_BUCKET},BROWSER_HEADLESS=true"
ENV_VARS+=",AUTH0_DOMAIN=${AUTH0_DOMAIN},AUTH0_AUDIENCE=${AUTH0_AUDIENCE}"
ENV_VARS+=",AIRHOST_CLIENT=browser,MFA_STRATEGY=gmail"
ENV_VARS+=",GMAIL_CREDENTIALS_PATH=${GMAIL_CREDS_PATH},GMAIL_TOKEN_PATH=${GMAIL_TOKEN_PATH}"
if [[ -n "${AUTH0_ISSUER}" ]]; then
  ENV_VARS+=",AUTH0_ISSUER=${AUTH0_ISSUER}"
fi
if [[ -n "${MCP_PUBLIC_URL}" ]]; then
  ENV_VARS+=",MCP_PUBLIC_URL=${MCP_PUBLIC_URL}"
fi

# Secrets wired as env vars (credentials) and file mounts (gmail JSON files).
SECRETS="MCP_ALLOWED_EMAILS=MCP_ALLOWED_EMAILS:latest"
SECRETS+=",AIRHOST_USERNAME=AIRHOST_USERNAME:latest"
SECRETS+=",AIRHOST_PASSWORD=AIRHOST_PASSWORD:latest"
SECRETS+=",${GMAIL_CREDS_PATH}=GMAIL_CREDENTIALS:latest"
SECRETS+=",${GMAIL_TOKEN_PATH}=GMAIL_TOKEN:latest"

DEPLOY_ARGS=(
  "${SERVICE}"
  --image "gcr.io/${PROJECT_ID}/${SERVICE}:latest"
  --region "${REGION}"
  --project "${PROJECT_ID}"
  --platform managed
  --allow-unauthenticated
  --port 8080
  --min-instances 0
  --max-instances 2
  --concurrency 4
  --cpu 2
  --memory 2Gi
  --timeout 300
  --set-env-vars "${ENV_VARS}"
  --set-secrets "${SECRETS}"
)

if [[ -n "${SERVICE_ACCOUNT}" ]]; then
  DEPLOY_ARGS+=(--service-account "${SERVICE_ACCOUNT}")
fi

echo "Deploying to Cloud Run..."
gcloud run deploy "${DEPLOY_ARGS[@]}"

echo
echo "Done. Set the rest of the env (AIRHOST_*, MFA_*, GMAIL_*) via:"
echo "  gcloud run services update ${SERVICE} --region ${REGION} --update-env-vars ..."
echo "or use Secret Manager via --update-secrets."
