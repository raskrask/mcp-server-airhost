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
