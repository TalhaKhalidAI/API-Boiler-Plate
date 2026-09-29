from fastapi import APIRouter
from .UserAuth import router
from .Admin import admin_router
from .Users import user_router
from .MFA import mfa_router
from .EmailOTP import email_otp_router
app_router=APIRouter()
app_router.include_router(router,prefix="/auth")
app_router.include_router(admin_router,prefix="/admin")
app_router.include_router(user_router,prefix="/users")
app_router.include_router(mfa_router,prefix="/mfa")
app_router.include_router(email_otp_router,prefix="/email_otp")