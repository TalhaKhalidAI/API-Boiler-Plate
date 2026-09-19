# App/repository/CommentRepository.py

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, func, or_, text as sql_text
from sqlalchemy.orm import selectinload, joinedload
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from typing import Optional, List
from datetime import datetime, timezone

from App.core.LoggingInit import core_logger
from App.core.exceptions import (
    CommentNotFoundError,
    CommentAlreadyDeletedError,
    CommentDepthExceededError,
    InvalidCommentParentError,
    CommentLikeNotFoundError,
    InfrastructureError,
)
from App.api.databases.MigrateTable import (
    Comment,
    CommentLike,
)


class CommentRepository:
    """
    Repository for the Comment aggregate.

    Owns: comments, comment_likes.

    Transaction policy:
        This repository does NOT commit. The service layer owns
        transaction boundaries.

    Deletion policy:
        Comments are soft-deleted via `deleted_at`. `get_*` methods
        filter on `deleted_at IS NULL` by default. `include_deleted=True`
        is available where you need to inspect historical state (e.g.
        moderation tools).

    Threading policy:
        Comments form a tree via `parent_comment_id`. Max depth is
        enforced in app code (MAX_DEPTH below). The DB CHECK constraint
        `ck_comments_depth` (0..3) is the safety net.

    Counter policy:
        `like_count` and `reply_count` are denormalized on `Comment`.
        This repo exposes atomic increments/decrements; the service is
        responsible for calling them in the same transaction as the
        underlying insert/delete.

    Hard-delete policy:
        `hard_delete` exists but should only be used by moderation
        tooling and cascade tests. Normal flows use `soft_delete`.
    """

    # Maximum reply depth. Root comment = depth 0.
    # A reply to a root = depth 1. A reply to a reply = depth 2.
    # A reply to a depth-2 comment = depth 3 (last allowed).
    # No comment may be at depth > MAX_DEPTH.
    # Must match the DB CHECK constraint `ck_comments_depth` (0..3).
    MAX_DEPTH = 3

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # =================================================================
    # INTERNAL HELPERS
    # =================================================================

    async def _safe_flush(self) -> None:
        """Flush pending ORM changes, translating DB errors to domain errors."""
        try:
            await self.session.flush()
        except IntegrityError as e:
            core_logger.warning(
                f"IntegrityError on flush: {e.orig if hasattr(e, 'orig') else e}"
            )
            raise
        except SQLAlchemyError as e:
            core_logger.error(f"SQLAlchemyError on flush: {e}")
            raise InfrastructureError("Database error during flush") from e

    async def _get_or_raise(
        self, comment_id: int, *, include_deleted: bool = False
    ) -> Comment:
        stmt = select(Comment).where(Comment.id == comment_id)
        if not include_deleted:
            stmt = stmt.where(Comment.deleted_at.is_(None))
        result = await self.session.execute(stmt)
        comment = result.scalar_one_or_none()
        if comment is None:
            raise CommentNotFoundError(f"Comment {comment_id} not found")
        return comment

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    # =================================================================
    # COMMENT — READ
    # =================================================================

    async def get_by_id(
        self, comment_id: int, *, include_deleted: bool = False
    ) -> Optional[Comment]:
        stmt = select(Comment).where(Comment.id == comment_id)
        if not include_deleted:
            stmt = stmt.where(Comment.deleted_at.is_(None))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_uuid(
        self, comment_uuid: str, *, include_deleted: bool = False
    ) -> Optional[Comment]:
        stmt = select(Comment).where(Comment.comment_uuid == comment_uuid)
        if not include_deleted:
            stmt = stmt.where(Comment.deleted_at.is_(None))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_with_replies(self, comment_id: int) -> Optional[Comment]:
        """Eager-load direct replies (one level deep) for a comment thread view."""
        stmt = (
            select(Comment)
            .where(Comment.id == comment_id, Comment.deleted_at.is_(None))
            .options(selectinload(Comment.replies))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_with_user(self, comment_id: int) -> Optional[Comment]:
        """Eager-load author for detail views."""
        stmt = (
            select(Comment)
            .where(Comment.id == comment_id, Comment.deleted_at.is_(None))
            .options(joinedload(Comment.user))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_top_level_for_video(
        self,
        video_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Comment]:
        """
        Top-level comments (no parent) for a video, newest first.
        Replies are fetched separately via `list_replies`.
        """
        stmt = (
            select(Comment)
            .where(
                Comment.video_id == video_id,
                Comment.parent_comment_id.is_(None),
                Comment.deleted_at.is_(None),
            )
            .order_by(
                Comment.is_pinned.desc(),
                Comment.created_at.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_top_level_for_video(self, video_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(Comment)
            .where(
                Comment.video_id == video_id,
                Comment.parent_comment_id.is_(None),
                Comment.deleted_at.is_(None),
            )
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def list_replies(
        self,
        parent_comment_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Comment]:
        """Direct replies to a comment, oldest first (chronological)."""
        stmt = (
            select(Comment)
            .where(
                Comment.parent_comment_id == parent_comment_id,
                Comment.deleted_at.is_(None),
            )
            .order_by(Comment.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_replies(self, parent_comment_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(Comment)
            .where(
                Comment.parent_comment_id == parent_comment_id,
                Comment.deleted_at.is_(None),
            )
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def list_by_user(
        self,
        user_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Comment]:
        """A user's own comments, newest first. For their profile page."""
        stmt = (
            select(Comment)
            .where(
                Comment.user_id == user_id,
                Comment.deleted_at.is_(None),
            )
            .order_by(Comment.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_for_video(self, video_id: int) -> int:
        """All non-deleted comments on a video (top-level + replies)."""
        stmt = (
            select(func.count())
            .select_from(Comment)
            .where(
                Comment.video_id == video_id,
                Comment.deleted_at.is_(None),
            )
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def list_all_for_video(
        self,
        video_id: int,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Comment]:
        """
        Flat list of every comment (top-level and replies) for a video.
        For admin moderation views. Ordered newest first.
        """
        stmt = (
            select(Comment)
            .where(
                Comment.video_id == video_id,
                Comment.deleted_at.is_(None),
            )
            .order_by(Comment.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # =================================================================
    # COMMENT — TREE HELPERS
    # =================================================================

    async def get_descendant_ids(self, comment_id: int) -> set[int]:
        """
        All descendants (replies, replies-to-replies, ...) of a comment,
        NOT including the comment itself. Uses a single recursive CTE.
        """
        sql = sql_text("""
            WITH RECURSIVE tree AS (
                SELECT id FROM comments WHERE parent_comment_id = :root_id
                UNION ALL
                SELECT c.id FROM comments c
                INNER JOIN tree t ON c.parent_comment_id = t.id
                WHERE c.deleted_at IS NULL
            )
            SELECT id FROM tree
        """)
        result = await self.session.execute(sql, {"root_id": comment_id})
        return {row[0] for row in result.all()}

    async def get_ancestor_ids(self, comment_id: int) -> list[int]:
        """
        Ancestors of a comment, ordered from immediate parent up to root.
        Used for "reply to @user" breadcrumbs and for depth checks.
        """
        ancestors: list[int] = []
        current_id = comment_id
        # Bounded walk — comments can't be deeper than MAX_DEPTH + safety.
        for _ in range(self.MAX_DEPTH + 5):
            stmt = select(Comment.parent_comment_id).where(Comment.id == current_id)
            result = await self.session.execute(stmt)
            parent_id = result.scalar_one_or_none()
            if parent_id is None:
                break
            ancestors.append(parent_id)
            current_id = parent_id
        return ancestors

    async def _compute_depth(self, comment_id: int) -> int:
        """Depth of a comment = number of ancestors above it."""
        return len(await self.get_ancestor_ids(comment_id))

    # =================================================================
    # COMMENT — WRITE
    # =================================================================

    async def create(
        self,
        *,
        video_id: int,
        user_id: int,
        content: str,
        parent_comment_id: Optional[int] = None,
    ) -> Comment:
        """
        Create a comment. Validates:
          - if parent_comment_id given, it must exist, be non-deleted,
            and belong to the same video
          - resulting depth must not exceed MAX_DEPTH
        """
        depth = 0

        if parent_comment_id is not None:
            parent = await self._get_or_raise(parent_comment_id)

            if parent.video_id != video_id:
                raise InvalidCommentParentError(
                    f"Parent comment {parent_comment_id} belongs to a "
                    f"different video"
                )

            parent_depth = await self._compute_depth(parent_comment_id)
            depth = parent_depth + 1
            if depth > self.MAX_DEPTH:
                raise CommentDepthExceededError(
                    f"Cannot reply deeper than {self.MAX_DEPTH} levels"
                )

        comment = Comment(
            video_id=video_id,
            user_id=user_id,
            parent_comment_id=parent_comment_id,
            depth=depth,
            content=content,
            like_count=0,
            reply_count=0,
            is_pinned=False,
            is_edited=False,
        )
        self.session.add(comment)
        await self._safe_flush()

        # Bump parent's reply_count in the same transaction
        if parent_comment_id is not None:
            await self.increment_reply_count(parent_comment_id, by=1)

        await self.session.refresh(comment)
        return comment

    async def update_content(self, comment_id: int, content: str) -> Comment:
        """Edit a comment's content. Sets `is_edited = True`."""
        comment = await self._get_or_raise(comment_id)
        comment.content = content
        comment.is_edited = True
        await self._safe_flush()
        return comment

    async def soft_delete(self, comment_id: int) -> None:
        """
        Soft-delete a comment. Also decrements the parent's reply_count
        if this comment was a reply, so the parent's count stays accurate.
        """
        comment = await self._get_or_raise(comment_id, include_deleted=True)
        if comment.deleted_at is not None:
            raise CommentAlreadyDeletedError(
                f"Comment {comment_id} already deleted"
            )

        parent_id = comment.parent_comment_id
        comment.deleted_at = self._now()
        await self._safe_flush()

        if parent_id is not None:
            await self.increment_reply_count(parent_id, by=-1)

    async def hard_delete(self, comment_id: int) -> None:
        """
        Permanently remove a comment row. Children are removed by the
        DB via ON DELETE CASCADE on `parent_comment_id`.

        Use ONLY in moderation tooling and tests — the normal flow is
        soft_delete.
        """
        result = await self.session.execute(
            delete(Comment)
            .where(Comment.id == comment_id)
            .returning(Comment.id)
        )
        if result.scalar_one_or_none() is None:
            raise CommentNotFoundError(f"Comment {comment_id} not found")

    async def pin(self, comment_id: int) -> Comment:
        comment = await self._get_or_raise(comment_id)
        comment.is_pinned = True
        await self._safe_flush()
        return comment

    async def unpin(self, comment_id: int) -> Comment:
        comment = await self._get_or_raise(comment_id)
        comment.is_pinned = False
        await self._safe_flush()
        return comment

    # =================================================================
    # COMMENT — COUNTERS (atomic, worker-only)
    # =================================================================

    async def increment_like_count(self, comment_id: int, by: int = 1) -> None:
        await self.session.execute(
            update(Comment)
            .where(Comment.id == comment_id)
            .values(like_count=Comment.like_count + by)
        )

    async def increment_reply_count(self, comment_id: int, by: int = 1) -> None:
        await self.session.execute(
            update(Comment)
            .where(Comment.id == comment_id)
            .values(reply_count=Comment.reply_count + by)
        )

    # =================================================================
    # COMMENT LIKES — junction
    # =================================================================

    async def like(
        self, comment_id: int, user_id: int
    ) -> bool:
        """
        Add a like. Returns True if a new like was created,
        False if the user already liked this comment.
        Idempotent — safe to call twice.
        """
        stmt = (
            pg_insert(CommentLike)
            .values({"comment_id": comment_id, "user_id": user_id})
            .on_conflict_do_nothing(index_elements=["comment_id", "user_id"])
            .returning(CommentLike.comment_id)
        )
        result = await self.session.execute(stmt)
        created = result.scalar_one_or_none() is not None

        if created:
            await self.increment_like_count(comment_id, by=1)

        return created

    async def unlike(self, comment_id: int, user_id: int) -> bool:
        """
        Remove a like. Returns True if a like was removed,
        False if the user had not liked this comment.
        """
        result = await self.session.execute(
            delete(CommentLike)
            .where(
                CommentLike.comment_id == comment_id,
                CommentLike.user_id == user_id,
            )
            .returning(CommentLike.comment_id)
        )
        removed = result.scalar_one_or_none() is not None

        if removed:
            await self.increment_like_count(comment_id, by=-1)

        return removed

    async def has_liked(self, comment_id: int, user_id: int) -> bool:
        stmt = select(CommentLike.comment_id).where(
            CommentLike.comment_id == comment_id,
            CommentLike.user_id == user_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def list_likers(
        self,
        comment_id: int,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> List[CommentLike]:
        """Users who liked a comment, newest first."""
        stmt = (
            select(CommentLike)
            .where(CommentLike.comment_id == comment_id)
            .order_by(CommentLike.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_likes(self, comment_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(CommentLike)
            .where(CommentLike.comment_id == comment_id)
        )
        return (await self.session.execute(stmt)).scalar_one()