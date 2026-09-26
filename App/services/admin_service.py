import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from App.api.dependencies.auth import create_short_live_token, get_password_hash, verify_password
from App.core.RedisConnector import redis_client
from App.core.exceptions import DomainError, UserNotFoundError,AccountAlreadyDisabledError,PasswordRequiredError,IncorrectPasswordError,PermissionDeniedError,ValidationError,DuplicateEmailError
from App.models.Permissions import Permission
from App.repository.UserRepository import UserRepository


class AdminService:
    """Business logic for admin account, permission, and security flows."""

    def __init__(self, db):
        self.db = db

    @staticmethod
    def _is_slt(current_user: Dict[str, Any]) -> bool:
        return (
            current_user.get("types") == "slts"
            or current_user.get("token_type") == "slts"
        )

    @staticmethod
    def _normalize_permissions(perms: Any) -> Dict[str, Any]:
        if not perms:
            return {}
        if isinstance(perms, str):
            try:
                return json.loads(perms)
            except (TypeError, ValueError):
                return {}
        return perms

    async def update_account(self, user_id: int, update_data, current_user: Dict[str, Any]):
        repo = UserRepository(self.db)

        current_user_id = current_user.get("id")
        current_user_role = current_user.get("role")
        current_user_perms = self._normalize_permissions(current_user.get("permissions", {}))
        current_user_email = current_user.get("email")

        target_user = await repo.get_by_id(user_id)
        if not target_user:
            raise UserNotFoundError(f"User {user_id} not found")

        is_admin = current_user_role == "admin"
        is_self = current_user_id == user_id

        has_admin_update_permission = (
            current_user_perms.get("admin.settings.update", False)
            or current_user_perms.get("admin.users.promote", False)
        )
        has_self_update_permission = (
            current_user_perms.get("user.update.self", False)
            or current_user_perms.get("user.update.profile", False)
        )

        if is_admin or has_admin_update_permission:
            pass
        elif is_self and has_self_update_permission:
            pass
        else:
            raise PermissionDeniedError("You don't have permission to update this user's account")

        update_dict = update_data.model_dump(exclude_unset=True)
        if not update_dict:
            raise ValidationError("No fields to update")

        sensitive_fields = ["user_role", "disabled", "is_active"]
        if not (is_admin or has_admin_update_permission):
            for field in sensitive_fields:
                if field in update_dict:
                    raise PermissionDeniedError(
                        f"Only admin or users with admin permissions can update '{field}'"
                    )

        if is_admin and is_self:
            for locked_field in ("user_role", "permissions", "disabled", "is_active"):
                if locked_field in update_dict:
                    raise PermissionDeniedError(
                        f"Admin cannot change their own '{locked_field}'"
                    )

        if "email" in update_dict and update_dict["email"] != target_user.email:
            existing = await repo.get_by_email(update_dict["email"])
            if existing and existing.id != user_id:
                raise DuplicateEmailError("Email already in use")

        if "password" in update_dict:
            update_dict["password_hash"] = get_password_hash(update_dict["password"])
            del update_dict["password"]

        updated_user = await repo.update(user_id, update_dict)
        return updated_user


    async def disable_account(
        self,
        user_id: int,
        password: Optional[str],
        current_user: Dict[str, Any],
    ):
        repo = UserRepository(self.db)

        current_user_id = current_user.get("id")
        current_user_role = current_user.get("role")
        current_user_perms = self._normalize_permissions(current_user.get("permissions", {}))

        target_user = await repo.get_by_id(user_id)
        if not target_user:
            raise UserNotFoundError(f"User {user_id} not found")

        is_admin = current_user_role == "admin"
        is_self = current_user_id == user_id

        has_promote_permission = current_user_perms.get("admin.users.promote", False)
        has_restore_permission = current_user_perms.get("admin.users.restore", False)
        has_self_disable = current_user_perms.get("user.disable.self", False)

        # ← FIX 1: Admins must NEVER disable themselves.
        # A disabled user cannot authenticate (get_current_user rejects them at the
        # gate), so the only way back in is an SLT issued by *another* admin. If
        # the self-disabling admin is the last admin, the system is bricked until
        # someone with direct DB access flips the row by hand.
        if is_self and is_admin:
            raise PermissionDeniedError(
                "Admins cannot disable their own account. Ask another admin to do it."
            )

        can_disable_any = is_admin or has_promote_permission or has_restore_permission

        # ── Path 1: disable someone else ────────────────────────────────────────
        if not is_self:
            if not can_disable_any:
                raise PermissionDeniedError(
                    "You don't have permission to disable this account"
                )
            # Repository raises AccountAlreadyDisabledError if already disabled.
            await repo.disable_account(user_id)
            return {
                "status": "success",
                "user_id": user_id,
                "disabled_by": "admin_or_permission",
            }

        # ── Path 2: disable self (non-admin only, admin case already rejected) ──
        # ← FIX 2: reachable only when is_self AND NOT is_admin.
        # Requires the user.disable.self permission AND password confirmation.
        if not has_self_disable:
            raise PermissionDeniedError(
                "You don't have permission to disable your own account"
            )
        if target_user.disabled:
            raise AccountAlreadyDisabledError(f"User {user_id} is already disabled")
        if not password:
            raise PasswordRequiredError("Password required to disable your own account")
        if not verify_password(password, target_user.password_hash):
            raise IncorrectPasswordError("Incorrect password")

        await repo.disable_account(user_id)
        return {
            "status": "success",
            "user_id": user_id,
            "disabled_by": "self",
        }

    async def enable_account(
        self,
        user_id: int,
        password: Optional[str],
        current_user: Dict[str, Any],
    ):
        repo = UserRepository(self.db)

        cur_id = current_user.get("id")
        cur_role = current_user.get("role")
        cur_perms = self._normalize_permissions(current_user.get("permissions", {}))
        is_slt_token = self._is_slt(current_user)

        is_admin = cur_role == "admin"
        is_self = cur_id == user_id

        has_admin_enable = cur_perms.get("admin.user.enable", False)
        has_admin_promote = cur_perms.get("admin.users.promote", False)
        has_self_enable = cur_perms.get("user.self.enable", False)

        can_enable_any = is_admin or has_admin_enable or has_admin_promote

        # ---- Authorization ----
        if is_slt_token:
            if current_user.get("token_purpose") != "account_restoration":
                raise PermissionDeniedError("SLT token is not valid for account enablement")
            if cur_id != user_id:
                raise PermissionDeniedError(
                    f"SLT token can only enable user {cur_id}, not {user_id}"
                )
        elif can_enable_any:
            # Admin or delegated user — can enable anyone.
            pass
        elif has_self_enable and is_self:
            # Self-enable — only if the caller is targeting themselves.
            pass
        else:
            raise PermissionDeniedError("You don't have permission to enable this account")

        # ---- State checks ----
        target_user = await repo.get_by_id(user_id)
        if not target_user:
            raise UserNotFoundError(f"User {user_id} not found")
        if target_user.is_deleted:
            raise DomainError("User is deleted. Use restore endpoint instead.")
        if not target_user.disabled and target_user.is_active:
            raise DomainError("User is already active. No enable needed.")

        # ---- Enablement ----
        if is_slt_token:
            success = await repo.full_restore(user_id)
            if not success:
                raise DomainError("Failed to enable account with SLT token")
            return {
                "status": "success",
                "message": f"Account {user_id} enabled via SLT token",
                "user_id": user_id,
                "user_email": target_user.email,
                "enabled_by": "SLT Token",
            }

        if can_enable_any:
            # Admin or delegated (can only reach here if not SLT)
            enabled_by = "Admin" if is_admin else "User with permission"
            success = await repo.full_restore(user_id)
            if not success:
                raise DomainError("Failed to enable user account")
            return {
                "status": "success",
                "message": f"User {user_id} enabled by {enabled_by.lower()}",
                "user_id": user_id,
                "user_email": target_user.email,
                "enabled_by": enabled_by,
            }

        # Self-enable (has_self_enable and is_self) — password required.
        if is_self and has_self_enable:
            if not password:
                raise PasswordRequiredError("Password required for self-enable")
            if not verify_password(password, target_user.password_hash):
                raise IncorrectPasswordError("Incorrect password")
            success = await repo.full_restore(user_id)
            if not success:
                raise DomainError("Failed to enable your account")
            return {
                "status": "success",
                "message": "Your account has been enabled successfully",
                "user_id": user_id,
                "user_email": target_user.email,
                "enabled_by": "Self",
            }

        # Shouldn't reach here — the authz block above already rejected the
        # cases that could.
        raise PermissionDeniedError("You don't have permission to enable this account")

    async def temp_token_maker(self, user_id: int, current_user: Dict[str, Any], db, cookie_login: bool, restore_passwd: bool):
        repo = UserRepository(self.db)

        if current_user.get("role") != "admin":
            raise PermissionDeniedError("Only admin can access this")

        target_user = await repo.get_by_id(user_id)
        if not target_user:
            raise UserNotFoundError(f"User {user_id} not found")

        token_purpose = "password_restore" if restore_passwd else "account_restoration"

        if restore_passwd:
            pass
        else:
            if not (target_user.disabled or not target_user.is_active or target_user.is_deleted):
                raise ValidationError("User is already active and valid. No restoration needed.")

        temp_token = await create_short_live_token(user_id, self.db, purpose=token_purpose)

        if cookie_login:
            return {
                "status": "success",
                "message": "Temporary token created and set as cookie",
                "user_id": target_user.id,
                "user_name": target_user.name,
                "purpose": token_purpose,
                "expires_in_minutes": 2,
                "cookie_value": temp_token,
            }

        return {
            "status": "success",
            "message": "Temporary token created",
            "user_id": target_user.id,
            "user_name": target_user.name,
            "user_status": (
                {
                    "disabled": target_user.disabled,
                    "is_active": target_user.is_active,
                    "is_deleted": target_user.is_deleted,
                }
                if not restore_passwd
                else None
            ),
            "purpose": token_purpose,
            "temp_token": temp_token,
            "expires_in_minutes": 2,
        }

    async def reset_auto_kill(self, current_user: Dict[str, Any]):
        perms = self._normalize_permissions(current_user.get("permissions", {}))
        is_admin = current_user.get("role") == "admin"
        has_permission = perms.get("admin.system.kill_switch", False)
        if not (is_admin or has_permission):
            raise PermissionDeniedError("Only administrators can reset the safety mode")

    async def delete_account(self, user_id: int, password: Optional[str], current_user: Dict[str, Any]):
        repo = UserRepository(self.db)

        cur_role = current_user.get("role")
        cur_id = current_user.get("id")
        cur_perms = self._normalize_permissions(current_user.get("permissions", {}))

        is_self_deletion = cur_id == user_id
        is_admin = cur_role == "admin"

        has_admin_delete = cur_perms.get("admin.users.delete", False)
        has_admin_promote = cur_perms.get("admin.users.promote", False)

        can_delete_any = is_admin or has_admin_delete or has_admin_promote

        if can_delete_any:
            target_user = await repo.get_by_id(user_id)
            if not target_user:
                raise UserNotFoundError(f"User {user_id} not found")
            if target_user.is_deleted:
                raise ValidationError("User is already deleted")
            if is_self_deletion:
                raise PermissionDeniedError("You cannot delete your own account")
            success = await repo.delete_account(user_id)
            if not success:
                raise DomainError("Failed to delete user account")
            return {
                "status": "success",
                "message": f"User {user_id} deleted by {current_user.get('email')}",
            }

        if is_self_deletion:
            target_user = await repo.get_by_id(user_id)
            if not target_user:
                raise UserNotFoundError(f"User {user_id} not found")
            if target_user.is_deleted:
                raise ValidationError("User is already deleted")
            if not password:
                raise ValidationError("Password required for self-deletion")
            if not verify_password(password, target_user.password_hash):
                raise PermissionDeniedError("Incorrect password")
            success = await repo.delete_account(user_id)
            if not success:
                raise DomainError("Failed to delete your account")
            return {
                "status": "success",
                "message": "Your account has been soft deleted",
            }

        raise PermissionDeniedError("You don't have permission to delete this account")

    async def restore_account(self, user_id: int, current_user: Dict[str, Any]):
        repo = UserRepository(self.db)

        cur_role = current_user.get("role")
        cur_id = current_user.get("id")
        cur_perms = self._normalize_permissions(current_user.get("permissions", {}))
        is_slt_token = self._is_slt(current_user)
        is_admin = cur_role == "admin"

        has_restore_permission = cur_perms.get("admin.users.restore", False)
        has_promote_permission = cur_perms.get("admin.users.promote", False)
        can_restore_any = is_admin or has_restore_permission or has_promote_permission or is_slt_token

        if is_slt_token:
            if current_user.get("token_purpose") != "account_restoration":
                raise PermissionDeniedError("SLT token is not valid for account restoration")
            slt_user_id = current_user.get("id")
            if slt_user_id != user_id:
                raise PermissionDeniedError(f"SLT token can only restore user {slt_user_id}, not {user_id}")

            target_user = await repo.get_by_id(user_id)
            if not target_user:
                raise UserNotFoundError(f"User {user_id} not found")
            if not target_user.is_deleted and not target_user.disabled and target_user.is_active:
                raise ValidationError("User is already active. No restoration needed.")

            success = await repo.restore_deleted(user_id)
            if not success:
                success = await repo.restore_disable(user_id)
            if not success:
                raise DomainError("Failed to restore account with SLT token")
            return {
                "status": "success",
                "message": f"Account {user_id} restored via SLT token",
                "user_id": user_id,
                "user_email": target_user.email,
                "restored_by": "SLT Token",
            }

        if not can_restore_any:
            raise PermissionDeniedError("You don't have permission to restore accounts")

        target_user = await repo.get_by_id(user_id)
        if not target_user:
            raise UserNotFoundError(f"User {user_id} not found")
        if not target_user.is_deleted and not target_user.disabled and target_user.is_active:
            raise ValidationError("User is already active. No restoration needed.")

        success = False
        if target_user.is_deleted:
            success = await repo.restore_deleted(user_id)
        if not success:
            success = await repo.restore_disable(user_id)
        if not success:
            raise DomainError("Failed to restore user account")

        restored_by = "Admin" if is_admin else "User with permission"
        return {
            "status": "success",
            "message": f"User {user_id} restored by {restored_by}",
            "user_id": user_id,
            "user_email": target_user.email,
            "restored_by": restored_by,
        }

    async def update_password(self, user_id: int, new_password: str, current_user: Dict[str, Any], old_password: Optional[str]):
        repo = UserRepository(self.db)

        cur_role = current_user.get("role")
        cur_id = current_user.get("id")
        is_slt_token = self._is_slt(current_user)
        slt_user_id = current_user.get("user_id")
        cur_perms = self._normalize_permissions(current_user.get("permissions", {}))

        has_admin_any = cur_perms.get("admin.anyuser.password.update", False)
        has_user_update = cur_perms.get("user.update.password", False)

        target_user = await repo.get_by_id(user_id)
        if not target_user:
            raise UserNotFoundError(f"User {user_id} not found")

        is_self_update = cur_id == user_id
        is_admin = cur_role == "admin"
        can_update_any = is_admin or has_admin_any
        can_update_self = has_user_update and is_self_update

        if is_slt_token:
            if current_user.get("token_purpose") != "password_restore":
                raise PermissionDeniedError("SLT token is not valid for password reset")
            if slt_user_id != user_id:
                raise PermissionDeniedError(
                    f"SLT token can only update password for user {slt_user_id}, not {user_id}"
                )
            new_hash = get_password_hash(new_password)
            success = await repo.update_password_hash(user_id, new_hash)
            if not success:
                raise DomainError("Failed to update password with SLT token")
            return {
                "status": "success",
                "message": f"Password updated via SLT token for user {user_id}",
                "user_id": user_id,
                "user_email": target_user.email,
                "updated_by": "SLT Token",
            }

        if can_update_any:
            new_hash = get_password_hash(new_password)
            success = await repo.update_password_hash(user_id, new_hash)
            if not success:
                raise DomainError("Failed to update password")
            updated_by = "Admin" if is_admin else "User with admin permission"
            return {
                "status": "success",
                "message": f"Password updated by {updated_by} for user {user_id}",
                "user_id": user_id,
                "user_email": target_user.email,
                "updated_by": updated_by,
            }

        if can_update_self:
            if not old_password:
                raise ValidationError("Old password is required to change your password")
            if not verify_password(old_password, target_user.password_hash):
                raise PermissionDeniedError("Incorrect old password")
            new_hash = get_password_hash(new_password)
            success = await repo.update_password_hash(user_id, new_hash)
            if not success:
                raise DomainError("Failed to update your password")
            return {
                "status": "success",
                "message": "Your password has been updated successfully",
                "user_id": user_id,
                "user_email": target_user.email,
                "updated_by": "Self",
            }

        raise PermissionDeniedError("You don't have permission to update this user's password")

    async def get_all_permissions(self):
        return [{"name": p.name, "value": p.value} for p in Permission]

    async def set_permissions(self, pm, current_user: Dict[str, Any]):
        repo = UserRepository(self.db)
        uid = current_user.get("id")

        if uid == pm.user_id:
            raise PermissionDeniedError("You cannot update your own permissions")

        target = await repo.get_by_id(pm.user_id)
        if not target:
            raise UserNotFoundError(f"User {pm.user_id} not found")

        updated = await repo.set_permissions(user_id=pm.user_id, permissions=pm.permissions)
        return {
            "status": "success",
            "message": f"Permissions updated for user {pm.user_id}",
            "user_id": pm.user_id,
            "permissions": updated.permissions,
        }

    async def set_permissions_bulk(self, bulk_pm, current_user: Dict[str, Any], replace_all: bool = False):
        repo = UserRepository(self.db)

        has_promote = (current_user.get("permissions") or {}).get("admin.users.promote", False)
        if not has_promote:
            raise PermissionDeniedError("You don't have permission to set permissions. Need admin.users.promote.")

        user_ids = [p.user_id for p in bulk_pm.all_user_permissions]
        existing_users = await repo.get_by_ids(user_ids)
        existing_user_ids = {user.id for user in existing_users}
        missing_users = [uid for uid in user_ids if uid not in existing_user_ids]
        if missing_users:
            raise UserNotFoundError(f"Users not found with IDs: {missing_users}")

        current_user_id = current_user.get("id")
        if current_user_id in user_ids:
            raise PermissionDeniedError("You cannot update your own permissions")

        updated_users = []
        for perm_update in bulk_pm.all_user_permissions:
            if replace_all:
                updated = await repo.replace_all_permissions(perm_update.user_id, perm_update.permissions)
                operation = "replaced"
            else:
                updated = await repo.set_permissions(perm_update.user_id, perm_update.permissions)
                operation = "merged"
            updated_users.append(
                {
                    "user_id": perm_update.user_id,
                    "permissions": updated.permissions,
                    "status": "success",
                    "operation": operation,
                }
            )

        # await self.db.commit()

        return {
            "status": "success",
            "message": (
                f"Permissions {('replaced' if replace_all else 'merged')} for {len(updated_users)} users"
            ),
            "mode": "replace_all" if replace_all else "merge",
            "updated_users": updated_users,
            "total_processed": len(bulk_pm.all_user_permissions),
            "total_success": len(updated_users),
            "total_failed": 0,
        }

    async def get_users_permissions(self, user_id: Optional[int], skip: int, limit: int, include_user_info: bool, current_user: Dict[str, Any]):
        repo = UserRepository(self.db)

        current_user_id = current_user.get("id")
        current_user_perms = self._normalize_permissions(current_user.get("permissions"))

        has_promote = current_user_perms.get("admin.users.promote", False)
        has_settings_view = current_user_perms.get("admin.settings.view", False)
        is_admin = current_user.get("role") == "admin"
        can_view_any = is_admin or has_promote or has_settings_view

        if user_id is None:
            target_user_id = current_user_id
        else:
            is_viewing_self = user_id == current_user_id
            if not is_viewing_self and not can_view_any:
                raise PermissionDeniedError(
                    "You don't have permission to view other users' permissions. Need admin.users.promote or admin.settings.view."
                )
            target_user_id = user_id

        result = await repo.get_user_permission_db(
            user_id=target_user_id,
            skip=skip,
            limit=limit,
            include_user_info=include_user_info,
        )
        return {"status": "success", "data": result}

    async def remove_permissions(self, data, current_user: Dict[str, Any]):
        repo = UserRepository(self.db)

        current_user_id = current_user.get("id")
        current_user_role = current_user.get("role")
        current_user_perms = self._normalize_permissions(current_user.get("permissions"))

        target = await repo.get_by_id(data.user_id)
        if not target:
            raise UserNotFoundError(f"User {data.user_id} not found")

        is_self = current_user_id == data.user_id
        is_admin = current_user_role == "admin"
        has_promote = current_user_perms.get("admin.users.promote", False)

        if is_admin:
            pass
        elif is_self and has_promote:
            pass
        else:
            raise PermissionDeniedError("You don't have permission to delete permissions")

        if is_admin and is_self:
            raise PermissionDeniedError("Admin cannot delete their own permissions")
        if is_self and not is_admin:
            if data.permission_keys and "admin.users.promote" in data.permission_keys:
                raise PermissionDeniedError("You cannot remove your own admin.users.promote permission")

        target_perms = target.permissions or {}
        if isinstance(target_perms, str):
            try:
                target_perms = json.loads(target_perms)
            except (TypeError, ValueError):
                target_perms = {}

        removed_count: Any = 0
        if data.remove_all:
            if not target_perms:
                raise ValidationError("User has no permissions to delete")
            updated = await repo.remove_all_permissions(data.user_id)
            message = f"All permissions removed for user {target.email}"
            removed_count = "all"

        elif data.permission_keys:
            existing_keys = [k for k in data.permission_keys if k in target_perms]
            missing_keys = [k for k in data.permission_keys if k not in target_perms]
            if not existing_keys:
                raise ValidationError(f"Permissions not found: {', '.join(missing_keys)}")
            updated = await repo.remove_permissions(data.user_id, existing_keys)
            message = f"Permissions removed for user {target.email}: {existing_keys}"
            if missing_keys:
                message += f" (Warning: {', '.join(missing_keys)} not found)"
            removed_count = len(existing_keys)

        else:
            raise ValidationError("Either permission_keys or remove_all must be provided")

        return {
            "status": "success",
            "message": message,
            "user_id": data.user_id,
            "permissions": updated.permissions,
            "removed_count": removed_count,
        }