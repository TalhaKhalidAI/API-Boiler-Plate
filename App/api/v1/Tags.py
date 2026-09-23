from fastapi import APIRouter, Depends, Query, Body
from App.core.Connector import get_db
from App.api.dependencies.auth import get_current_active_user
from App.services.tag_service import TagService

router = APIRouter(prefix="/tags", tags=["Tags"])


@router.get("/popular")
async def list_popular(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
):
    return await TagService(db).list_popular(limit=limit, offset=offset)


@router.get("/search")
async def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=50),
    db=Depends(get_db),
):
    return await TagService(db).search(q, limit=limit)


@router.post("/get-or-create")
async def get_or_create(
    name: str = Body(..., embed=True),
    current_user=Depends(get_current_active_user),
    db=Depends(get_db),
):
    return await TagService(db).get_or_create(name, current_user)