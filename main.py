"""
바티인베스트 텔레그램 관리 서버
Railway에 배포하여 대시보드에서 HTTP로 호출합니다.
"""

import os
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors.rpcerrorlist import ChatAdminRequiredError, UserNotParticipantError
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import (
    ChannelParticipantBanned,
    UserStatusOnline, UserStatusOffline,
    UserStatusRecently, UserStatusLastWeek,
    UserStatusLastMonth, UserStatusEmpty,
)
from supabase import create_client, Client

# ── 환경변수 ──
API_ID         = int(os.environ["TG_API_ID"])
API_HASH       = os.environ["TG_API_HASH"]
SESSION_STRING = os.environ["TG_SESSION_STRING"]
SB_URL         = os.environ["SB_URL"]
SB_SERVICE_KEY = os.environ["SB_SERVICE_KEY"]
API_SECRET     = os.environ.get("API_SECRET", "")  # 대시보드 인증용 시크릿 키

SKIP_ADMINS    = True
SKIP_BOTS      = True
ACTION_DELAY   = 0.4

app = FastAPI(title="바티인베스트 TG 관리 API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://batiinvest.github.io", "http://localhost"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 인증 ──
def verify_secret(x_api_secret: str = Header(default="")):
    if API_SECRET and x_api_secret != API_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

# ── Supabase 클라이언트 ──
def get_sb() -> Client:
    return create_client(SB_URL, SB_SERVICE_KEY)

# ── Telegram 클라이언트 (요청마다 연결) ──
async def get_tg_client() -> TelegramClient:
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.connect()
    return client

# ── 접속 상태 판별 ──
def get_last_seen(status, now: datetime) -> Optional[datetime]:
    if status is None: return None
    cls = type(status).__name__
    if cls == "UserStatusOnline":    return None  # 현재 온라인
    if cls == "UserStatusOffline":   return datetime.fromtimestamp(status.was_online, tz=timezone.utc)
    if cls == "UserStatusRecently":  return now - timedelta(days=1)
    if cls == "UserStatusLastWeek":  return now - timedelta(days=7)
    if cls == "UserStatusLastMonth": return now - timedelta(days=30)
    if cls == "UserStatusEmpty":     return now - timedelta(days=180)
    return None

# ── 모델 ──
class ScanRequest(BaseModel):
    inactive_days: int = 4
    chat_ids: Optional[List[dict]] = None  # [{"id": "...", "name": "...", "chat_id": "..."}]

class KickRequest(BaseModel):
    kick_all_inactive: bool = False
    chat_id: Optional[str] = None
    room_id: Optional[str] = None
    user_ids: Optional[List[str]] = None

# ── 헬스체크 ──
@app.get("/")
def health():
    return {"status": "ok", "service": "batiinvest-tg-manager"}

# ── 스캔 API ──
@app.post("/scan-inactive")
async def scan_inactive(req: ScanRequest, _: bool = Depends(verify_secret)):
    sb = get_sb()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=req.inactive_days)

    # 대상 채팅방 결정
    if req.chat_ids:
        rooms = req.chat_ids
    else:
        res = sb.table("rooms").select("id, name, chat_id").order("name").execute()
        rooms = res.data or []

    client = await get_tg_client()
    results = []

    try:
        for room in rooms:
            chat_id = room.get("chat_id") or room.get("id")
            room_name = room.get("name", chat_id)
            room_db_id = room.get("id")

            try:
                participants = await client.get_participants(chat_id)
                admin_ids = set()
                if SKIP_ADMINS:
                    from telethon.tl.types import ChannelParticipantsAdmins
                    admins = await client.get_participants(chat_id, filter=ChannelParticipantsAdmins())
                    admin_ids = {a.id for a in admins}

                inactive = []
                for p in participants:
                    if SKIP_BOTS and getattr(p, "bot", False): continue
                    if getattr(p, "deleted", False): continue
                    if p.id in admin_ids: continue

                    last_seen = get_last_seen(p.status, now)
                    if last_seen is None: continue  # 현재 온라인
                    if last_seen >= cutoff: continue  # 활성 유저

                    inactive.append({
                        "room_id":     room_db_id,
                        "room_name":   room_name,
                        "chat_id":     str(chat_id),
                        "user_id":     str(p.id),
                        "first_name":  p.first_name or "",
                        "last_name":   p.last_name or "",
                        "username":    p.username or "",
                        "last_seen":   last_seen.isoformat(),
                        "status_label": type(p.status).__name__ if p.status else "unknown",
                        "scanned_at":  now.isoformat(),
                        "kicked":      False,
                    })

                # DB upsert
                if inactive and room_db_id:
                    sb.table("inactive_members").upsert(
                        inactive,
                        on_conflict="room_id,user_id"
                    ).execute()

                results.append({
                    "room_name": room_name,
                    "total": len(participants),
                    "inactive": len(inactive),
                    "success": True,
                })
                await asyncio.sleep(0.5)

            except Exception as e:
                results.append({"room_name": room_name, "success": False, "error": str(e)})

    finally:
        await client.disconnect()

    total_inactive = sum(r.get("inactive", 0) for r in results)
    return {
        "ok": True,
        "scanned": len(results),
        "total_inactive": total_inactive,
        "inactive_days": req.inactive_days,
        "results": results,
    }

# ── 강퇴 API ──
@app.post("/kick-members")
async def kick_members(req: KickRequest, _: bool = Depends(verify_secret)):
    sb = get_sb()

    if req.kick_all_inactive:
        res = sb.table("inactive_members").select("room_id, chat_id, user_id").eq("kicked", False).execute()
        targets = res.data or []
    else:
        targets = [
            {"chat_id": req.chat_id, "room_id": req.room_id, "user_id": uid}
            for uid in (req.user_ids or [])
        ]

    if not targets:
        return {"ok": True, "kicked": 0, "message": "강퇴 대상 없음"}

    client = await get_tg_client()
    kicked = 0
    failed = 0
    kicked_ids = []

    try:
        for t in targets:
            try:
                await client.kick_participant(t["chat_id"], int(t["user_id"]))
                kicked_ids.append(t["user_id"])
                kicked += 1
                await asyncio.sleep(ACTION_DELAY)
            except Exception as e:
                print(f"강퇴 실패 user={t['user_id']} chat={t['chat_id']}: {e}")
                failed += 1
    finally:
        await client.disconnect()

    # DB 강퇴 완료 표시
    if kicked_ids:
        sb.table("inactive_members").update({
            "kicked": True,
            "kicked_at": datetime.now(timezone.utc).isoformat()
        }).in_("user_id", kicked_ids).execute()

    return {"ok": True, "kicked": kicked, "failed": failed, "total": len(targets)}
