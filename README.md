# airhost-mcp

Airhost 操作用の MCP サーバ。Claude（リモート MCP）から呼び出せる Streamable HTTP transport で公開し、Cloud Run にデプロイして使う想定。

> **現状はモック**。`AIRHOST_CLIENT=mock`（既定）で起動すると、決定論的なダミーデータを返す。Airhost は重い JS の管理コンソールなので、実環境連携は **Playwright（Chromium）でのブラウザ自動化**で行う。スケルトンは `src/airhost_mcp/airhost/browser_client.py` の `BrowserAirhostClient` にあり、ログイン＋MFAフローまで枠組みが入っている。

---

## 機能（MCP ツール）

| ツール名                       | 概要                                                                 |
| ------------------------------ | -------------------------------------------------------------------- |
| `list_listings`                | 管理対象リスティングの一覧                                           |
| `get_availability`             | 指定リスティング・指定日の空き状況                                   |
| `get_reservations_on`          | 指定リスティング・指定日の予約詳細                                   |
| `block_date`                   | 指定リスティング・指定日をブロック                                   |
| `update_reservation`           | 予約の任意フィールドをパッチ                                         |
| `list_reservations_in_range`   | 期間×（任意）リスティングの予約一覧（売上 / 稼働分析向け）           |

---

## アーキテクチャ

```
Claude (remote MCP client)
        │  HTTPS + Bearer
        ▼
FastAPI ──► /health
        └─► /mcp  (Streamable HTTP, MCP protocol)
                │
                └─► AirhostClient
                        ├─ MockAirhostClient        (default, deterministic)
                        └─ BrowserAirhostClient     (Playwright + Chromium, TBD)
                              │
                              ├─ SessionStore       (local | GCS) — stores Playwright storage_state
                              └─ MFAStrategy        (gmail | pubsub | manual)
```

差し替えポイント:

- **AirhostClient** — モック ↔ 実 HTTP の切替。env `AIRHOST_CLIENT`。
- **SessionStore** — ローカルファイル ↔ GCS。env `SESSION_STORE`。Cloud Run では `gcs`。
- **MFAStrategy** — Gmail ポーリング / Pub/Sub（将来） / 手動。env `MFA_STRATEGY`。

---

## 必要なもの

- Python 3.11+
- **Playwright + Chromium**（`AIRHOST_CLIENT=browser` を使う場合。`pip install` 後に `playwright install chromium` が必要）
- `gcloud` CLI（Cloud Run デプロイ時）
- Gmail API の OAuth client（`MFA_STRATEGY=gmail` を使う場合）
- GCS バケット（Cloud Run 上でセッション永続化する場合）

---

## ローカル実行

### 1. 依存インストール

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Chromium 本体をローカルにインストール（モック起動だけなら不要）
playwright install chromium
# Linux でブラウザ依存ライブラリも入れる場合:
# playwright install --with-deps chromium
```

### 2. `.env` を作成

```bash
cp .env.example .env
# 最低限編集: MCP_BEARER_TOKENS, AIRHOST_USERNAME, AIRHOST_PASSWORD
# モック起動なら AIRHOST_CLIENT=mock のまま、Gmail/MFA 系は空でも可
```

ベアラトークンの生成例:

```bash
openssl rand -hex 32
```

### 3. 起動

```bash
# どちらでも可
airhost-mcp
# or
uvicorn airhost_mcp.server:app --host 0.0.0.0 --port 8080 --reload
```

### 4. 動作確認

```bash
curl -s http://localhost:8080/health
# {"status":"ok"}
# (path is /health, not /healthz — Cloud Run reserves /healthz at the frontend layer)

# 認証なしは 401
curl -i http://localhost:8080/mcp/

# 認証あり（実際の MCP 通信は Claude 側のクライアントを使うのが楽）
curl -i -H "Authorization: Bearer YOUR_TOKEN" http://localhost:8080/mcp/
```

### 5. テスト

```bash
pytest
```

---

## Claude（リモート MCP）に登録

Claude のリモート MCP 設定で、URL とベアラトークンを指定する:

- URL: `https://<your-cloud-run-host>/mcp` （ローカルなら `http://localhost:8080/mcp`）
- 認証ヘッダ: `Authorization: Bearer <MCP_BEARER_TOKENS の1つ>`

> 利用者は2名のみ想定。`MCP_BEARER_TOKENS` にカンマ区切りでトークンを2つ並べ、各人に1つずつ配布する。

---

## Cloud Run へのデプロイ

### 0. 事前準備（1 回だけ）

```bash
# プロジェクトと API
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  secretmanager.googleapis.com storage.googleapis.com

# セッション保存用 GCS バケット
gsutil mb -l asia-northeast1 gs://airhost-mcp-sessions-YOURUNIQ

# サービスアカウント（Cloud Run 実行用）
gcloud iam service-accounts create airhost-mcp-runner \
  --display-name "Airhost MCP runner"

# バケットへの読み書き権限
gsutil iam ch \
  serviceAccount:airhost-mcp-runner@YOUR_PROJECT_ID.iam.gserviceaccount.com:objectAdmin \
  gs://airhost-mcp-sessions-YOURUNIQ
```

### 1. Secret Manager に機密値を入れる（推奨）

```bash
echo -n "token1,token2" | gcloud secrets create MCP_BEARER_TOKENS --data-file=-
echo -n "your-airhost-password" | gcloud secrets create AIRHOST_PASSWORD --data-file=-

# Gmail の token.json（事前にローカルで一度ログイン同意して生成）
gcloud secrets create GMAIL_TOKEN_JSON --data-file=./gmail_token.json

gcloud secrets add-iam-policy-binding MCP_BEARER_TOKENS \
  --member=serviceAccount:airhost-mcp-runner@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  --role=roles/secretmanager.secretAccessor
# 他の secret にも同様に
```

### 2. デプロイ

最短:

```bash
PROJECT_ID=YOUR_PROJECT_ID \
REGION=asia-northeast1 \
SESSION_BUCKET=airhost-mcp-sessions-YOURUNIQ \
SERVICE_ACCOUNT=airhost-mcp-runner@YOUR_PROJECT_ID.iam.gserviceaccount.com \
./scripts/deploy_cloudrun.sh
```

このスクリプトはイメージビルド + 初回デプロイまで。Bearer token などの機密値は次のコマンドで上書きする:

```bash
gcloud run services update airhost-mcp \
  --region asia-northeast1 \
  --update-secrets MCP_BEARER_TOKENS=MCP_BEARER_TOKENS:latest \
  --update-secrets AIRHOST_PASSWORD=AIRHOST_PASSWORD:latest \
  --update-env-vars AIRHOST_USERNAME=you@example.com \
  --update-env-vars AIRHOST_CLIENT=http \
  --update-env-vars MFA_STRATEGY=gmail \
  --update-env-vars MFA_SENDER=no-reply@airhost.co
```

`--allow-unauthenticated` でデプロイしているのは、Claude 側のリモート MCP が GCP IAM を持たないため。**Bearer token があれば素通り、無ければ 401** という構成で守る。

### 3. 動作確認

```bash
URL=$(gcloud run services describe airhost-mcp --region asia-northeast1 --format='value(status.url)')
curl -s "$URL/health"
curl -i -H "Authorization: Bearer YOUR_TOKEN" "$URL/mcp/"
```

---

## MFA 戦略の使い分け

| 戦略     | env                         | 用途                                                                                      |
| -------- | --------------------------- | ----------------------------------------------------------------------------------------- |
| `gmail`  | `MFA_STRATEGY=gmail`        | 現在の既定。Gmail API を直接ポーリングして MFA メールから 6 桁コードを抽出。              |
| `pubsub` | `MFA_STRATEGY=pubsub`       | **未実装の枠**。将来「メール → Zapier or Gmail forwarder → Pub/Sub push」を組んだら有効化。 |
| `manual` | `MFA_STRATEGY=manual`       | デバッグ用。stdin からコードを入力。Cloud Run では使えない。                              |

戦略追加は `src/airhost_mcp/mfa/` に新ファイルを置き、`factory.py` に分岐を 1 行足すだけ。

---

## セッション永続化

Cloud Run はインスタンスが頻繁に落ちる前提なので、Airhost のログインセッションを `SessionStore` に書き出して使い回す。Playwright クライアントは `BrowserContext.storage_state()` の戻り値（cookies + 各 origin の localStorage）を JSON 化して保存する。

- `SESSION_STORE=local`（既定） — `./.sessions/<user>.json`
- `SESSION_STORE=gcs` — `gs://$SESSION_GCS_BUCKET/$SESSION_GCS_PREFIX<user>.json`

`SESSION_TTL_SECONDS` を超えたセッションは自動で再ログイン（パスワード + メール MFA）に流れる。

---

## ディレクトリ構成

```
mcp-server-airhost/
├── src/airhost_mcp/
│   ├── server.py            # FastAPI + FastMCP マウント + Bearer ミドルウェア
│   ├── tools.py             # 6 つの MCP ツール定義
│   ├── auth.py              # Bearer 検証
│   ├── config.py            # pydantic-settings
│   ├── airhost/             # AirhostClient + Mock + Browser(Playwright, TBD)
│   ├── mfa/                 # MFAStrategy (gmail / pubsub / manual)
│   └── session/             # SessionStore (local / gcs)
├── scripts/deploy_cloudrun.sh
├── tests/
├── Dockerfile
├── pyproject.toml
└── .env.example
```

---

## 実装するときの注意

1. `.env` と `gmail_credentials.json` / `gmail_token.json` は **絶対に commit しない**（`.gitignore` 済み）。
2. `BrowserAirhostClient` が動くまで本番 `AIRHOST_CLIENT=browser` には切り替えない。モックのまま Claude に接続して動作確認するのが先。
3. ベアラトークンは長く（32 バイト hex 推奨）、利用者ごとに別の値を発行する（誰のアクセスかログで識別したい場合に効く）。
4. Cloud Run + Playwright は **メモリ ≥ 1Gi、min-instances=1** にしないとコールドスタートで Chromium 起動に数秒かかる。常時呼ぶならコスト面で min-instances=1 が現実的。
5. `Dockerfile` の `mcr.microsoft.com/playwright/python:vX.Y.Z-jammy` のタグは `pyproject.toml` の `playwright>=` バージョンと合わせる（バージョンずれは起動時に警告 → 失敗の元）。
