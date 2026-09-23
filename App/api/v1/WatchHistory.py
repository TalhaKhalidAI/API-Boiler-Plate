from fastapi import APIRouter, Depends, Body
from typing import Optional
from App.core.Connector import get_db
from App.api.dependencies.auth import get_current_active_user
from App.services.watch_history_service import WatchHistoryService

router = APIRouter(prefix="/watch-history", tags=["Watch History"])


@router.post("/progress")
async def record_progress(
    video_id: int = Body(...),
    channel_id: int = Body(...),
    last_position_sec: int = Body(0),
    watched_seconds: int = Body(0),
    completed: bool = Body(False),
    current_user=Depends(get_current_active_user),
    db=Depends(get_db),
):
    return await WatchHistoryService(db).record_progress(
        current_user, video_id=video_id, channel_id=channel_id,
        last_position_sec=last_position_sec,
        watched_seconds=watched_seconds, completed=completed,
    )


@router.get("/continue")
async def continue_watching(
    limit: int = 10,
    current_user=Depends(get_current_active_user),
    db=Depends(get_db),
):
    return await WatchHistoryService(db).list_continue_watching(
        current_user, limit=limit
    )


@router.get("/mine")
async def my_history(
    limit: int = 30,
    offset: int = 0,
    current_user=Depends(get_current_active_user),
    db=Depends(get_db),
):
    return await WatchHistoryService(db).list_my_history(
        current_user, limit=limit, offset=offset
    )


@router.delete("/clear", status_code=204)
async def clear_history(
    current_user=Depends(get_current_active_user),
    db=Depends(get_db),
):
    await WatchHistoryService(db).clear_history(current_user)


@router.delete("/{video_id}", status_code=204)
async def delete_entry(
    video_id: int,
    current_user=Depends(get_current_active_user),
    db=Depends(get_db),
):
    await WatchHistoryService(db).delete_entry(video_id, current_user)