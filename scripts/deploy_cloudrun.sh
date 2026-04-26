#!/usr/bin/env bash
# Deploy airhost-mcp to Cloud Run. Set env in your shell or .env and run.
#
#   PROJECT_ID=my-proj REGION=asia-northeast1 ./scripts/deploy_cloudrun.sh
#
# Prereqs: gcloud auth login, gcloud config set project, billing enabled,
# and a GCS bucket created for session storage.

set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID env var required}"
REGION="${REGION:-asia-northeast1}"
SERVICE="${SERVICE:-airhost-mcp}"
SESSION_BUCKET="${SESSION_BUCKET:?SESSION_BUCKET env var required (existing GCS bucket name)}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-}"

echo "Building image with Cloud Build..."
gcloud builds submit --tag "gcr.io/${PROJECT_ID}/${SERVICE}:latest" --project "${PROJECT_ID}"

# Playwright + Chromium needs significantly more memory and CPU than a plain
# Python service. 2Gi / 2 vCPU is a safe starting point; tune down later.
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
  --set-env-vars "SESSION_STORE=gcs,SESSION_GCS_BUCKET=${SESSION_BUCKET},BROWSER_HEADLESS=true"
)

if [[ -n "${SERVICE_ACCOUNT}" ]]; then
  DEPLOY_ARGS+=(--service-account "${SERVICE_ACCOUNT}")
fi

echo "Deploying to Cloud Run..."
gcloud run deploy "${DEPLOY_ARGS[@]}"

echo
echo "Done. Set the rest of the env (MCP_BEARER_TOKENS, AIRHOST_*, MFA_*, GMAIL_*)"
echo "via 'gcloud run services update ${SERVICE} --region ${REGION} --update-env-vars ...'"
echo "or use Secret Manager via --update-secrets."
