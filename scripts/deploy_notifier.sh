#!/usr/bin/env bash
# Deploy airhost-notifier as a Cloud Run Job + Cloud Scheduler trigger.
#
#   set -a; source .env; set +a
#   ./scripts/deploy_notifier.sh
#
# Prereqs (one-time):
#   - gcloud auth login + gcloud config set project
#   - billing enabled
#   - 既存 GCS バケット（SESSION_GCS_BUCKET と同じでOK）
#   - Secret Manager secrets:
#       LINE_CHANNEL_TOKEN   : LINE Messaging API チャネルアクセストークン
#       MCP_ACCESS_TOKEN     : MCPサーバーの長期アクセストークン
#
# notifier は起動時に MCP_CLIENT_ID + MCP_CLIENT_SECRET で OAuth フロー（Authorization Code + PKCE）を
# 実行してアクセストークンを取得する。oauth_smoke.py と同じ実装。

set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID env var required}"
REGION="${REGION:-asia-northeast1}"
JOB="${JOB:-airhost-notifier}"
GCS_BUCKET="${SESSION_GCS_BUCKET:?SESSION_GCS_BUCKET env var required}"
MCP_PUBLIC_URL="${MCP_PUBLIC_URL:?MCP_PUBLIC_URL env var required}"
MCP_CLIENT_ID="${MCP_CLIENT_ID:-airhost-mcp}"
LISTING_IDS="${LISTING_IDS:?LISTING_IDS env var required (comma-separated)}"
LINE_USER_IDS="${LINE_USER_IDS:?LINE_USER_IDS env var required (comma-separated)}"
REMINDER_SCHEDULE="${NOTIFIER_REMINDER_SCHEDULE:-0 9 * * *}"          # 当日未完了への催促あり
NORMAL_SCHEDULE="${NOTIFIER_NORMAL_SCHEDULE:-0 12,15,18,21 * * *}"     # 完了通知のみ（催促なし）
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-}"
LOOKAHEAD_DAYS="${LOOKAHEAD_DAYS:-60}"
GCS_NOTIFIER_PREFIX="${GCS_NOTIFIER_PREFIX:-airhost-notifier/notified/}"
IMAGE="gcr.io/${PROJECT_ID}/${JOB}:latest"

echo "=== Building image with Cloud Build ==="
TMPCONFIG=$(mktemp /tmp/cloudbuild_XXXXXX.yaml)
cat > "${TMPCONFIG}" <<EOF
steps:
- name: 'gcr.io/cloud-builders/docker'
  args: ['build', '-f', 'notifier/Dockerfile', '-t', '${IMAGE}', '.']
images: ['${IMAGE}']
EOF
gcloud builds submit . \
  --config "${TMPCONFIG}" \
  --project "${PROJECT_ID}"
rm -f "${TMPCONFIG}"

echo "=== Creating / updating Cloud Run Job ==="

TMPENV=$(mktemp /tmp/envvars_XXXXXX.yaml)
cat > "${TMPENV}" <<ENVEOF
MCP_PUBLIC_URL: ${MCP_PUBLIC_URL}
MCP_CLIENT_ID: ${MCP_CLIENT_ID}
LISTING_IDS: ${LISTING_IDS}
LINE_USER_IDS: ${LINE_USER_IDS}
GCS_BUCKET: ${GCS_BUCKET}
GCS_NOTIFIER_PREFIX: ${GCS_NOTIFIER_PREFIX}
LOOKAHEAD_DAYS: "${LOOKAHEAD_DAYS}"
LOG_LEVEL: ${LOG_LEVEL:-INFO}
ENVEOF

SECRETS="LINE_CHANNEL_TOKEN=LINE_CHANNEL_TOKEN:latest"
SECRETS+=",MCP_CLIENT_SECRET=MCP_CLIENT_SECRET:latest"

JOB_ARGS=(
  "${JOB}"
  --image "${IMAGE}"
  --region "${REGION}"
  --project "${PROJECT_ID}"
  --max-retries 1
  --task-timeout 600
  --env-vars-file "${TMPENV}"
  --set-secrets "${SECRETS}"
)

if [[ -n "${SERVICE_ACCOUNT}" ]]; then
  JOB_ARGS+=(--service-account "${SERVICE_ACCOUNT}")
fi

if gcloud run jobs describe "${JOB}" --region "${REGION}" --project "${PROJECT_ID}" &>/dev/null; then
  gcloud run jobs update "${JOB_ARGS[@]}"
else
  gcloud run jobs create "${JOB_ARGS[@]}"
fi
rm -f "${TMPENV}"

echo "=== Creating / updating Cloud Scheduler ==="

RUN_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB}:run"
OAUTH_SA="${SERVICE_ACCOUNT:-$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')-compute@developer.gserviceaccount.com}"

# Cloud Run Jobs の RunJobRequest.overrides で実行ごとに REMINDER_RUN を上書きする
REMINDER_BODY='{"overrides":{"containerOverrides":[{"env":[{"name":"REMINDER_RUN","value":"true"}]}]}}'
NORMAL_BODY='{"overrides":{"containerOverrides":[{"env":[{"name":"REMINDER_RUN","value":"false"}]}]}}'

create_or_update_scheduler() {
  local name="$1" schedule="$2" body="$3"
  local args=(
    "${name}"
    --schedule "${schedule}"
    --time-zone "Asia/Tokyo"
    --uri "${RUN_URI}"
    --http-method POST
    --headers "Content-Type=application/json"
    --message-body "${body}"
    --oauth-service-account-email "${OAUTH_SA}"
    --location "${REGION}"
    --project "${PROJECT_ID}"
  )
  if gcloud scheduler jobs describe "${name}" --location "${REGION}" --project "${PROJECT_ID}" &>/dev/null; then
    gcloud scheduler jobs update http "${args[@]}"
  else
    gcloud scheduler jobs create http "${args[@]}"
  fi
}

create_or_update_scheduler "${JOB}-trigger-reminder" "${REMINDER_SCHEDULE}" "${REMINDER_BODY}"
create_or_update_scheduler "${JOB}-trigger" "${NORMAL_SCHEDULE}" "${NORMAL_BODY}"

echo
echo "=== Done ==="
echo "Job:       ${JOB}"
echo "Schedule:  ${SCHEDULE} (Asia/Tokyo)"
echo
echo "手動実行:"
echo "  gcloud run jobs execute ${JOB} --region ${REGION} --project ${PROJECT_ID}"
