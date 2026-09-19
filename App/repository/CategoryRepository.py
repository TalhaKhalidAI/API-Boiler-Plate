# App/repository/CategoryRepository.py

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, func, or_
from sqlalchemy.orm import selectinload, joinedload
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from typing import Optional, List
from datetime import datetime, timezone

from App.core.LoggingInit import core_logger
from App.core.exceptions import (
    CategoryNotFoundError,
    DuplicateCategoryError,
    InvalidCategoryParentError,
    InfrastructureError,
)
from App.api.databases.MigrateTable import (
    Category,
    VideoCategory,
    Video,
)


class CategoryRepository:
    """
    Repository for the Category aggregate.

    Owns: categories, video_categories.

    Transaction policy:
        This repository does NOT commit. The service layer owns
        transaction boundaries.

    Hierarchy policy:
        Categories form a tree via `parent_id`. The parent must exist
        and must not create a cycle (a category cannot be its own
        ancestor). Depth is bounded in app code — see MAX_DEPTH below.

    Deletion policy:
        Categories are hard-deleted (no `deleted_at` column). Deleting a
        parent sets `parent_id = NULL` on its children at the DB level
        (ON DELETE SET NULL). Children become root categories.

    Attachment policy:
        `video_categories` is a junction table. It belongs to this
        aggregate but its rows are created/destroyed by the Video
        service when videos are tagged. This repo exposes the read
        side and a cleanup helper.
    """

    # Maximum nesting depth for categories. Enforced in app code
    # because Postgres recursive CTEs can't easily enforce a limit
    # at the constraint level. Root = depth 0.
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

    async def _get_or_raise(self, category_id: int) -> Category:
        stmt = select(Category).where(Category.id == category_id)
        result = await self.session.execute(stmt)
        category = result.scalar_one_or_none()
        if category is None:
            raise CategoryNotFoundError(f"Category {category_id} not found")
        return category

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    async def _get_ancestor_ids(self, category_id: int) -> set[int]:
        """
        Walk up the parent chain and return the set of ancestor IDs
        (not including `category_id` itself). Used for cycle detection
        when re-parenting.
        """
        ancestors: set[int] = set()
        current_id = category_id
        # Bounded walk — categories can't be deeper than MAX_DEPTH + safety.
        for _ in range(self.MAX_DEPTH + 5):
            stmt = select(Category.parent_id).where(Category.id == current_id)
            result = await self.session.execute(stmt)
            parent_id = result.scalar_one_or_none()
            if parent_id is None:
                break
            if parent_id in ancestors:
                # Already-seen ancestor means the existing tree has a cycle
                # (shouldn't happen if we enforce on write, but be defensive).
                raise InvalidCategoryParentError(
                    "Existing category tree contains a cycle"
                )
            ancestors.add(parent_id)
            current_id = parent_id
        return ancestors

    async def _compute_depth(self, category_id: int) -> int:
        """Depth of a category = number of ancestors above it."""
        return len(await self._get_ancestor_ids(category_id))

    # =================================================================
    # CATEGORY — READ
    # =================================================================

    async def get_by_id(self, category_id: int) -> Optional[Category]:
        stmt = select(Category).where(Category.id == category_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> Optional[Category]:
        stmt = select(Category).where(Category.slug == slug)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_with_children(self, category_id: int) -> Optional[Category]:
        """Eager-load direct children (one level deep)."""
        stmt = (
            select(Category)
            .where(Category.id == category_id)
            .options(selectinload(Category.children))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_with_parent(self, category_id: int) -> Optional[Category]:
        """Eager-load the parent chain root (one level up)."""
        stmt = (
            select(Category)
            .where(Category.id == category_id)
            .options(joinedload(Category.parent))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_roots(
        self, *, active_only: bool = True
    ) -> List[Category]:
        """Top-level categories (parent_id IS NULL)."""
        stmt = select(Category).where(Category.parent_id.is_(None))
        if active_only:
            stmt = stmt.where(Category.is_active.is_(True))
        stmt = stmt.order_by(Category.display_order.asc(), Category.name.asc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_children(
        self, parent_id: int, *, active_only: bool = True
    ) -> List[Category]:
        """Direct children of a category."""
        stmt = select(Category).where(Category.parent_id == parent_id)
        if active_only:
            stmt = stmt.where(Category.is_active.is_(True))
        stmt = stmt.order_by(Category.display_order.asc(), Category.name.asc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_all(
        self,
        *,
        active_only: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Category]:
        """Flat list of categories. For admin views."""
        stmt = select(Category)
        if active_only:
            stmt = stmt.where(Category.is_active.is_(True))
        stmt = (
            stmt.order_by(Category.display_order.asc(), Category.name.asc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_all(self, *, active_only: bool = True) -> int:
        stmt = select(func.count()).select_from(Category)
        if active_only:
            stmt = stmt.where(Category.is_active.is_(True))
        return (await self.session.execute(stmt)).scalar_one()

    # =================================================================
    # CATEGORY — TREE HELPERS
    # =================================================================

    async def get_descendant_ids(self, category_id: int) -> set[int]:
        """
        All descendants (children, grandchildren, ...) of a category,
        NOT including the category itself. Uses a single recursive CTE.
        """
        from sqlalchemy import text as sql_text
        sql = sql_text("""
            WITH RECURSIVE tree AS (
                SELECT id FROM categories WHERE parent_id = :root_id
                UNION ALL
                SELECT c.id FROM categories c
                INNER JOIN tree t ON c.parent_id = t.id
            )
            SELECT id FROM tree
        """)
        result = await self.session.execute(sql, {"root_id": category_id})
        return {row[0] for row in result.all()}

    async def get_ancestor_ids(self, category_id: int) -> set[int]:
        """Public version of the ancestor-walk helper."""
        return await self._get_ancestor_ids(category_id)

    # =================================================================
    # CATEGORY — WRITE
    # =================================================================

    async def create(
        self,
        *,
        slug: str,
        name: str,
        description: Optional[str] = None,
        icon_key: Optional[str] = None,
        parent_id: Optional[int] = None,
        display_order: int = 0,
        is_active: bool = True,
    ) -> Category:
        """
        Create a category. Validates:
          - slug uniqueness
          - parent exists (if provided)
          - resulting depth would not exceed MAX_DEPTH
        """
        if await self.get_by_slug(slug) is not None:
            raise DuplicateCategoryError(f"Category slug '{slug}' already exists")

        if parent_id is not None:
            parent = await self.get_by_id(parent_id)
            if parent is None:
                raise InvalidCategoryParentError(
                    f"Parent category {parent_id} not found"
                )
            parent_depth = await self._compute_depth(parent_id)
            if parent_depth + 1 > self.MAX_DEPTH:
                raise InvalidCategoryParentError(
                    f"Cannot nest deeper than {self.MAX_DEPTH} levels"
                )

        category = Category(
            slug=slug,
            name=name,
            description=description,
            icon_key=icon_key,
            parent_id=parent_id,
            display_order=display_order,
            is_active=is_active,
        )
        self.session.add(category)
        await self._safe_flush()
        await self.session.refresh(category)
        return category

    async def update(
        self,
        category_id: int,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        icon_key: Optional[str] = None,
        display_order: Optional[int] = None,
        is_active: Optional[bool] = None,
    ) -> Category:
        """
        Update mutable fields. Slug and parent_id are NOT changed here —
        they have dedicated methods because they affect tree structure
        and uniqueness.
        """
        category = await self._get_or_raise(category_id)
        if name is not None:
            category.name = name
        if description is not None:
            category.description = description
        if icon_key is not None:
            category.icon_key = icon_key
        if display_order is not None:
            category.display_order = display_order
        if is_active is not None:
            category.is_active = is_active
        await self._safe_flush()
        return category

    async def update_slug(self, category_id: int, new_slug: str) -> Category:
        category = await self._get_or_raise(category_id)
        if category.slug == new_slug:
            return category

        existing = await self.get_by_slug(new_slug)
        if existing is not None and existing.id != category_id:
            raise DuplicateCategoryError(f"Category slug '{new_slug}' already exists")

        category.slug = new_slug
        await self._safe_flush()
        return category

    async def set_parent(
        self, category_id: int, new_parent_id: Optional[int]
    ) -> Category:
        """
        Re-parent a category. Validates:
          - new parent exists (if provided)
          - new parent is not a descendant of this category
            (would create a cycle)
          - new parent is not this category itself
          - resulting depth would not exceed MAX_DEPTH
        """
        category = await self._get_or_raise(category_id)

        if new_parent_id is None:
            category.parent_id = None
            await self._safe_flush()
            return category

        if new_parent_id == category_id:
            raise InvalidCategoryParentError("A category cannot be its own parent")

        new_parent = await self.get_by_id(new_parent_id)
        if new_parent is None:
            raise InvalidCategoryParentError(
                f"Parent category {new_parent_id} not found"
            )

        # Cycle check: the new parent must not be a descendant of this category.
        descendants = await self.get_descendant_ids(category_id)
        if new_parent_id in descendants:
            raise InvalidCategoryParentError(
                "Cannot move a category under its own descendant (cycle)"
            )

        # Depth check: resulting depth = parent_depth + 1.
        parent_depth = await self._compute_depth(new_parent_id)
        if parent_depth + 1 > self.MAX_DEPTH:
            raise InvalidCategoryParentError(
                f"Cannot nest deeper than {self.MAX_DEPTH} levels"
            )

        category.parent_id = new_parent_id
        await self._safe_flush()
        return category

    async def set_active(self, category_id: int, active: bool) -> Category:
        category = await self._get_or_raise(category_id)
        category.is_active = active
        await self._safe_flush()
        return category

    async def delete(self, category_id: int) -> None:
        """
        Hard-delete a category.

        FK constraint on `categories.parent_id` is ON DELETE SET NULL,
        so children become root categories automatically.

        `video_categories` rows referencing this category are also
        removed via ON DELETE CASCADE.
        """
        result = await self.session.execute(
            delete(Category)
            .where(Category.id == category_id)
            .returning(Category.id)
        )
        if result.scalar_one_or_none() is None:
            raise CategoryNotFoundError(f"Category {category_id} not found")

    # =================================================================
    # VIDEO ↔ CATEGORY — attachments
    # =================================================================

    async def attach_to_video(
        self, video_id: int, category_ids: List[int]
    ) -> None:
        """
        Attach one or more categories to a video.
        Idempotent — uses ON CONFLICT DO NOTHING on the junction.
        """
        if not category_ids:
            return

        # Validate that all categories exist first
        result = await self.session.execute(
            select(Category.id).where(Category.id.in_(category_ids))
        )
        existing = {row[0] for row in result.all()}
        missing = set(category_ids) - existing
        if missing:
            raise CategoryNotFoundError(
                f"Categories not found: {sorted(missing)}"
            )

        stmt = (
            pg_insert(VideoCategory)
            .values(
                [{"video_id": video_id, "category_id": cid} for cid in category_ids]
            )
            .on_conflict_do_nothing(index_elements=["video_id", "category_id"])
        )
        await self.session.execute(stmt)

    async def detach_from_video(
        self, video_id: int, category_ids: List[int]
    ) -> None:
        if not category_ids:
            return
        await self.session.execute(
            delete(VideoCategory).where(
                VideoCategory.video_id == video_id,
                VideoCategory.category_id.in_(category_ids),
            )
        )

    async def set_for_video(
        self, video_id: int, category_ids: List[int]
    ) -> None:
        """
        Replace all categories for a video.
        Validates before mutating so a bad input can't leave the video
        in a half-updated state.
        """
        if category_ids:
            result = await self.session.execute(
                select(Category.id).where(Category.id.in_(category_ids))
            )
            existing = {row[0] for row in result.all()}
            missing = set(category_ids) - existing
            if missing:
                raise CategoryNotFoundError(
                    f"Categories not found: {sorted(missing)}"
                )

        await self.session.execute(
            delete(VideoCategory).where(VideoCategory.video_id == video_id)
        )
        if category_ids:
            stmt = (
                pg_insert(VideoCategory)
                .values(
                    [{"video_id": video_id, "category_id": cid} for cid in category_ids]
                )
                .on_conflict_do_nothing(index_elements=["video_id", "category_id"])
            )
            await self.session.execute(stmt)

    async def list_videos_in_category(
        self,
        category_id: int,
        *,
        include_subcategories: bool = False,
        only_public: bool = True,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Video]:
        """
        Videos attached to a category (read-only cross-aggregate helper).

        If `include_subcategories` is True, walks the tree and includes
        videos from all descendants.
        """
        category_ids: List[int] = [category_id]
        if include_subcategories:
            descendants = await self.get_descendant_ids(category_id)
            category_ids.extend(descendants)

        stmt = (
            select(Video)
            .join(VideoCategory, VideoCategory.video_id == Video.id)
            .where(
                VideoCategory.category_id.in_(category_ids),
                Video.deleted_at.is_(None),
            )
            .order_by(
                Video.published_at.desc().nullslast(),
                Video.created_at.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        if only_public:
            stmt = stmt.where(
                Video.visibility == "public",
                Video.status == "ready",
            )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_videos_in_category(
        self,
        category_id: int,
        *,
        include_subcategories: bool = False,
        only_public: bool = True,
    ) -> int:
        category_ids: List[int] = [category_id]
        if include_subcategories:
            descendants = await self.get_descendant_ids(category_id)
            category_ids.extend(descendants)

        stmt = (
            select(func.count())
            .select_from(Video)
            .join(VideoCategory, VideoCategory.video_id == Video.id)
            .where(
                VideoCategory.category_id.in_(category_ids),
                Video.deleted_at.is_(None),
            )
        )
        if only_public:
            stmt = stmt.where(
                Video.visibility == "public",
                Video.status == "ready",
            )
        return (await self.session.execute(stmt)).scalar_one()

    async def list_categories_for_video(self, video_id: int) -> List[Category]:
        """Categories attached to a video, ordered by display_order."""
        stmt = (
            select(Category)
            .join(VideoCategory, VideoCategory.category_id == Category.id)
            .where(VideoCategory.video_id == video_id)
            .order_by(Category.display_order.asc(), Category.name.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())