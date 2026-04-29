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

---

## アーキテクチャ

```
Claude (remote MCP client)
        │  HTTPS + Auth0 access token (OAuth 2.1 Bearer)
        ▼
FastAPI ──► /health
        ├─► /.well-known/oauth-protected-resource     (RFC 9728)
        ├─► /.well-known/oauth-authorization-server   (RFC 8414, proxied from Auth0)
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
# 最低限編集: AUTH0_DOMAIN, AUTH0_AUDIENCE, MCP_ALLOWED_EMAILS, AIRHOST_USERNAME, AIRHOST_PASSWORD
# モック起動なら AIRHOST_CLIENT=mock のまま、Gmail/MFA 系は空でも可
```

ローカルで Auth0 検証を本物の token で試す場合は、Auth0 Dashboard の API 詳細
ページから **Test** タブで test token を発行して `Authorization: Bearer <token>`
として使える。素のスモークテストだけなら `DEV_DISABLE_AUTH=true` でミドルウェア
そのものをスキップできる（**Cloud Run 上では K_SERVICE が常にセットされている
ため、production では絶対に有効化されない**）。

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

# OAuth 2.1 discovery (公開, 認証不要)
curl -s http://localhost:8080/.well-known/oauth-protected-resource
curl -s http://localhost:8080/.well-known/oauth-authorization-server

# 認証なしは 401 + WWW-Authenticate ヘッダ
curl -i http://localhost:8080/mcp/

# Auth0 access token を持って叩く（実際の MCP 通信は Claude 側のクライアントを使うのが楽）
curl -i -H "Authorization: Bearer ${AUTH0_ACCESS_TOKEN}" http://localhost:8080/mcp/
```

ローカルで access token を得る最短ルート: Auth0 Dashboard >
**Applications → APIs → (your API) → Test** タブにある "Response" の
`access_token` をコピー（M2M クライアント経由）。Google ログインフローで実際の
ユーザ token が要る場合は、後述 "Auth0 Action" で email claim を付けた状態
にしてから使うこと。


### 5. テスト

```bash
pytest
```

---

## 認証モデル（OAuth 2.1 / Auth0）

このサーバは **OAuth 2.1 の Resource Server** として振る舞う。Authorization
Server は **Auth0** で、Google ログインプロバイダ経由でユーザを認証する。
claude.ai のような MCP クライアントは Auth0 の **Dynamic Client Registration
(RFC 7591)** で自身を OAuth クライアントとして登録 → Authorization Code +
PKCE フローで access token を取得 → `Authorization: Bearer <token>` として
MCP リクエストに載せる。サーバは毎リクエストで:

1. `python-jose` で署名・iss・aud・exp を Auth0 JWKS に対し検証
2. `email_verified == true` を要求
3. `email`（小文字化）が **`MCP_ALLOWED_EMAILS`** 許可リストに入っているか確認
4. いずれか失敗 → `401 + WWW-Authenticate: Bearer resource_metadata="..."`

許可リストは Secret Manager で管理する。GCP の owner / editor 権限を持つ人だけ
が書き換えられる。Auth0 だけで認証が通っても、許可リストに居なければ MCP
サーバ側で拒否される。

### email claim を access token に乗せる Auth0 Action（必須）

Auth0 の access token はデフォルトで `email` claim を含まない。Auth0 Dashboard
の **Actions → Library → Build Custom** から下記の Login Flow を作成し、
**Login Flow に attach する**:

```js
exports.onExecutePostLogin = async (event, api) => {
  api.accessToken.setCustomClaim('https://airhost-mcp/email', event.user.email);
  api.accessToken.setCustomClaim('https://airhost-mcp/email_verified', event.user.email_verified);
};
```

このサーバは標準の `email` クレーム / 名前空間付き `https://airhost-mcp/email`
クレームの **どちらでも** 受け付ける（Action 設定が違っても動くように）。

### Discovery エンドポイント

| パス | 役割 |
| ---- | ---- |
| `GET /.well-known/oauth-protected-resource`     | RFC 9728. `resource` と `authorization_servers` (Auth0 issuer) を返す |
| `GET /.well-known/oauth-authorization-server`   | RFC 8414. Auth0 の OpenID configuration を 10 分キャッシュでプロキシ。`registration_endpoint` も含むのでクライアント DCR が機能する |

> **Dynamic Client Registration (RFC 7591) は Auth0 ネイティブ対応**。Auth0 の
> Tenant Settings → Advanced で **OIDC Dynamic Application Registration** を
> ON にしておく必要がある（README の "Auth0 セットアップ" 参照）。
> claude.ai はこの DCR エンドポイントを使って自身を登録するため、ここを ON に
> しないと "Couldn't reach the MCP server" になる。

---

## Claude.ai (リモート MCP) に登録

Claude.ai の Custom Connector に **URL だけ** 登録すれば良い:

- URL: `https://<cloud-run-host>/mcp/`

初回接続時に claude.ai が `/.well-known/oauth-protected-resource` を取得 →
`authorization_servers` に書かれている Auth0 の issuer を見つけ → そこの
`/.well-known/openid-configuration` を取得 → DCR エンドポイントで自身を OAuth
クライアントとして登録 → ブラウザを開いて Google ログインを促す、という
標準フローが走る。**ベアラ秘密値の手動配布は不要**。

許可リスト (`MCP_ALLOWED_EMAILS`) に載っていない Google アカウントでログイン
すると、claude.ai 側に 401 が返って再ログインが促される。

## Claude Code (CLI) からの接続

```bash
claude mcp add airhost --transport http https://<cloud-run-host>/mcp/
```

OAuth が必要な MCP server については、`claude` 起動時にブラウザで一度だけ
ログインを求められる。claude が ID-token をキャッシュ・自動リフレッシュする。

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

# Auth0 セットアップ（GCP の外、操作はブラウザで）:
#   1. https://auth0.com で tenant 作成（region は jp 推奨）
#   2. Applications → APIs → Create API
#        Name: airhost-mcp / Identifier: 任意 URL（aud に使う） / RS256
#   3. Authentication → Social → Google を Enable
#   4. Tenant Settings → Advanced → "OIDC Dynamic Application Registration" を ON
#        ← claude.ai の DCR フローに必須
#   5. Actions → Library → Build Custom で email claim 注入の Login Flow を追加
#        （上の "認証モデル" セクション参照）
#   6. AUTH0_DOMAIN（例: tenant.jp.auth0.com）と
#      AUTH0_AUDIENCE（API の Identifier）をメモ
```

### 1. Secret Manager に機密値を入れる

```bash
# 許可メールリスト。**実メールはコミットせず、Secret Manager にだけ書く**。
printf 'alice@example.com,bob@example.com' \
  | gcloud secrets create MCP_ALLOWED_EMAILS --data-file=-

echo -n "your-airhost-username" | gcloud secrets create AIRHOST_USERNAME --data-file=-
echo -n "your-airhost-password" | gcloud secrets create AIRHOST_PASSWORD --data-file=-

# Gmail の credentials.json と token.json（事前にローカルで一度ログイン同意して生成）
gcloud secrets create GMAIL_CREDENTIALS --data-file=./gmail_credentials.json
gcloud secrets create GMAIL_TOKEN --data-file=./gmail_token.json

# 各 secret に runner SA の secretAccessor を付与
for s in MCP_ALLOWED_EMAILS AIRHOST_USERNAME AIRHOST_PASSWORD GMAIL_CREDENTIALS GMAIL_TOKEN; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member=serviceAccount:airhost-mcp-runner@YOUR_PROJECT_ID.iam.gserviceaccount.com \
    --role=roles/secretmanager.secretAccessor
done
```

### 2. デプロイ

最短:

```bash
PROJECT_ID=YOUR_PROJECT_ID \
REGION=asia-northeast1 \
SESSION_BUCKET=airhost-mcp-sessions-YOURUNIQ \
AUTH0_DOMAIN=tenant.jp.auth0.com \
AUTH0_AUDIENCE=https://airhost-mcp.example.com \
MCP_PUBLIC_URL=https://airhost-mcp-XXXXX.asia-northeast1.run.app \
SERVICE_ACCOUNT=airhost-mcp-runner@YOUR_PROJECT_ID.iam.gserviceaccount.com \
./scripts/deploy_cloudrun.sh
```

このスクリプトはイメージビルド + 初回デプロイまで。残りの env / secret は
次のコマンドで上書きする:

```bash
gcloud run services update airhost-mcp \
  --region asia-northeast1 \
  --update-secrets "AIRHOST_USERNAME=AIRHOST_USERNAME:latest" \
  --update-secrets "AIRHOST_PASSWORD=AIRHOST_PASSWORD:latest" \
  --update-secrets "MCP_ALLOWED_EMAILS=MCP_ALLOWED_EMAILS:latest" \
  --set-secrets "/secrets/gmail_credentials.json=GMAIL_CREDENTIALS:latest" \
  --set-secrets "/secrets/gmail_token.json=GMAIL_TOKEN:latest" \
  --update-env-vars "AIRHOST_CLIENT=browser" \
  --update-env-vars "MFA_STRATEGY=gmail" \
  --update-env-vars "MFA_SENDER=no-reply@airhost.co" \
  --update-env-vars "GMAIL_CREDENTIALS_PATH=/secrets/gmail_credentials.json" \
  --update-env-vars "GMAIL_TOKEN_PATH=/secrets/gmail_token.json"
```

`--allow-unauthenticated` でデプロイしているのは、claude.ai が GCP IAM を
持たないため。**OAuth 2.1 + Auth0 access token + メール許可リスト** で
アクセス制御する。

### 3. 動作確認

```bash
URL=$(gcloud run services describe airhost-mcp --region asia-northeast1 --format='value(status.url)')
curl -s "$URL/health"
curl -i -H "Authorization: Bearer YOUR_TOKEN" "$URL/mcp/"
```

---

## ユーザーの追加 / 失効 / ローテーション

「誰が使えるか」は **`MCP_ALLOWED_EMAILS` Secret に列挙されたメール
アドレス** だけで決まる。サーバ側では追加の発行物（トークン文字列等）は
持たない。実際のログインは Google アカウントが行うので、**メールが許可
リストに居るかどうか == アクセス権の全て**。

許可リストを書き換えられるのは GCP プロジェクトの Secret Manager に書き込み
権限を持つ人（owner / editor / `roles/secretmanager.admin`）だけ。

### 新しい人を追加する

```bash
EXISTING=$(gcloud secrets versions access latest --secret=MCP_ALLOWED_EMAILS)
printf '%s,%s' "$EXISTING" "newuser@example.com" \
  | gcloud secrets versions add MCP_ALLOWED_EMAILS --data-file=-

# Cloud Run は secret を起動時に解決するので、新リビジョン作成が必要。
gcloud run services update airhost-mcp \
  --region asia-northeast1 \
  --update-secrets "MCP_ALLOWED_EMAILS=MCP_ALLOWED_EMAILS:latest"
```

新規ユーザは Auth0 側にも 1 回ログインしておく必要がある（Google プロバイダ
なので、claude.ai 側のブラウザログインで自動的にユーザレコードが作られる）。

### 既存ユーザーを失効させる

```bash
TARGET="leaver@example.com"
KEEP=$(gcloud secrets versions access latest --secret=MCP_ALLOWED_EMAILS \
  | tr ',' '\n' | grep -iv "^${TARGET}$" | paste -sd, -)
printf '%s' "$KEEP" | gcloud secrets versions add MCP_ALLOWED_EMAILS --data-file=-

gcloud run services update airhost-mcp \
  --region asia-northeast1 \
  --update-secrets "MCP_ALLOWED_EMAILS=MCP_ALLOWED_EMAILS:latest"
```

新リビジョンが routing された後は、当該ユーザの Auth0 access token は即 401。

### Auth0 側でユーザを完全に消す（漏洩・解雇など強い対応）

許可リストから消した時点でこの MCP サーバへのアクセスは止まるので、通常は
そこまで踏み込まなくて良い。Auth0 全体（他のアプリも含む）からブロックしたい
場合は Auth0 Dashboard → User Management → Users で対象を **Block** または
**Delete** する。

### 注意

- 監査ログ: ミドルウェアが `request.state.user_email` をセットするので、ここを
  読んで構造化ログを吐けば「誰がいつどのツールを呼んだか」を取れる。
- DCR (Dynamic Client Registration) は実装していない。理由は前述。

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
│   ├── tools.py             # 6 つの MCP ツール定義
│   ├── auth.py              # Auth0 access-token 検証 + 許可リストチェック
│   ├── well_known.py        # /.well-known/oauth-* discovery エンドポイント
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

1. `.env` と `gmail_credentials.json` / `gmail_token.json` は **絶対に commit しない**（`.gitignore` 済み）。**実メールアドレスもコミットしない** — `MCP_ALLOWED_EMAILS` は Secret Manager 側にだけ置く。
2. `BrowserAirhostClient` が動くまで本番 `AIRHOST_CLIENT=browser` には切り替えない。モックのまま Claude に接続して動作確認するのが先。
3. Cloud Run + Playwright は **メモリ ≥ 1Gi、min-instances=1** にしないとコールドスタートで Chromium 起動に数秒かかる。常時呼ぶならコスト面で min-instances=1 が現実的。
4. `Dockerfile` の `mcr.microsoft.com/playwright/python:vX.Y.Z-noble` のタグは `pyproject.toml` の `playwright==` バージョンと合わせる（バージョンずれは起動時に警告 → 失敗の元）。

---

## 未実装 / TODO

優先度順ではなく、**気付いたら拾うリスト**。短くやれそうなものから。

- ~~**Gmail MFA メールの自動整理**~~: ✅ 実装済み。`MFA_AFTER_FETCH=keep|read|archive|trash` で制御。scope は `gmail.modify`。
- ~~**監査ログ**~~: ✅ 実装済み。`tools.py` の `_audit()` が `AUDIT tool=... user=... ts=...` 形式で INFO ログを出力。
- **Pub/Sub MFA strategy**: 枠だけ用意（`MFA_STRATEGY=pubsub`）。Gmail forwarder + Zapier or 直接 Pub/Sub push のパイプラインを組んだら有効化。
- **`block_date` の本実装**: 読み取り系 4 ツールは API 経由で実装済みだが、書き込み系の `block_date` はまだ `NotImplementedError`。Airhost UI でブロックを **作成**したときに走る POST と、**削除**したときに走る DELETE/POST を Network タブで観察し、URL とリクエスト/レスポンスの shape をメモ → 実装する。本物データへの影響を避けるため、テストは遠い未来の空き日（例: 2027-12-31）でやる。
- **`update_reservation` の本実装**: 同じく未実装。予約画面で日付変更 / ゲスト数変更 / メモ追記 をしたときに走る PATCH/PUT を観察 → 実装。Airbnb / Booking.com 系の予約は OTA 側からの変更しか受け付けない可能性があるので、対応できる項目を最初に整理してから実装。
- **Cloud Run の min-instances 切替**: 現状 0（コールドスタート許容）。Playwright + Chromium だと初回 5–10 秒待つ。実運用に入ったら min=1 へ（月数千円のコスト増）。
- **エラー通知 / モニタリング**: Cloud Logging のエラー検知 → メール / Slack 通知。今は無し。
