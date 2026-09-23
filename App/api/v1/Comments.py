# App/api/v1/Comments.py
"""
Comment endpoints.

    GET    /videos/{video_id}/comments       → top-level thread
    POST   /videos/{video_id}/comments       → create comment
    GET    /comments/{id}/replies            → list replies
    PATCH  /comments/{id}                    → edit
    DELETE /comments/{id}                    → soft-delete
    POST   /comments/{id}/like               → like
    DELETE /comments/{id}/like               → unlike
    POST   /comments/{id}/pin                → pin (mod/admin)
    DELETE /comments/{id}/pin                → unpin
"""
from typing import Optional
from fastapi import APIRouter, Depends, Body, Query
from sqlalchemy.ext.asyncio import AsyncSession

from App.core.Connector import get_db
from App.api.dependencies.auth import (
    get_current_active_user,
    get_current_user_optional,
)
from App.services.comment_service import CommentService
from App.services.comment_like_service import CommentLikeService

router = APIRouter(tags=["Comments"])


# ============================================================================
# THREAD READS
# ============================================================================

@router.get("/videos/{video_id}/comments")
async def list_video_comments(
    video_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Top-level comments for a video (pinned first, then newest)."""
    return await CommentService(db).list_thread(
        video_id, limit=limit, offset=offset
    )


@router.get("/comments/{comment_id}/replies")
async def list_replies(
    comment_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Replies to a comment (chronological)."""
    return await CommentService(db).list_replies(
        comment_id, limit=limit, offset=offset
    )


# ============================================================================
# WRITE
# ============================================================================

@router.post("/videos/{video_id}/comments", status_code=201)
async def create_comment(
    video_id: int,
    content: str = Body(..., embed=True),
    parent_comment_id: Optional[int] = Body(None),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a comment or reply (max depth 3)."""
    comment = await CommentService(db).create_comment(
        video_id, current_user,
        content=content, parent_comment_id=parent_comment_id,
    )
    return {"id": comment.id, "video_id": video_id}


@router.patch("/comments/{comment_id}")
async def update_comment(
    comment_id: int,
    content: str = Body(..., embed=True),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Edit a comment's content."""
    comment = await CommentService(db).update_comment(
        comment_id, current_user, content=content
    )
    return {"id": comment.id, "updated": True}


@router.delete("/comments/{comment_id}", status_code=204)
async def delete_comment(
    comment_id: int,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a comment."""
    await CommentService(db).delete_comment(comment_id, current_user)


# ============================================================================
# LIKES
# ============================================================================

@router.post("/comments/{comment_id}/like")
async def like_comment(
    comment_id: int,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Like a comment (idempotent)."""
    created = await CommentLikeService(db).like(comment_id, current_user)
    return {"comment_id": comment_id, "liked": True, "created": created}


@router.delete("/comments/{comment_id}/like")
async def unlike_comment(
    comment_id: int,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove a like (idempotent)."""
    removed = await CommentLikeService(db).unlike(comment_id, current_user)
    return {"comment_id": comment_id, "liked": False, "removed": removed}


# ============================================================================
# PIN
# ============================================================================

@router.post("/comments/{comment_id}/pin")
async def pin_comment(
    comment_id: int,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Pin a comment (channel owner / admin)."""
    comment = await CommentService(db).pin(comment_id, current_user)
    return {"id": comment.id, "is_pinned": True}


@router.delete("/comments/{comment_id}/pin")
async def unpin_comment(
    comment_id: int,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Unpin a comment."""
    comment = await CommentService(db).unpin(comment_id, current_user)
    return {"id": comment.id, "is_pinned": False}