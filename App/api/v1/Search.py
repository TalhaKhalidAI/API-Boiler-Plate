from fastapi import APIRouter, Depends, Query
from App.core.Connector import get_db
from App.services.search_service import SearchService

router = APIRouter(prefix="/search", tags=["Search"])


@router.get("")
async def search(
    q: str = Query(..., min_length=1, max_length=100),
    limit_videos: int = Query(20, ge=1, le=50),
    limit_channels: int = Query(10, ge=1, le=30),
    limit_tags: int = Query(10, ge=1, le=30),
    db=Depends(get_db),
):
    return await SearchService(db).search(
        q, limit_videos=limit_videos,
        limit_channels=limit_channels, limit_tags=limit_tags,
    )