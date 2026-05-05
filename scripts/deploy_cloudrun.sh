#!/usr/bin/env bash
# Deploy airhost-mcp to Cloud Run. Set env in your shell or .env and run.
#
#   set -a; source .env; set +a
#   ./scripts/deploy_cloudrun.sh
#
# Prereqs (one-time):
#   - gcloud auth login + gcloud config set project
#   - billing enabled
#   - GCS bucket created for session storage
#   - Secret Manager secrets created:
#       MCP_CLIENT_SECRET, MCP_TOKEN_SECRET,
#       AIRHOST_USERNAME, AIRHOST_PASSWORD,
#       GMAIL_CREDENTIALS, GMAIL_TOKEN
#
# Generate secrets locally (end='' avoids trailing newline that breaks Secret Manager comparison):
#   python3 -c "import secrets; print(secrets.token_urlsafe(32), end='')"  # MCP_CLIENT_SECRET
#   python3 -c "import secrets; print(secrets.token_urlsafe(48), end='')"  # MCP_TOKEN_SECRET

set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID env var required}"
REGION="${REGION:-asia-northeast1}"
SERVICE="${SERVICE:-airhost-mcp}"
SESSION_GCS_BUCKET="${SESSION_GCS_BUCKET:?SESSION_GCS_BUCKET env var required (existing GCS bucket name)}"
MCP_CLIENT_ID="${MCP_CLIENT_ID:-airhost-mcp}"
MCP_ACCESS_TOKEN_TTL_DAYS="${MCP_ACCESS_TOKEN_TTL_DAYS:-365}"
MCP_PUBLIC_URL="${MCP_PUBLIC_URL:-}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-}"

echo "Building image with Cloud Build..."
gcloud builds submit --tag "gcr.io/${PROJECT_ID}/${SERVICE}:latest" --project "${PROJECT_ID}"

GMAIL_CREDS_PATH="/secrets/gmail-credentials/credentials.json"
GMAIL_TOKEN_PATH="/secrets/gmail-token/token.json"

ENV_VARS="SESSION_STORE=gcs,SESSION_GCS_BUCKET=${SESSION_GCS_BUCKET},BROWSER_HEADLESS=true"
ENV_VARS+=",MCP_CLIENT_ID=${MCP_CLIENT_ID},MCP_ACCESS_TOKEN_TTL_DAYS=${MCP_ACCESS_TOKEN_TTL_DAYS}"
ENV_VARS+=",AIRHOST_CLIENT=browser,MFA_STRATEGY=gmail"
ENV_VARS+=",GMAIL_CREDENTIALS_PATH=${GMAIL_CREDS_PATH},GMAIL_TOKEN_PATH=${GMAIL_TOKEN_PATH}"
if [[ -n "${MCP_PUBLIC_URL}" ]]; then
  ENV_VARS+=",MCP_PUBLIC_URL=${MCP_PUBLIC_URL}"
fi

# MCP_CLIENT_SECRET and MCP_TOKEN_SECRET are secrets; wired via Secret Manager.
SECRETS="MCP_CLIENT_SECRET=MCP_CLIENT_SECRET:latest"
SECRETS+=",MCP_TOKEN_SECRET=MCP_TOKEN_SECRET:latest"
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
  --min-instances 1
  --max-instances 1
  --concurrency 4
  --cpu 2
  --memory 2Gi
  --timeout 3600
  --set-env-vars "${ENV_VARS}"
  --set-secrets "${SECRETS}"
)

if [[ -n "${SERVICE_ACCOUNT}" ]]; then
  DEPLOY_ARGS+=(--service-account "${SERVICE_ACCOUNT}")
fi

echo "Deploying to Cloud Run..."
gcloud run deploy "${DEPLOY_ARGS[@]}"

echo
echo "Done."
echo
echo "Next: register MCP connector in Claude with:"
echo "  URL:           ${MCP_PUBLIC_URL:-<Cloud Run URL>}/mcp/"
echo "  Client ID:     ${MCP_CLIENT_ID}"
echo "  Client Secret: (value of MCP_CLIENT_SECRET secret)"
