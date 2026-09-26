# App/api/dependencies/permissions.py

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Dict, List, Optional, Union

from App.repository.UserRepository import UserRepository
from App.core.Connector import get_db
from App.api.dependencies.auth import get_current_user
from App.core.LoggingInit import get_core_logger
from App.models.Permissions import Permission

logger = get_core_logger(__name__)


def require_permission(
    required_permissions: List[Union[str, Permission]],
    mode: str = "all",
    bypass_admin: bool = True,
    additional_dependency:callable=get_current_user,
 
):  
    def normalize(p):
        return p.value if isinstance(p, Permission) else p
    
    required = [normalize(p) for p in required_permissions]

    async def _check(
        current_user: Dict = Depends(additional_dependency),
        db: AsyncSession = Depends(get_db),
    ) -> Dict:
        user_perms = current_user.get('permissions', {})
        
        # Admin bypass
        if bypass_admin and current_user.get('role') == 'admin':
            return current_user

        # if current_user.get("types") == "slts":
        #     logger.debug(f"🔓 SLT token bypass for {current_user.get('email')}")
        #     return current_user


        # ============================================================
        # ✅ MODE: "all" — User must have ALL permissions
        # ============================================================
        if mode == "all":
            missing = [p for p in required if not user_perms.get(p, False)]
            if missing:
                logger.warning(f"{current_user.get('email')} missing: {missing}")
                raise HTTPException(
                    403, 
                    f"Missing permissions: {', '.join(missing)}"  # ← Only missing ones!
                )
        
        # ============================================================
        # ✅ MODE: "any" — User must have AT LEAST ONE
        # ============================================================
        elif mode == "any":
            # ✅ Check if user has at least one
            has_any = any(user_perms.get(p, False) for p in required)
            if not has_any:
                # ✅ Show the required list, but indicate "need at least one"
                raise HTTPException(
                    403, 
                    f"Need at least one of: {', '.join(required)}"
                )
            # ✅ If user has at least one, PASS!
        
        else:
            raise HTTPException(500, f"Invalid mode: {mode}")

        return current_user

    return _check