# airhost-mcp

Airhost 操作用の MCP サーバ。Claude（リモート MCP）から呼び出せる Streamable HTTP transport で公開し、Cloud Run にデプロイして使う想定。

Airhost 管理コンソールは重い JS アプリのため、実環境連携は **Playwright（Chromium）でのブラウザ自動化**で行う。`AIRHOST_CLIENT=browser` で実際の Airhost アカウントと接続し、`AIRHOST_CLIENT=mock`（既定）は決定論的なダミーデータを返す。

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
| `get_guest_registration`       | 予約の宿泊者名簿（オンラインチェックイン）の入力完了状況・代表者の本人確認画像URL。**個人情報を含む** |
| `get_guest_id_photo`           | 代表者の本人確認画像本体を取得（vision で氏名照合用）。**個人情報を含む**     |

---

## アーキテクチャ

```
Claude (remote MCP client)
        │  HTTPS + self-issued JWT (OAuth 2.1 Bearer)
        ▼
FastAPI ──► /health
        ├─► /.well-known/oauth-protected-resource     (RFC 9728)
        ├─► /.well-known/oauth-authorization-server   (RFC 8414, self-hosted)
        ├─► /oauth/authorize                          (Authorization Code + PKCE)
        ├─► /oauth/token                              (token endpoint)
        ├─► /oidc/register                            (DCR proxy — returns fixed client_id)
        └─► /mcp  (Streamable HTTP, MCP protocol)
                │
                └─► AirhostClient
                        ├─ MockAirhostClient        (default, deterministic)
                        └─ BrowserAirhostClient     (Playwright + Chromium)
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
- ~~Auth0 テナント~~ 不要になりました（自前 OAuth に移行）

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
# 最低限編集: MCP_CLIENT_ID, MCP_CLIENT_SECRET, MCP_TOKEN_SECRET, AIRHOST_USERNAME, AIRHOST_PASSWORD
# モック起動なら AIRHOST_CLIENT=mock のまま、Gmail/MFA 系は空でも可
```

シークレット値の生成:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"  # MCP_CLIENT_SECRET 用
python3 -c "import secrets; print(secrets.token_urlsafe(48))"  # MCP_TOKEN_SECRET 用
```

素のスモークテストだけなら `DEV_DISABLE_AUTH=true` でミドルウェアそのものをスキップできる
（**Cloud Run 上では K_SERVICE が常にセットされているため、production では絶対に有効化されない**）。

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

# OAuth 2.1 discovery (公開, 認証不要)
curl -s http://localhost:8080/.well-known/oauth-protected-resource
curl -s http://localhost:8080/.well-known/oauth-authorization-server

# 認証なしは 401 + WWW-Authenticate ヘッダ
curl -i http://localhost:8080/mcp/
```


### 5. テスト

```bash
pytest
```

---

## 認証モデル（自前 OAuth 2.1）

Auth0 を使わず、このサーバ自身が **Authorization Server** と **Resource Server**
を兼ねる。Claude のコネクタに登録した `client_id` / `client_secret` を使って
Authorization Code + PKCE フローでトークンを発行する。ユーザログイン画面は不要
（`client_secret` を知っているコネクタだけが接続できる）。

サーバは毎リクエストで `MCP_TOKEN_SECRET` で署名した HMAC-SHA256 JWT を検証する。
検証失敗 → `401 + WWW-Authenticate: Bearer resource_metadata="..."`。

**アクセストークン有効期限は `MCP_ACCESS_TOKEN_TTL_DAYS`（デフォルト 365 日）。**
Claude のコネクタがリフレッシュトークンを正しく使わない問題を回避するため、
長期トークンを採用している。

### OAuth エンドポイント

| パス | 役割 |
| ---- | ---- |
| `GET  /.well-known/oauth-protected-resource`  | RFC 9728. `authorization_servers` にこのサーバ自身を返す |
| `GET  /.well-known/oauth-authorization-server` | RFC 8414. このサーバの `/oauth/authorize`, `/oauth/token` を返す |
| `GET  /oauth/authorize`                        | Authorization Code 発行（即時リダイレクト、ログイン画面なし） |
| `POST /oauth/token`                            | code → JWT access_token + refresh_token の交換、またはリフレッシュ |
| `POST /oidc/register`                          | DCR プロキシ。`MCP_CLIENT_ID` を固定で返す |

### 設定値（3 つのシークレット）

| 変数名 | 役割 | 保管場所 |
|--------|------|----------|
| `MCP_CLIENT_ID` | Claude コネクタに登録する ID（例: `airhost-mcp`） | 平文 env var |
| `MCP_CLIENT_SECRET` | Claude コネクタに登録するシークレット | **Secret Manager** |
| `MCP_TOKEN_SECRET` | JWT HMAC 署名キー | **Secret Manager** |

---

## Claude.ai (リモート MCP) に登録

Claude.ai の Custom Connector に以下を設定する:

- **URL**: `https://<cloud-run-host>/mcp/`
- **Client ID**: `MCP_CLIENT_ID` の値（例: `airhost-mcp`）
- **Client Secret**: `MCP_CLIENT_SECRET` の値

初回接続時に claude.ai が `/.well-known/oauth-protected-resource` を取得 →
`authorization_servers` からこのサーバ自身の `/oauth/authorize` を見つけ →
Authorization Code + PKCE フローで access token を取得、という流れが走る。
**ブラウザでのユーザログインは不要**。

## Claude Code (CLI) からの接続

```bash
claude mcp add airhost --transport http https://<cloud-run-host>/mcp/
```

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

### 1. Secret Manager に機密値を入れる

```bash
# OAuth シークレット（print() は末尾に \n を付けるため end='' を使う）
python3 -c "import secrets; print(secrets.token_urlsafe(32), end='')" \
  | gcloud secrets create MCP_CLIENT_SECRET --project=YOUR_PROJECT_ID --data-file=-
python3 -c "import secrets; print(secrets.token_urlsafe(48), end='')" \
  | gcloud secrets create MCP_TOKEN_SECRET  --project=YOUR_PROJECT_ID --data-file=-

echo -n "your-airhost-username" | gcloud secrets create AIRHOST_USERNAME --data-file=-
echo -n "your-airhost-password" | gcloud secrets create AIRHOST_PASSWORD --data-file=-

# Gmail の credentials.json と token.json（事前にローカルで一度ログイン同意して生成）
gcloud secrets create GMAIL_CREDENTIALS --data-file=./gmail_credentials.json
gcloud secrets create GMAIL_TOKEN --data-file=./gmail_token.json

# 各 secret に runner SA の secretAccessor を付与
for s in MCP_CLIENT_SECRET MCP_TOKEN_SECRET AIRHOST_USERNAME AIRHOST_PASSWORD GMAIL_CREDENTIALS GMAIL_TOKEN; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member=serviceAccount:airhost-mcp-runner@YOUR_PROJECT_ID.iam.gserviceaccount.com \
    --role=roles/secretmanager.secretAccessor
done
```

### 2. デプロイ

```bash
PROJECT_ID=YOUR_PROJECT_ID \
REGION=asia-northeast1 \
SESSION_GCS_BUCKET=airhost-mcp-sessions-YOURUNIQ \
MCP_CLIENT_ID=airhost-mcp \
MCP_PUBLIC_URL=https://airhost-mcp-XXXXX.asia-northeast1.run.app \
SERVICE_ACCOUNT=airhost-mcp-runner@YOUR_PROJECT_ID.iam.gserviceaccount.com \
./scripts/deploy_cloudrun.sh
```

`--allow-unauthenticated` でデプロイしているのは、claude.ai が GCP IAM を
持たないため。**OAuth 2.1 の client_secret + HMAC JWT** でアクセス制御する。

### 3. 動作確認

```bash
URL=$(gcloud run services describe airhost-mcp --region asia-northeast1 --format='value(status.url)')
curl -s "$URL/health"
curl -i -H "Authorization: Bearer YOUR_TOKEN" "$URL/mcp/"
```

---

## アクセス制御 / シークレットのローテーション

「接続できるクライアント」は **`MCP_CLIENT_SECRET` を知っているコネクタ** だけ。
身内専用のためユーザ単位の制御はなく、シークレットの共有 = アクセス許可。

### シークレットのローテーション

```bash
# 新しいシークレットを生成
python3 -c "import secrets; print(secrets.token_urlsafe(32))" | \
  gcloud secrets versions add MCP_CLIENT_SECRET --data-file=-

# Cloud Run に反映（新リビジョン作成）
gcloud run services update airhost-mcp \
  --region asia-northeast1 \
  --update-secrets "MCP_CLIENT_SECRET=MCP_CLIENT_SECRET:latest"
```

ローテーション後は Claude コネクタの Client Secret を新しい値に更新して
再認証が必要。

### コネクタを無効化する（漏洩対応）

`MCP_CLIENT_SECRET` を新しい値にローテーションすれば既存のトークンは
次回 401 になるため、事実上のアクセス遮断になる。

---

## MFA 戦略の使い分け

| 戦略     | env                         | 用途                                                                                      |
| -------- | --------------------------- | ----------------------------------------------------------------------------------------- |
| `gmail`  | `MFA_STRATEGY=gmail`        | 現在の既定。Gmail API を直接ポーリングして MFA メールから 6 桁コードを抽出。              |
| `pubsub` | `MFA_STRATEGY=pubsub`       | **未実装の枠**。将来「メール → Zapier or Gmail forwarder → Pub/Sub push」を組んだら有効化。 |
| `manual` | `MFA_STRATEGY=manual`       | デバッグ用。stdin からコードを入力。Cloud Run では使えない。                              |

戦略追加は `src/airhost_mcp/mfa/` に新ファイルを置き、`factory.py` に分岐を 1 行足すだけ。

---

## ローカルMCP単体テスト
```
DEV_DISABLE_AUTH=true

curl http://localhost:8080/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{ "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": { "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": { "name": "Human", "title": "Human Powered Client", "version": "0.0.1" }}}'


curl http://localhost:8080/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: SESSION-ID" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {}
  }'


curl http://localhost:8080/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: SESSION-ID" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
      "name": "list_listings",
      "arguments": {}
    }
}'
```

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
│   ├── server.py            # FastAPI + FastMCP マウント + OAuth ミドルウェア
│   ├── tools.py             # MCP ツール定義
│   ├── auth.py              # Auth0 access-token 検証 + 許可リストチェック
│   ├── well_known.py        # /.well-known/oauth-* discovery エンドポイント
│   ├── config.py            # pydantic-settings
│   ├── airhost/             # AirhostClient + Mock + Browser(Playwright, TBD)
│   ├── mfa/                 # MFAStrategy (gmail / pubsub / manual)
│   └── session/             # SessionStore (local / gcs)
├── notifier/                # 宿泊者名簿 完了通知ジョブ（後述）
│   ├── main.py
│   ├── Dockerfile
│   └── .env.example
├── scripts/
│   ├── deploy_cloudrun.sh
│   ├── deploy_notifier.sh   # notifier の Cloud Run Job + Scheduler デプロイ
│   ├── login_smoke.py       # ログイン〜MFA フローのスモークテスト
│   └── tools_smoke.py       # 各 MCP ツールを直接叩くスモークテスト
├── tests/
├── Dockerfile
├── pyproject.toml
└── .env.example
```

---

## 宿泊者名簿 完了通知（notifier）

`notifier/` は MCP サーバーとは独立した **Cloud Run Job** として動作する。
Cloud Scheduler から2種類のジョブで起動し、対象リスティングの未来予約を走査して
宿泊者名簿（オンラインチェックイン）が 100% 完了した予約を LINE に通知する。
チェックイン間近でまだ未完了の予約には、朝の実行時に催促通知も送る（未完了の間は毎回送るため状態は保存しない）。

- `<job>-trigger-reminder`（デフォルト 9時 JST）: 完了通知に加え、チェックイン `REMINDER_DAYS_BEFORE` 日前以内で未完了の予約に催促通知（`REMINDER_RUN=true`）
- `<job>-trigger`（デフォルト 12,15,18,21時 JST）: 完了通知のみ（`REMINDER_RUN=false`）

### 動作フロー

```
Cloud Scheduler (9時=催促あり / 12,15,18,21時=完了通知のみ, JST)
    ↓ overrides.containerOverrides.env で REMINDER_RUN を実行ごとに指定
Cloud Run Job: airhost-notifier   ← python:3.11-slim（Playwright 不要・軽量）
    ↓ Bearer token (MCP_ACCESS_TOKEN)
MCP サーバー（既存 Cloud Run）
    ↓ get_guest_registration / list_reservations_in_range
通知済みフラグ: gs://<bucket>/airhost-notifier/notified/<reservation_id>.json
    ↓ is_complete=true & 未通知 → 完了通知（送信後フラグ保存） / 未完了 & REMINDER_RUN & チェックインN日前以内 → 催促通知（毎回送信、状態は保存しない）
LINE Messaging API → 対象ユーザー全員に push
```

### 環境変数

| 変数名 | 説明 |
|--------|------|
| `MCP_PUBLIC_URL` | MCPサーバーのURL（例: `https://airhost-mcp-xxx.run.app`） |
| `MCP_ACCESS_TOKEN` | MCPサーバーへの長期アクセストークン（Secret Manager） |
| `LINE_CHANNEL_TOKEN` | LINE Messaging API チャネルアクセストークン（Secret Manager） |
| `LINE_USER_IDS` | 通知先ユーザーID（カンマ区切り） |
| `LISTING_IDS` | 対象リスティングID（カンマ区切りで複数可） |
| `GCS_BUCKET` | 通知フラグ保存先バケット（MCPサーバーと同一可） |
| `GCS_NOTIFIER_PREFIX` | GCS内のプレフィックス（デフォルト: `airhost-notifier/notified/`） |
| `LOOKAHEAD_DAYS` | 何日先の予約まで対象にするか（デフォルト: 60） |
| `REMINDER_RUN` | 未完了予約への催促通知も行うか（デフォルト: `false`。`trigger-reminder` ジョブが `true` を渡す） |
| `REMINDER_DAYS_BEFORE` | チェックイン何日前から催促するか（デフォルト: 0＝当日のみ） |

### セットアップ

```bash
# 1. Secret Manager に登録（MCP_CLIENT_SECRET はMCPサーバーと同じ値）
echo -n "your-line-channel-token" | gcloud secrets create LINE_CHANNEL_TOKEN --data-file=-
# MCP_CLIENT_SECRET は deploy_cloudrun.sh で登録済みのものをそのまま共有

# 2. デプロイ
PROJECT_ID=YOUR_PROJECT_ID \
SESSION_GCS_BUCKET=your-bucket \
MCP_PUBLIC_URL=https://airhost-mcp-xxx.run.app \
MCP_CLIENT_ID=airhost-mcp \
LISTING_IDS=46349408-127c-4401-ae23-28b10b61ce15 \
LINE_USER_IDS=Uxxxxxxxxxxxxxxxx \
SERVICE_ACCOUNT=airhost-mcp-runner@YOUR_PROJECT_ID.iam.gserviceaccount.com \
./scripts/deploy_notifier.sh

# 3. 手動実行でテスト
gcloud run jobs execute airhost-notifier --region asia-northeast1
```

このリポジトリでは上記の値を手打ちする代わりに、**リポジトリ直下の`.env`（PROJECT_ID/SESSION_GCS_BUCKET/MCP_PUBLIC_URLなど）と`notifier/.env`（LISTING_IDS/LINE_USER_IDS/LINE_CHANNEL_TOKENなど）の両方をsourceしてから**`./scripts/deploy_notifier.sh`を実行する運用にしている。

```bash
set -a && source .env && source notifier/.env && set +a
./scripts/deploy_notifier.sh
```

### LINE 通知フォーマット

```
【宿泊者名簿 完了】
物件: VILLA Seamu
予約元: Airbnb
代表者: 山田 太郎
チェックイン: 2026-07-01
チェックアウト: 2026-07-03
人数: 4名
名簿: 4/4名 完了
本人確認書類: あり
```

---

## 実装するときの注意

1. `.env` と `gmail_credentials.json` / `gmail_token.json` は **絶対に commit しない**（`.gitignore` 済み）。**実メールアドレスもコミットしない** — `MCP_ALLOWED_EMAILS` は Secret Manager 側にだけ置く。
2. `BrowserAirhostClient` が動くまで本番 `AIRHOST_CLIENT=browser` には切り替えない。モックのまま Claude に接続して動作確認するのが先。
3. Cloud Run + Playwright は **メモリ ≥ 1Gi、min-instances=1** にしないとコールドスタートで Chromium 起動に数秒かかる。常時呼ぶならコスト面で min-instances=1 が現実的。
4. `Dockerfile` の `mcr.microsoft.com/playwright/python:vX.Y.Z-noble` のタグは `pyproject.toml` の `playwright==` バージョンと合わせる（バージョンずれは起動時に警告 → 失敗の元）。

---

## 未実装 / TODO

優先度順ではなく、**気付いたら拾うリスト**。短くやれそうなものから。

- ~~**Gmail MFA メールの自動整理**~~: ✅ 実装済み。`MFA_AFTER_FETCH=keep|read|archive|trash|delete` で制御。scope は `gmail.modify`。`delete` は Trash をバイパスして完全削除（復元不可）。
- ~~**監査ログ**~~: ✅ 実装済み。`tools.py` の `_audit()` が `AUDIT tool=... user=... ts=...` 形式で INFO ログを出力。
- **Pub/Sub MFA strategy**: 枠だけ用意（`MFA_STRATEGY=pubsub`）。Gmail forwarder + Zapier or 直接 Pub/Sub push のパイプラインを組んだら有効化。
- **`block_date` の本実装**: 読み取り系 4 ツールは API 経由で実装済みだが、書き込み系の `block_date` はまだ `NotImplementedError`。Airhost UI でブロックを **作成**したときに走る POST と、**削除**したときに走る DELETE/POST を Network タブで観察し、URL とリクエスト/レスポンスの shape をメモ → 実装する。本物データへの影響を避けるため、テストは遠い未来の空き日（例: 2027-12-31）でやる。
- **`update_reservation` の本実装**: 同じく未実装。予約画面で日付変更 / ゲスト数変更 / メモ追記 をしたときに走る PATCH/PUT を観察 → 実装。Airbnb / Booking.com 系の予約は OTA 側からの変更しか受け付けない可能性があるので、対応できる項目を最初に整理してから実装。
- ~~**Cloud Run の min-instances 切替**~~: ✅ min=1 / max=1 に変更済み。
- **OAuth state の GCS 永続化**: `oauth_server.py` の認証コード (`_auth_codes`) とリフレッシュトークン (`_refresh_tokens`) が現在インメモリのため、インスタンス再起動（デプロイ・メンテ等）でリセットされる。アクセストークンは 365 日有効な JWT なので実害は限定的だが、長期的には GCS に永続化すべき。既存の `SessionStore` と同じバケットに `oauth/auth_codes/<code>.json`・`oauth/refresh_tokens/<token>.json` として保存するのが最短。
- **GET /mcp 409 ループ問題**: Cloud Run のロードバランサーが SSE 接続を黙って切断するが FastMCP がそれを検知できず、Claude のコネクタが再接続しようとすると 409 が返り続ける。POST（ツール呼び出し）は正常動作するため実害は軽微だが、根本解決には FastMCP 側での SSE keepalive 実装または接続管理の改善が必要。
- **エラー通知 / モニタリング**: Cloud Logging のエラー検知 → メール / Slack 通知。今は無し。
- **GET /mcp 409 Conflict ループ**: Cloud Run LB が SSE 接続を無音でクローズするため、FastMCP が同一セッションの再接続を 409 で拒否することがある。POST の tool call 自体は成功するため実害は限定的だが、Claude のコネクタが 409 ループに入ると接続が不安定になる。FastMCP の内部セッション管理（`StreamableHTTPSessionManager`）に手を入れるか、セッション ID を使い捨てにするプロキシを挟む必要がある。
