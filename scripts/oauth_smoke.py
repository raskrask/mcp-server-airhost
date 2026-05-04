"""OAuth 2.1 + MCP 接続フルスモークテスト。

サーバーに対して Claude のコネクタと同じ OAuth フロー（Authorization Code + PKCE）を
実行し、取得したトークンで MCP initialize まで通るかを確認する。

使い方:
    # リモートサーバー（Cloud Run）に対して実行
    MCP_CLIENT_ID=airhost-mcp \
    MCP_CLIENT_SECRET=<secret> \
    MCP_PUBLIC_URL=https://airhost-mcp-134107996490.asia-northeast1.run.app \
    .venv/bin/python scripts/oauth_smoke.py

    # ローカルサーバーに対して実行（DEV_DISABLE_AUTH=true で起動中の場合は --no-auth）
    MCP_PUBLIC_URL=http://localhost:8080 \
    .venv/bin/python scripts/oauth_smoke.py [--no-auth]

    # 既存トークンで MCP だけ試す
    .venv/bin/python scripts/oauth_smoke.py --token <jwt>

各ステップを ✅ / ❌ で表示し、失敗したステップで詳細を出力して終了する。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import urllib.parse
from typing import Any

import httpx


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _step(label: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {label}")
    print(f"{'─'*60}")


def _ok(msg: str) -> None:
    print(f"  ✅ {msg}")


def _fail(msg: str) -> None:
    print(f"  ❌ {msg}", file=sys.stderr)


def _info(key: str, value: Any) -> None:
    val_str = str(value)
    if len(val_str) > 120:
        val_str = val_str[:120] + "..."
    print(f"     {key}: {val_str}")


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-auth", action="store_true", help="MCP テストのみ（認証なし、DEV_DISABLE_AUTH=true 用）")
    parser.add_argument("--token", help="既存の JWT アクセストークンを使って MCP テストのみ実行")
    args = parser.parse_args()

    base_url = os.environ.get("MCP_PUBLIC_URL", "http://localhost:8080").rstrip("/")
    client_id = os.environ.get("MCP_CLIENT_ID", "airhost-mcp")
    client_secret = os.environ.get("MCP_CLIENT_SECRET", "")

    print(f"\nTarget: {base_url}")
    print(f"Client ID: {client_id}")
    print(f"Client Secret: {'(set)' if client_secret else '(not set)'}")

    access_token: str | None = args.token

    # ------------------------------------------------------------------
    # 既存トークン or --no-auth の場合はOAuthをスキップ
    # ------------------------------------------------------------------
    if args.no_auth:
        print("\n⚠️  --no-auth: OAuth をスキップして MCP テストのみ実行")
        return _test_mcp(base_url, token=None)

    if access_token:
        print(f"\n⚠️  --token: 指定されたトークンで MCP テストのみ実行")
        return _test_mcp(base_url, token=access_token)

    if not client_secret:
        _fail("MCP_CLIENT_SECRET が未設定です。")
        print("     export MCP_CLIENT_SECRET=<secret>", file=sys.stderr)
        return 2

    # ------------------------------------------------------------------
    # Step 1: well-known
    # ------------------------------------------------------------------
    _step("Step 1: .well-known/oauth-protected-resource")
    with httpx.Client(timeout=10) as http:
        r = http.get(f"{base_url}/.well-known/oauth-protected-resource")
    if r.status_code != 200:
        _fail(f"HTTP {r.status_code}: {r.text[:200]}")
        return 1
    pr = r.json()
    _ok(f"HTTP {r.status_code}")
    _info("resource", pr.get("resource"))
    _info("authorization_servers", pr.get("authorization_servers"))

    _step("Step 2: .well-known/oauth-authorization-server")
    with httpx.Client(timeout=10) as http:
        r = http.get(f"{base_url}/.well-known/oauth-authorization-server")
    if r.status_code != 200:
        _fail(f"HTTP {r.status_code}: {r.text[:200]}")
        return 1
    as_meta = r.json()
    _ok(f"HTTP {r.status_code}")
    _info("authorization_endpoint", as_meta.get("authorization_endpoint"))
    _info("token_endpoint", as_meta.get("token_endpoint"))
    _info("registration_endpoint", as_meta.get("registration_endpoint"))

    # ------------------------------------------------------------------
    # Step 2b: /oidc/register (DCR)
    # ------------------------------------------------------------------
    _step("Step 3: POST /oidc/register (DCR)")
    redirect_uri = "http://localhost:9999/callback"
    dcr_body = {
        "client_name": "oauth-smoke-test",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
    }
    with httpx.Client(timeout=10) as http:
        r = http.post(
            as_meta.get("registration_endpoint", f"{base_url}/oidc/register"),
            json=dcr_body,
        )
    if r.status_code not in (200, 201):
        _fail(f"HTTP {r.status_code}: {r.text[:200]}")
        return 1
    dcr_resp = r.json()
    registered_client_id = dcr_resp.get("client_id", client_id)
    _ok(f"HTTP {r.status_code}")
    _info("client_id returned", registered_client_id)
    if registered_client_id != client_id:
        _fail(f"DCR が返した client_id ({registered_client_id}) と MCP_CLIENT_ID ({client_id}) が不一致")
        return 1

    # ------------------------------------------------------------------
    # Step 3: /oauth/authorize
    # ------------------------------------------------------------------
    _step("Step 4: GET /oauth/authorize (Authorization Code + PKCE)")
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": "offline_access",
    }
    auth_url = as_meta.get("authorization_endpoint", f"{base_url}/oauth/authorize")
    with httpx.Client(timeout=10, follow_redirects=False) as http:
        r = http.get(auth_url, params=auth_params)

    if r.status_code != 302:
        _fail(f"302 リダイレクトを期待しましたが HTTP {r.status_code}")
        _info("body", r.text[:300])
        return 1
    location = r.headers.get("location", "")
    _ok(f"HTTP {r.status_code} → {location[:100]}")
    parsed = urllib.parse.urlparse(location)
    qs = urllib.parse.parse_qs(parsed.query)
    if "error" in qs:
        _fail(f"authorize error: {qs['error']} / {qs.get('error_description')}")
        return 1
    code = qs.get("code", [None])[0]
    returned_state = qs.get("state", [None])[0]
    if not code:
        _fail("code がリダイレクト URL に含まれていません")
        _info("location", location)
        return 1
    if returned_state != state:
        _fail(f"state mismatch: expected={state} got={returned_state}")
        return 1
    _ok(f"code 取得成功: {code[:16]}...")
    _info("state match", "✓")

    # ------------------------------------------------------------------
    # Step 4: /oauth/token
    # ------------------------------------------------------------------
    _step("Step 5: POST /oauth/token (code exchange)")
    token_url = as_meta.get("token_endpoint", f"{base_url}/oauth/token")
    token_body = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }
    with httpx.Client(timeout=10) as http:
        r = http.post(token_url, data=token_body)
    if r.status_code != 200:
        _fail(f"HTTP {r.status_code}: {r.text[:300]}")
        return 1
    token_resp = r.json()
    access_token = token_resp.get("access_token")
    refresh_token = token_resp.get("refresh_token")
    expires_in = token_resp.get("expires_in")
    if not access_token:
        _fail("access_token がレスポンスに含まれていません")
        _info("response", json.dumps(token_resp, indent=2))
        return 1
    _ok("access_token 取得成功")
    _info("token prefix", access_token[:40] + "...")
    _info("refresh_token present", bool(refresh_token))
    _info("expires_in", f"{expires_in}s ({expires_in // 86400 if expires_in else '?'}日)")

    # ------------------------------------------------------------------
    # Step 5: refresh token
    # ------------------------------------------------------------------
    if refresh_token:
        _step("Step 6: POST /oauth/token (refresh_token grant)")
        refresh_body = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        with httpx.Client(timeout=10) as http:
            r = http.post(token_url, data=refresh_body)
        if r.status_code != 200:
            _fail(f"HTTP {r.status_code}: {r.text[:300]}")
            # リフレッシュ失敗は警告扱い（続行）
            print("  ⚠️  リフレッシュトークンのテスト失敗（続行）")
        else:
            new_token_resp = r.json()
            new_access_token = new_token_resp.get("access_token")
            _ok(f"新しい access_token 取得成功: {new_access_token[:40] if new_access_token else '?'}...")
            # 以降は新しいトークンを使う
            access_token = new_access_token or access_token

    # ------------------------------------------------------------------
    # Step 6: MCP initialize
    # ------------------------------------------------------------------
    return _test_mcp(base_url, token=access_token)


def _test_mcp(base_url: str, token: str | None) -> int:
    _step(f"Step 7: POST /mcp (initialize){' [no-auth]' if token is None else ''}")
    mcp_url = base_url.rstrip("/") + "/mcp"
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    init_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "oauth-smoke", "version": "0.1.0"},
        },
    }
    with httpx.Client(timeout=15) as http:
        r = http.post(mcp_url, json=init_payload, headers=headers)

    _info("HTTP status", r.status_code)
    if r.status_code == 401:
        _fail("401 Unauthorized — トークンが無効または期限切れ")
        _info("WWW-Authenticate", r.headers.get("www-authenticate", "(なし)"))
        return 1
    if r.status_code not in (200, 202):
        _fail(f"HTTP {r.status_code}: {r.text[:300]}")
        return 1

    session_id = r.headers.get("mcp-session-id", "")
    _ok(f"MCP initialize 成功")
    _info("mcp-session-id", session_id or "(なし)")

    # tools/list
    _step("Step 8: POST /mcp (tools/list)")
    tools_payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    }
    if session_id:
        headers["mcp-session-id"] = session_id

    with httpx.Client(timeout=15) as http:
        r = http.post(mcp_url, json=tools_payload, headers=headers)

    if r.status_code not in (200, 202):
        _fail(f"HTTP {r.status_code}: {r.text[:300]}")
        return 1

    try:
        body = r.json()
        tools = body.get("result", {}).get("tools", [])
        _ok(f"tools/list 成功 — {len(tools)} ツール取得")
        for t in tools:
            _info("  tool", t.get("name"))
    except Exception:
        _ok(f"HTTP {r.status_code} (SSE形式)")
        _info("body preview", r.text[:200])

    print(f"\n{'='*60}")
    print("  🎉 全ステップ成功")
    print(f"{'='*60}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
