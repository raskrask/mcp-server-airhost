"""
宿泊者名簿 完了通知ジョブ

MCP サーバー（Streamable HTTP）を呼び出し、Seamuの未来予約を走査。
is_complete=True かつ未通知の予約を LINE Messaging API で送信する。
通知済みフラグは GCS に JSON ファイルとして保存する。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import urllib.parse
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from google.cloud import storage

# ── ロギング ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger(__name__)

# ── 設定 ──────────────────────────────────────────────────────────────────────
MCP_BASE_URL = os.environ["MCP_PUBLIC_URL"].rstrip("/")
MCP_URL = MCP_BASE_URL + "/mcp"
MCP_CLIENT_ID = os.environ["MCP_CLIENT_ID"]
MCP_CLIENT_SECRET = os.environ["MCP_CLIENT_SECRET"]
LINE_CHANNEL_TOKEN = os.environ["LINE_CHANNEL_TOKEN"]
LINE_USER_IDS = os.environ["LINE_USER_IDS"].split(",")  # カンマ区切り
LISTING_IDS = os.environ["LISTING_IDS"].split(",")      # カンマ区切りで複数可
LOOKAHEAD_DAYS = int(os.getenv("LOOKAHEAD_DAYS", "60"))
GCS_BUCKET = os.environ["GCS_BUCKET"]
GCS_PREFIX = os.getenv("GCS_NOTIFIER_PREFIX", "airhost-notifier/notified/")
REMINDER_RUN = os.getenv("REMINDER_RUN", "false").lower() == "true"  # Cloud Scheduler側で実行ごとに指定
REMINDER_DAYS_BEFORE = int(os.getenv("REMINDER_DAYS_BEFORE", "0"))  # チェックイン何日前から催促するか（0=当日のみ）
JST = ZoneInfo("Asia/Tokyo")


# ── GCS 通知ステータス ─────────────────────────────────────────────────────────

def _blob_name(reservation_id: str) -> str:
    return f"{GCS_PREFIX}{reservation_id}.json"


def is_notified(bucket: storage.Bucket, reservation_id: str) -> bool:
    return bucket.blob(_blob_name(reservation_id)).exists()


def mark_notified(bucket: storage.Bucket, reservation_id: str, info: dict) -> None:
    blob = bucket.blob(_blob_name(reservation_id))
    blob.upload_from_string(
        json.dumps(info, ensure_ascii=False, default=str),
        content_type="application/json",
    )


# ── OAuth トークン取得（oauth_smoke.py と同じ PKCE フロー） ──────────────────

def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


async def fetch_access_token(client: httpx.AsyncClient) -> str:
    redirect_uri = "http://localhost:9999/callback"
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    auth_params = {
        "response_type": "code",
        "client_id": MCP_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": "offline_access",
    }
    r = await client.get(
        f"{MCP_BASE_URL}/oauth/authorize",
        params=auth_params,
        follow_redirects=False,
    )
    if r.status_code != 302:
        r.raise_for_status()
    location = r.headers["location"]
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    code = qs["code"][0]

    r = await client.post(
        f"{MCP_BASE_URL}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": MCP_CLIENT_ID,
            "client_secret": MCP_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
        },
    )
    r.raise_for_status()
    return r.json()["access_token"]


# ── MCP 呼び出し ──────────────────────────────────────────────────────────────

async def call_tool(client: httpx.AsyncClient, tool: str, args: dict):
    """MCP Streamable HTTP で tool を呼び出し、structuredContent があればそれを返す。"""
    resp = await client.post(MCP_URL, json={
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }, timeout=120)
    resp.raise_for_status()

    # SSE または JSON どちらでも data: 行から JSON を取り出す
    body = resp.text
    rpc = None
    for line in body.splitlines():
        if line.startswith("data:"):
            rpc = json.loads(line[5:].strip())
            break
    if rpc is None:
        rpc = resp.json()

    if "error" in rpc:
        raise RuntimeError(f"MCP error [{tool}]: {rpc['error']}")

    mcp_result = rpc["result"]
    if mcp_result.get("isError"):
        raise RuntimeError(f"tool error [{tool}]: {mcp_result}")

    # structuredContent.result があれば使う（パースが確実）
    structured = mcp_result.get("structuredContent", {})
    if "result" in structured:
        return structured["result"]

    # フォールバック: content[].text を JSON パース してリスト化
    items = []
    for item in mcp_result.get("content", []):
        text = item.get("text", "")
        try:
            items.append(json.loads(text))
        except json.JSONDecodeError:
            items.append(text)
    return items


# ── LINE Messaging API ────────────────────────────────────────────────────────

async def send_line_message(client: httpx.AsyncClient, user_id: str, text: str) -> None:
    resp = await client.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {LINE_CHANNEL_TOKEN}"},
        json={"to": user_id, "messages": [{"type": "text", "text": text}]},
        timeout=10,
    )
    resp.raise_for_status()


def build_message(listing_name: str, res: dict, reg: dict) -> str:
    photo = "あり" if reg.get("main_guest_id_photo_url") else "なし"
    lines = [
        "【宿泊者名簿 完了】",
        f"物件: {listing_name}",
    ]
    if res.get("channel"):
        lines.append(f"予約元: {res['channel']}")
    lines += [
        f"代表者: {reg['main_guest_name']}",
        f"チェックイン: {res['check_in']}",
        f"チェックアウト: {res['check_out']}",
        f"人数: {res['guests']}名",
        f"名簿: {reg['completed_count']}/{reg['guest_count']}名 完了",
        f"本人確認書類: {photo}",
    ]
    return "\n".join(lines)


def build_reminder_message(listing_name: str, res: dict, reg: dict) -> str:
    photo = "あり" if reg.get("main_guest_id_photo_url") else "なし"
    lines = [
        "【宿泊者名簿 未完了・本日チェックイン】",
        f"物件: {listing_name}",
    ]
    if res.get("channel"):
        lines.append(f"予約元: {res['channel']}")
    lines += [
        f"代表者: {reg['main_guest_name']}",
        f"チェックイン: {res['check_in']}",
        f"人数: {res['guests']}名",
        f"名簿: {reg['completed_count']}/{reg['guest_count']}名 完了",
        f"本人確認書類: {photo}",
        "→ ゲストに入力を催促してください",
    ]
    return "\n".join(lines)


# ── メイン処理 ────────────────────────────────────────────────────────────────

async def run() -> None:
    gcs = storage.Client()
    bucket = gcs.bucket(GCS_BUCKET)

    async with httpx.AsyncClient() as client:
        token = await fetch_access_token(client)
        log.info("アクセストークン取得成功")
        client.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        })

        # MCP セッション初期化
        init_resp = await client.post(MCP_URL, json={
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "airhost-notifier", "version": "0.1.0"},
            },
        }, timeout=30)
        init_resp.raise_for_status()
        session_id = init_resp.headers.get("mcp-session-id", "")
        if session_id:
            client.headers["mcp-session-id"] = session_id
        log.info("MCP セッション確立: %s", session_id or "(なし)")

        # リスティング名マップ取得
        listings = await call_tool(client, "list_listings", {})
        listing_name_map = {l["listing_id"]: l["name"] for l in listings}

        # 未来の予約一覧（全対象リスティング）
        today = datetime.now(JST).date()
        end = today + timedelta(days=LOOKAHEAD_DAYS)
        reservations = []
        for lid in LISTING_IDS:
            res_list = await call_tool(client, "list_reservations_in_range", {
                "start_date": today.isoformat(),
                "end_date": end.isoformat(),
                "listing_id": lid.strip(),
            })
            reservations.extend(res_list)

        log.info("対象予約数: %d", len(reservations))

        for res in reservations:
            rid = res["reservation_id"]

            if is_notified(bucket, rid):
                log.debug("通知済み: %s", rid)
                continue

            try:
                reg_raw = await call_tool(client, "get_guest_registration", {"booking_id": rid})
                reg = reg_raw[0] if isinstance(reg_raw, list) else reg_raw
            except Exception as e:
                log.warning("get_guest_registration 失敗 %s: %s", rid, e)
                continue

            if not reg.get("is_complete"):
                days_until_checkin = (date.fromisoformat(res["check_in"]) - today).days
                is_within_reminder_window = 0 <= days_until_checkin <= REMINDER_DAYS_BEFORE
                if REMINDER_RUN and is_within_reminder_window:
                    listing_name = listing_name_map.get(res["listing_id"], res["listing_id"])
                    message = build_reminder_message(listing_name, res, reg)
                    for uid in LINE_USER_IDS:
                        await send_line_message(client, uid.strip(), message)
                    log.info("催促LINE通知送信: %s → %d名", rid, len(LINE_USER_IDS))
                else:
                    log.debug("未完了 %s (progress=%s)", rid, reg.get("overall_progress"))
                continue

            listing_name = listing_name_map.get(res["listing_id"], res["listing_id"])
            message = build_message(listing_name, res, reg)
            for uid in LINE_USER_IDS:
                await send_line_message(client, uid.strip(), message)
            log.info("LINE通知送信: %s → %d名", rid, len(LINE_USER_IDS))

            mark_notified(bucket, rid, {
                "listing_id": res["listing_id"],
                "guest_name": reg.get("main_guest_name"),
                "check_in": res["check_in"],
            })

    log.info("完了")


if __name__ == "__main__":
    asyncio.run(run())
