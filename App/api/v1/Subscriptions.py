from fastapi import APIRouter, Depends, Body
from App.core.Connector import get_db
from App.api.dependencies.auth import get_current_active_user
from App.services.subscription_service import SubscriptionService

router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])


@router.post("/{channel_id}", status_code=201)
async def subscribe(
    channel_id: int,
    notify: bool = Body(True, embed=True),
    current_user=Depends(get_current_active_user),
    db=Depends(get_db),
):
    await SubscriptionService(db).subscribe(channel_id, current_user, notify=notify)
    return {"channel_id": channel_id, "subscribed": True}


@router.delete("/{channel_id}", status_code=204)
async def unsubscribe(
    channel_id: int,
    current_user=Depends(get_current_active_user),
    db=Depends(get_db),
):
    await SubscriptionService(db).unsubscribe(channel_id, current_user)


@router.get("/mine")
async def list_my_subscriptions(
    current_user=Depends(get_current_active_user),
    db=Depends(get_db),
):
    return await SubscriptionService(db).list_my_subscriptions(current_user)