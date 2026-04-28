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
        │  HTTPS + Firebase ID-token (OAuth 2.1 Bearer)
        ▼
FastAPI ──► /health
        ├─► /.well-known/oauth-protected-resource     (RFC 9728)
        ├─► /.well-known/oauth-authorization-server   (RFC 8414, proxied from Firebase)
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
# 最低限編集: FIREBASE_PROJECT_ID, MCP_ALLOWED_EMAILS, AIRHOST_USERNAME, AIRHOST_PASSWORD
# モック起動なら AIRHOST_CLIENT=mock のまま、Gmail/MFA 系は空でも可
```

ローカルで Firebase 検証を有効にする場合は、`GOOGLE_APPLICATION_CREDENTIALS`
にダウンロード済みのサービスアカウント鍵 (`firebase-adminsdk-*.json`) を指す。
Firebase に当てない素のスモークテストだけで良ければ、`DEV_DISABLE_AUTH=true`
でミドルウェアそのものをスキップできる（**Cloud Run 上では K_SERVICE が常に
セットされているため、production では絶対に有効化されない**）。

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

# Firebase ID-token を持って叩く（実際の MCP 通信は Claude 側のクライアントを使うのが楽）
curl -i -H "Authorization: Bearer ${FIREBASE_ID_TOKEN}" http://localhost:8080/mcp/
```

ローカルで ID トークンを得る最短ルート: Firebase Console > Authentication >
ユーザ追加 → Firebase REST API (`signInWithPassword`) でログイン → 戻ってくる
`idToken` を `Authorization: Bearer` で渡す。


### 5. テスト

```bash
pytest
```

---

## 認証モデル（OAuth 2.1 / Firebase Authentication）

このサーバは **OAuth 2.1 の Resource Server** として振る舞う。Authorization
Server は同 GCP プロジェクトの **Firebase Authentication** で、Google ログイン
プロバイダ経由でユーザを認証する。手前の MCP クライアント（claude.ai など）は
標準の OAuth フローでブラウザ Sign-In を行い、Firebase ID-token を発行
させ、それを `Authorization: Bearer <id_token>` として MCP リクエストに
載せる。サーバは毎リクエストで:

1. Firebase Admin SDK で署名・iss・aud・exp を検証
2. `email_verified == true` を要求
3. `email`（小文字化）が **`MCP_ALLOWED_EMAILS`** 許可リストに入っているか確認
4. いずれか失敗 → `401 + WWW-Authenticate: Bearer resource_metadata="..."`

許可リストは Secret Manager で管理する。GCP の owner / editor 権限を
持つ人だけが書き換えられる。サーバ側はメール許可リストにあるアカウントしか
信頼しない。

### Discovery エンドポイント

| パス | 役割 |
| ---- | ---- |
| `GET /.well-known/oauth-protected-resource`     | RFC 9728. `resource` と `authorization_servers` を返す |
| `GET /.well-known/oauth-authorization-server`   | RFC 8414. Firebase の OpenID configuration を 10 分キャッシュでプロキシ。失敗時はハンド書きの subset にフォールバック |

> **Dynamic Client Registration (RFC 7591) は実装していない**。Firebase の
> OAuth クライアントは Firebase Console で管理する設計で、RFC 7591 を素直に
> 満たすのは無理。MCP の認可仕様 (2025-06-18) でも DCR は SHOULD であり MUST
> ではないため、`registration_endpoint` を AS metadata から省く形で「未対応」
> を表明する。claude.ai は `registration_endpoint` がない場合は事前構成済み
> のクライアント ID にフォールバックするので、これで成立する。

---

## Claude.ai (リモート MCP) に登録

Claude.ai の Custom Connector に **URL だけ** 登録すれば良い:

- URL: `https://<cloud-run-host>/mcp/`

初回接続時に claude.ai が `/.well-known/oauth-protected-resource` を取得 →
`authorization_servers` に書かれている Firebase の issuer を見つけ →
そこの `/.well-known/openid-configuration` を取得 → ブラウザを開いて Google
ログインを促す、という標準フローが走る。**ベアラ秘密値の手動配布は不要**。

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
  secretmanager.googleapis.com storage.googleapis.com \
  identitytoolkit.googleapis.com firebase.googleapis.com

# Firebase Authentication を有効化
# (Firebase Console > Authentication > Get started → Sign-in method →
#  Google を Enable。プロジェクトは GCP プロジェクトと同じものを使う。)

# セッション保存用 GCS バケット
gsutil mb -l asia-northeast1 gs://airhost-mcp-sessions-YOURUNIQ

# サービスアカウント（Cloud Run 実行用）
gcloud iam service-accounts create airhost-mcp-runner \
  --display-name "Airhost MCP runner"

# バケットへの読み書き権限
gsutil iam ch \
  serviceAccount:airhost-mcp-runner@YOUR_PROJECT_ID.iam.gserviceaccount.com:objectAdmin \
  gs://airhost-mcp-sessions-YOURUNIQ

# Firebase ID-token を verify するための権限。Application Default Credentials
# 経由なので鍵ファイル不要。最低限 Firebase project の閲覧権限があれば
# verify_id_token は通る。
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member=serviceAccount:airhost-mcp-runner@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  --role=roles/firebaseauth.viewer
```

### 1. Secret Manager に機密値を入れる

```bash
# 許可メールリスト。**実メールはコミットせず、Secret Manager にだけ書く**。
printf 'alice@example.com,bob@example.com' \
  | gcloud secrets create MCP_ALLOWED_EMAILS --data-file=-

echo -n "your-airhost-password" | gcloud secrets create AIRHOST_PASSWORD --data-file=-

# Gmail の token.json（事前にローカルで一度ログイン同意して生成）
gcloud secrets create GMAIL_TOKEN_JSON --data-file=./gmail_token.json

# 各 secret に runner SA の secretAccessor を付与
for s in MCP_ALLOWED_EMAILS AIRHOST_PASSWORD GMAIL_TOKEN_JSON; do
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
FIREBASE_PROJECT_ID=YOUR_PROJECT_ID \
MCP_PUBLIC_URL=https://airhost-mcp-XXXXX.asia-northeast1.run.app \
SERVICE_ACCOUNT=airhost-mcp-runner@YOUR_PROJECT_ID.iam.gserviceaccount.com \
./scripts/deploy_cloudrun.sh
```

このスクリプトはイメージビルド + 初回デプロイまで。残りの env / secret は
次のコマンドで上書きする:

```bash
gcloud run services update airhost-mcp \
  --region asia-northeast1 \
  --update-secrets AIRHOST_PASSWORD=AIRHOST_PASSWORD:latest \
  --update-env-vars AIRHOST_USERNAME=you@example.com \
  --update-env-vars AIRHOST_CLIENT=mock \
  --update-env-vars MFA_STRATEGY=gmail \
  --update-env-vars MFA_SENDER=no-reply@airhost.co
```

`--allow-unauthenticated` でデプロイしているのは、claude.ai が GCP IAM を
持たないため。**OAuth 2.1 + Firebase ID-token + メール許可リスト** で
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

新規ユーザは Firebase Authentication 側にも 1 回ログインしておく必要がある
（Google プロバイダなので、claude.ai 側のブラウザログインで自動的に作られる）。

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

新リビジョンが routing された後は、当該ユーザの Firebase ID-token は即 401。

### Firebase 側でユーザを完全に消す（漏洩・解雇など強い対応）

```bash
# Firebase Console > Authentication > Users から無効化 / 削除する。
# あるいは Admin SDK CLI 経由:
gcloud auth print-identity-token | head -c0   # ADC があることだけ確認
firebase auth:export users.json --project YOUR_PROJECT_ID
# users.json を編集して disabled=true で再 import など。
```

許可リストから消した時点でアクセスは止まるので、通常はそこまで踏み込まなくて
良い。

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
│   ├── auth.py              # Firebase ID-token 検証 + 許可リストチェック
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

- **Gmail MFA メールの自動整理**: 現状は読み取りのみ（scope `gmail.readonly`）。MFA コード取得後に **既読化 + アーカイブ** する設定 `MFA_AFTER_FETCH=keep|read|archive|trash` を追加したい。実装には scope を `gmail.modify` に昇格 → 既存 `gmail_token.json` の再 consent が必要。デフォルトは `archive`（Inbox から消えるが履歴は残る）が無難。
- **監査ログ**: 「どのユーザー（email）がどのツールをいつ呼んだか」を構造化ログに残す。OAuth ミドルウェアで `request.state.user_email` を立てているので、`tools.py` で thin wrapper を入れるだけで足りる。
- **Pub/Sub MFA strategy**: 枠だけ用意（`MFA_STRATEGY=pubsub`）。Gmail forwarder + Zapier or 直接 Pub/Sub push のパイプラインを組んだら有効化。
- **`block_date` の本実装**: 読み取り系 4 ツールは API 経由で実装済みだが、書き込み系の `block_date` はまだ `NotImplementedError`。Airhost UI でブロックを **作成**したときに走る POST と、**削除**したときに走る DELETE/POST を Network タブで観察し、URL とリクエスト/レスポンスの shape をメモ → 実装する。本物データへの影響を避けるため、テストは遠い未来の空き日（例: 2027-12-31）でやる。
- **`update_reservation` の本実装**: 同じく未実装。予約画面で日付変更 / ゲスト数変更 / メモ追記 をしたときに走る PATCH/PUT を観察 → 実装。Airbnb / Booking.com 系の予約は OTA 側からの変更しか受け付けない可能性があるので、対応できる項目を最初に整理してから実装。
- **Cloud Run の min-instances 切替**: 現状 0（コールドスタート許容）。Playwright + Chromium だと初回 5–10 秒待つ。実運用に入ったら min=1 へ（月数千円のコスト増）。
- **エラー通知 / モニタリング**: Cloud Logging のエラー検知 → メール / Slack 通知。今は無し。
