from fastapi import APIRouter, Depends, Query
from App.core.Connector import get_db
from App.api.dependencies.auth import get_current_active_user
from App.services.feed_service import FeedService

router = APIRouter(prefix="/feed", tags=["Feed"])


@router.get("/home")
async def get_home_feed(
    per_channel: int = Query(5, ge=1, le=20),
    exclude_watched: bool = Query(True),
    current_user=Depends(get_current_active_user),
    db=Depends(get_db),
):
    return await FeedService(db).get_home_feed(
        current_user, per_channel=per_channel, exclude_watched=exclude_watched
    )