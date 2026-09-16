# App/api/v1/Admin.py
"""
Admin endpoints: user account management, permissions, and safety-mode reset.

Routes:
    PUT    /account/{user_id}                   — update user account
    POST   /account/disable/{user_id}           — disable user
    POST   /account/enable/{user_id}            — enable user
    POST   /account/temp_token/{user_id}        — issue short-lived token (SLT)
    DELETE /account/{user_id}                   — soft-delete user
    POST   /account/restore/{user_id}           — restore soft-deleted/disabled user
    PUT    /account/password/{user_id}          — change password
    POST   /reset-auto-kill                     — clear safety mode
    GET    /get_all_permissions                 — list permission catalog
    POST   /set_permissions                     — set single user's permissions
    POST   /set_permissions_bulk                — set multiple users' permissions
    GET    /users/permissions                   — read user permissions
    DELETE /remove_permissions                  — remove permissions
"""

from datetime import timedelta
from typing import Optional, Dict, Any

from fastapi import (
    APIRouter, Depends, HTTPException, status, Query, Request, Response,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError

from App.repository.UserRepository import UserRepository
from App.services.admin_service import AdminService
from App.core.LoggingInit import get_core_logger
from App.core.Connector import get_db
from App.core.settings import settings
from App.core.exceptions import (
    DomainError,
    UserNotFoundError,
    DuplicateEmailError,
    AccountAlreadyDisabledError,
)
 
from App.schemas.AuthScheema import UserResponse, PasswordConfirmRequest,PasswordUpdateRequest
 
from App.schemas.AuthScheema import UserResponse, PasswordConfirmRequest
 
from App.models.UserAuthModel import UpdateUser
from App.models.Permissions import Permission
from App.models.PermissionModel import (
    PermissionModel,
    BulkPermissionModel,
    RemovePermissionsModel,
)
from App.api.dependencies.auth import (
    verify_password,
    get_current_user,
    get_password_hash,
    create_short_live_token,
    get_current_user_slt,
)
from App.api.dependencies.permissions import require_permission


admin_router = APIRouter(prefix="/admin_access", tags=["Admin"])
logger = get_core_logger(__name__)


# ============================================================================
# Helper: SLT detection
# ============================================================================
def _is_slt(current_user: Dict[str, Any]) -> bool:
    """True when the auth context comes from a short-lived token."""
    return (
        current_user.get("types") == "slts"
        or current_user.get("token_type") == "slts"
    )


# ============================================================================
# PUT /account/{user_id}
# ============================================================================
@admin_router.put(
    "/account/{user_id}",
    response_model=UserResponse,
    summary="Update user account",
    description=(
        "Update user information. Admin can update any user; "
        "users can only update themselves."
    ),
)
async def update_account(
    request: Request,
    user_id: int,
    update_data: UpdateUser,
    current_user: Dict[str, Any] = Depends(
        require_permission(
            [
                Permission.ADMIN_SETTINGS_UPDATE,
                Permission.ADMIN_USERS_PROMOTE,
                Permission.USER_UPDATE_SELF,
                Permission.USER_UPDATE_PROFILE,
            ],
            mode="any",
            bypass_admin=False,
        )
    ),
    db: AsyncSession = Depends(get_db),
):
    req_id = getattr(request.state, "request_id", "-")
    try:
        service = AdminService(db)
        updated_user = await service.update_account(user_id, update_data, current_user)
        logger.info(f"[{req_id}] User {user_id} updated by {current_user.get('email')}")
        return UserResponse.model_validate(updated_user)

    except HTTPException:
        raise
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except UserNotFoundError:
        raise HTTPException(status_code=404, detail="User not found")
    except DuplicateEmailError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except DomainError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SQLAlchemyError:
        logger.exception(f"[{req_id}] Update account DB error")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")
    except Exception:
        logger.exception(f"[{req_id}] Update account unexpected error")
        raise HTTPException(status_code=500, detail="Update failed")


# ============================================================================
# POST /account/disable/{user_id}
# ============================================================================
@admin_router.post(
    "/account/disable/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Disable user account",
    description=(
        "Disable user account. Admin can disable any user; "
        "users can only disable themselves with password verification."
    ),
)
async def disable_account(
    request: Request,
    user_id: int,
    payload: PasswordConfirmRequest = None,
    current_user: Dict[str, Any] = Depends(require_permission(required_permissions=[
                        Permission.ADMIN_USERS_DISABLE,
                Permission.ADMIN_USERS_PROMOTE,
                Permission.ADMIN_USERS_RESTORE,
                Permission.ADMIN_SETTINGS_UPDATE,
                Permission.USER_SELF_DISABLE,
    ],mode="any",bypass_admin=False,additional_dependency=get_current_user)),
    db: AsyncSession = Depends(get_db),
):
    req_id = getattr(request.state, "request_id", "-")
    try:
        pwd = payload.password if payload else None
        service = AdminService(db)
        result = await service.disable_account(user_id, pwd, current_user)
        logger.info(f"[{req_id}] User {current_user.get('id')} disabled user {user_id}")
        return result

    except HTTPException:
        raise
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except UserNotFoundError:
        raise HTTPException(status_code=404, detail="User not found")
    except AccountAlreadyDisabledError:
        raise HTTPException(status_code=409, detail="Account already disabled")
    except DomainError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SQLAlchemyError:
        logger.exception(f"[{req_id}] Disable account DB error")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")
    except Exception:
        logger.exception(f"[{req_id}] Disable account unexpected error")
        raise HTTPException(status_code=500, detail="Disable failed")


# ============================================================================
# POST /account/enable/{user_id}
# ============================================================================
@admin_router.post(
    "/account/enable/{user_id}",
    summary="Enable a disabled or inactive user account",
    description=(
        "Admin or user with admin permission can enable any user. "
        "A user with self-enable permission can only enable themselves. "
        "An SLT token can enable its own user."
    ),
)
async def enable_account(
    request: Request,
    user_id: int,
    payload: PasswordConfirmRequest = None,
    current_user: Dict[str, Any] = Depends(
        require_permission(
            required_permissions=[
                Permission.USER_SELF_ENABLE,
                Permission.ADMIN_USER_ENABLE,
                Permission.ADMIN_USERS_PROMOTE,
            ],
            mode="any",
            bypass_admin=False,
            additional_dependency=get_current_user_slt,
        )
    ),
    db: AsyncSession = Depends(get_db),
):
    req_id = getattr(request.state, "request_id", "-")
    try:
        pwd = payload.password if payload else None
        service = AdminService(db)
        result = await service.enable_account(user_id, pwd, current_user)
        logger.info(f"[{req_id}] Enable action processed for user {user_id}")
        return result

    except HTTPException:
        raise
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except UserNotFoundError:
        raise HTTPException(status_code=404, detail="User not found")
    except DomainError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SQLAlchemyError:
        logger.exception(f"[{req_id}] Enable account DB error")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")
    except Exception:
        logger.exception(f"[{req_id}] Enable account unexpected error")
        raise HTTPException(status_code=500, detail="Failed to enable account")


# ============================================================================
# POST /account/temp_token/{user_id}
# ============================================================================
@admin_router.post(
    "/account/temp_token/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="Make temp token for user to change password and restore accounts",
    description=(
        "Make temporary token expire in 2 min for user to restore "
        "disabled/deleted account or reset password."
    ),
)
async def temp_token_maker(
    request: Request,
    response: Response,
    user_id: int,
    current_user: Dict[str, Any] = Depends(
        require_permission(
            required_permissions=[
                Permission.ADMIN_USERS_PROMOTE,
                Permission.ADMIN_USERS_RESTORE,
                Permission.ADMIN_USERS_DISABLE,
            ],
            mode="any",
            bypass_admin=True,
        )
    ),
    db: AsyncSession = Depends(get_db),
    cookie_login: bool = False,
    restore_passwd: bool = False,
):
    req_id = getattr(request.state, "request_id", "-")
    try:
        service = AdminService(db)
        result = await service.temp_token_maker(
            user_id=user_id,
            current_user=current_user,
            db=db,
            cookie_login=cookie_login,
            restore_passwd=restore_passwd,
        )
        if cookie_login:
            response.set_cookie(
                key="CSO",
                value=result.get("cookie_value"),
                httponly=True,
                secure=settings.COOKIE_SECURE,
                samesite="strict",
                max_age=120,
                path="/",
                domain=None,
            )
            result.pop("cookie_value", None)
        logger.info(f"[{req_id}] Temporary token issued for user {user_id}")
        return result

    except HTTPException:
        raise
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except UserNotFoundError:
        raise HTTPException(status_code=404, detail="User not found")
    except DomainError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SQLAlchemyError:
        logger.exception(f"[{req_id}] Temp token DB error")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")
    except Exception:
        logger.exception(f"[{req_id}] Temp token unexpected error")
        raise HTTPException(status_code=500, detail="Failed to create temp token")


# ============================================================================
# POST /reset-auto-kill
# ============================================================================
@admin_router.post(
    "/reset-auto-kill",
    status_code=status.HTTP_200_OK,
    summary="Reset Auto-Kill Switch",
    description=(
        "Allows administrators to reset the system from safety mode "
        "after an internal error."
    ),
)
async def reset_auto_kill(
    request: Request,
    current_user: Dict[str, Any] = Depends(require_permission(required_permissions=[Permission.ADMIN_SYSTEM_KILL_SWITCH],mode="any",bypass_admin=False,additional_dependency=get_current_user)),
):
    req_id = getattr(request.state, "request_id", "-")
    try:
        service = AdminService(None)
        result = await service.reset_auto_kill(current_user)
        logger.info(f"[{req_id}] Safety mode reset by admin: {current_user.get('email')}")
        return result
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception:
        logger.exception(f"[{req_id}] Failed to clear auto-kill Redis key")
        raise HTTPException(
            status_code=503,
            detail="Could not clear safety mode — Redis unavailable",
        )


# ============================================================================
# DELETE /account/{user_id}
# ============================================================================
@admin_router.delete(
    "/account/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="Delete account",
    description=(
        "Soft-delete a user account. Admin can delete any user; "
        "users can delete themselves with password verification."
    ),
)
async def delete_account(
    request: Request,
    user_id: int,
    payload: PasswordConfirmRequest = None,
    current_user: Dict[str, Any] = Depends(
        require_permission(
            required_permissions=[
                Permission.USER_DELETE_SELF,
                Permission.ADMIN_USERS_DELETE,
                Permission.ADMIN_USERS_PROMOTE,
            ],
            mode="any",
            bypass_admin=False,
        )
    ),
    db: AsyncSession = Depends(get_db),
):
    req_id = getattr(request.state, "request_id", "-")
    try:
        pwd = payload.password if payload else None
        service = AdminService(db)
        result = await service.delete_account(user_id, pwd, current_user)
        logger.info(f"[{req_id}] Delete action processed for user {user_id}")
        return result

    except HTTPException:
        raise
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except UserNotFoundError:
        raise HTTPException(status_code=404, detail="User not found")
    except DomainError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SQLAlchemyError:
        logger.exception(f"[{req_id}] Delete account DB error")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")
    except Exception:
        logger.exception(f"[{req_id}] Delete account unexpected error")
        raise HTTPException(status_code=500, detail="Failed to delete account")


# ============================================================================
# POST /account/restore/{user_id}
# ============================================================================
@admin_router.post(
    "/account/restore/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="Restore deleted/disabled account",
    description=(
        "Restore a soft-deleted or disabled account. Admin can restore any "
        "user. Users can restore themselves with an SLT token."
    ),
)
async def restore_account(
    request: Request,
    user_id: int,
    current_user: Dict[str, Any] = Depends(
        require_permission(
            required_permissions=[
                Permission.ADMIN_USERS_RESTORE,
                Permission.ADMIN_USERS_PROMOTE,
            ],
            mode="any",
            bypass_admin=False,
            additional_dependency=get_current_user_slt,
        )
    ),
    db: AsyncSession = Depends(get_db),
):
    req_id = getattr(request.state, "request_id", "-")
    try:
        service = AdminService(db)
        result = await service.restore_account(user_id, current_user)
        logger.info(f"[{req_id}] Restore action processed for user {user_id}")
        return result

    except HTTPException:
        raise
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except UserNotFoundError:
        raise HTTPException(status_code=404, detail="User not found")
    except DomainError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SQLAlchemyError:
        logger.exception(f"[{req_id}] Restore account DB error")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")
    except Exception:
        logger.exception(f"[{req_id}] Restore account unexpected error")
        raise HTTPException(status_code=500, detail="Failed to restore account")


# ============================================================================
# PUT /account/password/{user_id}
# ============================================================================
@admin_router.put(
    "/account/password/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="Update user password",
    description=(
        "Admin can update any user's password. Normal users update their "
        "own password. SLT token can only update its own user's password."
    ),
)
async def update_password(
    request: Request,
    user_id: int,
    passwd:PasswordUpdateRequest,
    current_user: Dict[str, Any] = Depends(
        require_permission(
            required_permissions=[
                Permission.USER_UPDATE_PASSWORD,
                Permission.ADMIN_ANY_PASSWORD_UPDATE,
            ],
            mode="any",
            bypass_admin=False,
            additional_dependency=get_current_user_slt,
        )
    ),
     
    db: AsyncSession = Depends(get_db),
):
    req_id = getattr(request.state, "request_id", "-")
    try:
        service = AdminService(db)
        result = await service.update_password(
            user_id=user_id,
            new_password=passwd.new_password.get_secret_value(),
            current_user=current_user,
            old_password=passwd.old_password.get_secret_value(),
        )
        logger.info(f"[{req_id}] Password update processed for user {user_id}")
        return result

    except HTTPException:
        raise
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except UserNotFoundError:
        raise HTTPException(status_code=404, detail="User not found")
    except DomainError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SQLAlchemyError:
        logger.exception(f"[{req_id}] Update password DB error")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")
    except Exception:
        logger.exception(f"[{req_id}] Update password unexpected error")
        raise HTTPException(status_code=500, detail="Failed to update password")


# ============================================================================
# GET /get_all_permissions
# ============================================================================
@admin_router.get(
    "/get_all_permissions",
    summary="List all permissions in the catalog",
)
async def get_all_permissions(
    request: Request,
    current_user: Dict[str, Any] = Depends(
        require_permission(
            required_permissions=[Permission.ADMIN_USERS_PROMOTE],
            mode="any",
            bypass_admin=False,
        )
    ),
    db: AsyncSession = Depends(get_db),
):
    req_id = getattr(request.state, "request_id", "-")
    try:
        service = AdminService(db)
        return await service.get_all_permissions()
    except HTTPException:
        raise
    except Exception:
        logger.exception(f"[{req_id}] Failed to list permissions")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error retrieving permissions",
        )


# ============================================================================
# POST /set_permissions
# ============================================================================
@admin_router.post(
    "/set_permissions",
    summary="Set permissions for a single user",
)
async def set_permissions(
    request: Request,
    pm: PermissionModel,
    current_user: Dict[str, Any] = Depends(
        require_permission(
            required_permissions=[Permission.ADMIN_USERS_PROMOTE],
            bypass_admin=False,
        )
    ),
    db: AsyncSession = Depends(get_db),
):
    req_id = getattr(request.state, "request_id", "-")
    try:
        service = AdminService(db)
        result = await service.set_permissions(pm, current_user)
        logger.info(f"[{req_id}] Single permission update processed for user {pm.user_id}")
        return result

    except HTTPException:
        raise
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except UserNotFoundError:
        raise HTTPException(status_code=404, detail="User not found")
    except DomainError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SQLAlchemyError:
        logger.exception(f"[{req_id}] Set permissions DB error")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")
    except Exception:
        logger.exception(f"[{req_id}] Set permissions unexpected error")
        raise HTTPException(status_code=500, detail="Failed to set permissions")


# ============================================================================
# POST /set_permissions_bulk
# ============================================================================
@admin_router.post(
    "/set_permissions_bulk",
    summary="Set permissions for multiple users",
)
async def set_permissions_bulk(
    request: Request,
    bulk_pm: BulkPermissionModel,
    current_user: Dict[str, Any] = Depends(
        require_permission(
            [Permission.ADMIN_USERS_PROMOTE], bypass_admin=False
        )
    ),
    db: AsyncSession = Depends(get_db),
    replace_all: bool = False,
):
    req_id = getattr(request.state, "request_id", "-")
    try:
        service = AdminService(db)
        result = await service.set_permissions_bulk(bulk_pm, current_user, replace_all)
        logger.info(f"[{req_id}] Bulk permission update completed: {result.get('mode')}")
        return result

    except HTTPException:
        raise
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except UserNotFoundError:
        raise HTTPException(status_code=404, detail="User not found")
    except DomainError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SQLAlchemyError:
        logger.exception(f"[{req_id}] Bulk permissions DB error")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")
    except Exception:
        logger.exception(f"[{req_id}] Bulk permissions unexpected error")
        raise HTTPException(
            status_code=500, detail="Error in bulk permission update"
        )


# ============================================================================
# GET /users/permissions
# ============================================================================
@admin_router.get(
    "/users/permissions",
    summary="Get user permissions with pagination",
)
async def get_users_permissions(
    request: Request,
    user_id: Optional[int] = Query(None, description="Specific user ID to get permissions for"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    include_user_info: bool = Query(False, description="Include user email and name"),
    current_user: Dict[str, Any] = Depends(require_permission(required_permissions=[
        Permission.ADMIN_USERS_VIEW,
        Permission.ADMIN_USERS_PROMOTE,
    ],mode='any',bypass_admin=False,additional_dependency=get_current_user)),
    db: AsyncSession = Depends(get_db),
):
    req_id = getattr(request.state, "request_id", "-")
    try:
        service = AdminService(db)
        result = await service.get_users_permissions(
            user_id=user_id,
            skip=skip,
            limit=limit,
            include_user_info=include_user_info,
            current_user=current_user,
        )
        logger.info(f"[{req_id}] Permissions view requested for user {user_id}")
        return result

    except HTTPException:
        raise
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except UserNotFoundError:
        raise HTTPException(status_code=404, detail="User not found")
    except DomainError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SQLAlchemyError:
        logger.exception(f"[{req_id}] Get user permissions DB error")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")
    except Exception:
        logger.exception(f"[{req_id}] Get user permissions unexpected error")
        raise HTTPException(
            status_code=500, detail="Error retrieving permissions"
        )


# ============================================================================
# DELETE /remove_permissions
# ============================================================================
@admin_router.delete(
    "/remove_permissions",
    summary="Remove permissions from a user",
)
async def remove_permissions(
    request: Request,
    data: RemovePermissionsModel,
    current_user: Dict[str, Any] = Depends(
        require_permission(
            required_permissions=[Permission.ADMIN_USERS_PROMOTE],
            mode="any",
            bypass_admin=False,
        )
    ),
    db: AsyncSession = Depends(get_db),
):
    req_id = getattr(request.state, "request_id", "-")
    try:
        service = AdminService(db)
        result = await service.remove_permissions(data, current_user)
        logger.info(f"[{req_id}] Permission removal processed for user {data.user_id}")
        return result

    except HTTPException:
        raise
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except UserNotFoundError:
        raise HTTPException(status_code=404, detail="User not found")
    except DomainError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SQLAlchemyError:
        logger.exception(f"[{req_id}] Remove permissions DB error")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")
    except Exception:
        logger.exception(f"[{req_id}] Remove permissions unexpected error")
        raise HTTPException(status_code=500, detail="Error removing permissions")