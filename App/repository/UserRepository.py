# App/repository/UserRepository.py

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
from typing import Optional, List, Dict, Any, Tuple
import logging
from datetime import datetime, timezone

from App.api.databases.MigrateTable import User as UserModel
from App.core.exceptions import (
    UserNotFoundError,
    DuplicateEmailError,
    DuplicateNameError,
    AccountAlreadyDisabledError,
    InfrastructureError
)
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)


class UserRepository:
    """
    Repository for user database operations.

    Transaction policy:
        This repository does NOT commit. The service layer owns
        transaction boundaries (Database.session() commits at the end
        of the request, or the caller manages it explicitly).

    Error-handling convention:
        - Lookup methods (get_by_*) propagate DB errors untouched.
          Returning None means the row genuinely does not exist.
        - Write methods raise domain errors for expected business
          conditions (DuplicateEmailError, UserNotFoundError, ...).
        - IntegrityError is translated to a domain error when we can
          identify the constraint. Otherwise it propagates so main.py's
          IntegrityError handler returns 409.
        - Infrastructure errors (OperationalError, SQLAlchemyError)
          propagate to main.py, which returns a consistent 503.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    # ================================================================
    # INTERNAL HELPERS
    # ================================================================
    async def _safe_flush(self) -> None:
        """Flush pending ORM changes. Lets IntegrityError propagate so the
        caller can translate a specific constraint violation."""
        await self.session.flush()

    # ================================================================
    # BASIC CRUD
    # ================================================================
    async def get_by_id(self, user_id: int) -> Optional[UserModel]:
        result = await self.session.execute(
            select(UserModel).where(UserModel.id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_by_ids(self, user_ids: List[int]) -> List[UserModel]:
        if not user_ids:
            return []
        result = await self.session.execute(
            select(UserModel).where(UserModel.id.in_(user_ids))
        )
        return list(result.scalars().all())

    async def get_by_email(self, email: str) -> Optional[UserModel]:
        result = await self.session.execute(
            select(UserModel).where(UserModel.email == email)
        )
        return result.scalar_one_or_none()

    async def get_by_name(self, name: str) -> Optional[UserModel]:
        result = await self.session.execute(
            select(UserModel).where(UserModel.name == name)
        )
        return result.scalar_one_or_none()

    async def create(self, user_data: Dict[str, Any]) -> UserModel:
        """Create a new user. Does NOT commit.

        Raises:
            DuplicateEmailError, DuplicateNameError on unique-constraint
            violations. Other DB failures propagate.
        """
        user = UserModel(**user_data)
        self.session.add(user)
        try:
            await self._safe_flush()
        except IntegrityError as e:
            msg = str(e.orig).lower()
            if "email" in msg and ("unique" in msg or "duplicate" in msg):
                raise DuplicateEmailError(
                    f"Email '{user_data.get('email')}' already registered"
                ) from e
            if "name" in msg and ("unique" in msg or "duplicate" in msg):
                raise DuplicateNameError(
                    f"Name '{user_data.get('name')}' already taken"
                ) from e
            raise
        await self.session.refresh(user)
        logger.info(f"Created new user: {user.email}")
        return user

    async def update(self, user_id: int, update_data: Dict[str, Any]) -> UserModel:
        user = await self.get_by_id(user_id)
        if not user:
            raise UserNotFoundError(f"User {user_id} not found")

        for field, value in update_data.items():
            if hasattr(user, field):
                setattr(user, field, value)

        try:
            await self._safe_flush()
        except IntegrityError as e:
            msg = str(e.orig).lower()
            if "email" in msg:
                raise DuplicateEmailError("Email already in use") from e
            raise
        await self.session.refresh(user)
        logger.info(f"Updated user {user_id}")
        return user

    async def update_password_hash(self, user_id: int, password_hash: str) -> bool:
        user = await self.get_by_id(user_id)
        if not user:
            raise UserNotFoundError(f"User {user_id} not found")

        user.password_hash = password_hash
        await self._safe_flush()
        logger.info(f"Updated password hash for user {user_id}")
        return True

    # ================================================================
    # ACCOUNT STATE
    # ================================================================
    async def disable_account(self, user_id: int) -> bool:
        user = await self.get_by_id(user_id)
        if not user:
            raise UserNotFoundError(f"User {user_id} not found")
        if user.disabled:
            raise AccountAlreadyDisabledError(f"User {user_id} is already disabled")

        user.disabled = True
        await self._safe_flush()
        logger.info(f"Disabled user {user_id}")
        return True

    async def restore_disable(self, user_id: int) -> bool:
        user = await self.get_by_id(user_id)
        if not user:
            raise UserNotFoundError(f"User {user_id} not found")
        if not user.disabled:
            return False

        user.disabled = False
        user.is_active = True
        await self._safe_flush()
        await self.session.refresh(user)
        logger.info(f"User {user_id} restored from disabled")
        return True

    async def full_restore(self, user_id: int) -> bool:
        user = await self.get_by_id(user_id)
        if not user:
            raise UserNotFoundError(f"User {user_id} not found")

        user.disabled = False
        user.is_active = True
        user.is_deleted = False
        user.deleted_at = None
        await self._safe_flush()
        await self.session.refresh(user)
        logger.info(f"User {user_id} fully restored")
        return True

    async def delete_account(self, user_id: int) -> bool:
        user = await self.get_by_id(user_id)
        if not user:
            raise UserNotFoundError(f"User {user_id} not found")
        if user.is_deleted:
            return False

        user.is_deleted = True
        user.disabled = True
        user.is_active = False
        await self._safe_flush()
        await self.session.refresh(user)
        logger.info(f"User {user_id} soft-deleted")
        return True

    async def restore_deleted(self, user_id: int) -> bool:
        user = await self.get_by_id(user_id)
        if not user:
            raise UserNotFoundError(f"User {user_id} not found")
        if not user.is_deleted:
            return False

        user.is_deleted = False
        user.deleted_at = None
        user.disabled = False
        user.is_active = True
        await self._safe_flush()
        await self.session.refresh(user)
        logger.info(f"User {user_id} restored")
        return True

    # ================================================================
    # QUERIES
    # ================================================================
    async def get_user_role(self, user_id: int) -> Optional[str]:
        user = await self.get_by_id(user_id)
        return user.user_role if user else None

    async def is_user_active(self, user_id: int) -> bool:
        user = await self.get_by_id(user_id)
        return bool(user and user.is_active and not user.disabled)

    async def is_user_disabled(self, user_id: int) -> bool:
        user = await self.get_by_id(user_id)
        return bool(user and user.disabled)

    async def search_users(
        self,
        skip: int = 0,
        limit: int = 100,
        active_only: bool = True,
        search: Optional[str] = None,
    ) -> List[UserModel]:
        query = select(UserModel)
        if active_only:
            query = query.where(
                and_(UserModel.is_active == True, UserModel.disabled == False)
            )
        if search:
            search_filter = UserModel.name.ilike(f"%{search}%")
            if "@" in search:
                search_filter = search_filter | UserModel.email.ilike(f"%{search}%")
            query = query.where(search_filter)
        query = query.offset(skip).limit(limit).order_by(UserModel.created_at.desc())
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def count_users(self, active_only: bool = True) -> int:
        query = select(func.count()).select_from(UserModel)
        if active_only:
            query = query.where(
                and_(UserModel.is_active == True, UserModel.disabled == False)
            )
        result = await self.session.execute(query)
        return result.scalar() or 0

    async def exists_by_email(self, email: str) -> bool:
        user = await self.get_by_email(email)
        return user is not None

    # ================================================================
    # UTILITIES
    # ================================================================
    def to_dict(self, user: UserModel, exclude: List[str] = None) -> Dict[str, Any]:
        if exclude is None:
            exclude = []
        result = {}
        for column in user.__table__.columns:
            col_name = column.name
            if col_name not in exclude:
                result[col_name] = getattr(user, col_name)
        return result

    def to_safe_dict(self, user: UserModel) -> Dict[str, Any]:
        return self.to_dict(user, exclude=["password_hash", "secret_key", "token"])

    async def bulk_create(self, users_data: List[Dict[str, Any]]) -> List[UserModel]:
        users = [UserModel(**data) for data in users_data]
        for u in users:
            self.session.add(u)
        try:
            await self._safe_flush()
        except IntegrityError as e:
            raise DuplicateEmailError("One or more emails already exist") from e
        for u in users:
            await self.session.refresh(u)
        logger.info(f"Created {len(users)} users")
        return users

    async def bulk_update(
        self, user_updates: List[Tuple[int, Dict[str, Any]]]
    ) -> int:
        updated_count = 0
        for user_id, update_data in user_updates:
            user = await self.get_by_id(user_id)
            if user:
                for field, value in update_data.items():
                    if hasattr(user, field):
                        setattr(user, field, value)
                updated_count += 1
        try:
            await self._safe_flush()
        except IntegrityError as e:
            raise DuplicateEmailError("One or more emails already exist") from e
        logger.info(f"Bulk updated {updated_count} users")
        return updated_count

    @classmethod
    async def create_admin_if_not_exists(
        cls,
        session: AsyncSession,
        email: str,
        password_hash: str,
        name: str = "System Administrator",
        role: str = "admin",
        permissions: Dict[str, bool] = None,
    ) -> UserModel:
        """Race-safe admin creation via INSERT ... ON CONFLICT DO NOTHING."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(UserModel)
            .values(
                name=name,
                email=email,
                password_hash=password_hash,
                user_role=role,
                is_active=True,
                disabled=False,
                is_deleted=False,
                permissions=permissions or {},
            )
            .on_conflict_do_nothing(index_elements=["email"])
            .returning(UserModel.id)
        )
        result = await session.execute(stmt)
        inserted = result.scalar_one_or_none()

        if inserted is not None:
            logger.info(f"New admin created: {email}")
            return await cls._get_by_id(session, inserted)

        result = await session.execute(
            select(UserModel).where(UserModel.email == email)
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            # Extremely unlikely race; treat as failure to create.
            raise InfrastructureError(f"Admin row for {email} disappeared mid-upsert")

        if existing.user_role == "admin":
            logger.info(f"Admin already exists: {email}")
            return existing

        logger.info(f"Promoting user to admin: {email}")
        existing.user_role = "admin"
        await session.flush()
        return existing

    @classmethod
    async def _get_by_id(
        cls, session: AsyncSession, user_id: int
    ) -> Optional[UserModel]:
        result = await session.execute(
            select(UserModel).where(UserModel.id == user_id)
        )
        return result.scalar_one_or_none()

    # ================================================================
    # PERMISSIONS
    # ================================================================
    async def set_permissions(
        self, user_id: int, permissions: Dict[str, bool]
    ) -> UserModel:
        user = await self.get_by_id(user_id)
        if not user:
            raise UserNotFoundError(f"User {user_id} not found")

        permissions = permissions or {}
        if user.permissions is None:
            user.permissions = {}
        # Column-level reassignment so SQLAlchemy notices the JSON change.
        user.permissions = {**user.permissions, **permissions}
        await self._safe_flush()
        await self.session.refresh(user)
        return user

    async def replace_all_permissions(
        self, user_id: int, permissions: Dict[str, bool]
    ) -> UserModel:
        user = await self.get_by_id(user_id)
        if not user:
            raise UserNotFoundError(f"User {user_id} not found")

        user.permissions = permissions or {}
        await self._safe_flush()
        await self.session.refresh(user)
        return user

    async def remove_permissions(
        self, user_id: int, permission_keys: List[str]
    ) -> UserModel:
        user = await self.get_by_id(user_id)
        if not user:
            raise UserNotFoundError(f"User {user_id} not found")

        permission_keys = permission_keys or []
        current = dict(user.permissions or {})
        removed = []
        for key in permission_keys:
            if key in current:
                del current[key]
                removed.append(key)

        user.permissions = current
        await self._safe_flush()
        await self.session.refresh(user)
        if removed:
            logger.info(f"Removed permissions from user {user_id}: {removed}")
        return user

    async def remove_all_permissions(self, user_id: int) -> UserModel:
        user = await self.get_by_id(user_id)
        if not user:
            raise UserNotFoundError(f"User {user_id} not found")

        user.permissions = {}
        await self._safe_flush()
        await self.session.refresh(user)
        logger.info(f"Removed ALL permissions from user {user_id}")
        return user

    async def get_user_permission_db(
        self,
        user_id: Optional[int] = None,
        skip: int = 0,
        limit: int = 100,
        include_user_info: bool = False,
    ) -> Dict[str, Any]:
        # Case 1: single user
        if user_id is not None:
            user = await self.get_by_id(user_id)
            if not user:
                raise UserNotFoundError(f"User {user_id} not found")
            if include_user_info:
                return {
                    "permissions": user.permissions or {},
                    "user_info": {
                        "id": user.id,
                        "email": user.email,
                        "name": user.name,
                        "role": user.user_role,
                    },
                }
            return user.permissions or {}

        # Case 2: paginated list
        count_query = (
            select(func.count())
            .select_from(UserModel)
            .where(
                and_(
                    UserModel.is_active == True,
                    UserModel.disabled == False,
                    UserModel.is_deleted == False,
                )
            )
        )
        total_count = (await self.session.execute(count_query)).scalar() or 0

        query = (
            select(UserModel)
            .where(
                and_(
                    UserModel.is_active == True,
                    UserModel.disabled == False,
                    UserModel.is_deleted == False,
                )
            )
            .order_by(UserModel.id)
            .offset(skip)
            .limit(limit)
        )
        users = list((await self.session.execute(query)).scalars().all())

        users_dict = {}
        for user in users:
            if include_user_info:
                users_dict[str(user.id)] = {
                    "permissions": user.permissions or {},
                    "email": user.email,
                    "name": user.name,
                    "role": user.user_role,
                }
            else:
                users_dict[str(user.id)] = user.permissions or {}

        next_skip = skip + limit
        has_next = next_skip < total_count
        return {
            "users": users_dict,
            "pagination": {
                "total": total_count,
                "skip": skip,
                "limit": limit,
                "has_next": has_next,
                "next": (
                    f"/api/v1/admin/users/permissions?skip={next_skip}&limit={limit}"
                    if has_next
                    else None
                ),
            },
        }