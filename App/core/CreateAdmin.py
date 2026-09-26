import asyncio
import sys
import os

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.insert(0, project_root)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from App.core.settings import settings
from App.repository.UserRepository import UserRepository
from App.api.dependencies.auth import get_password_hash


async def create_admin():
    try:
        # Create database connection
        engine = create_async_engine(settings.database_url)
        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        
        admin_email = settings.ADMIN_EMAIL
        admin_passwd_hash = get_password_hash(settings.ADMIN_PASSWORD.get_secret_value())
        admin_name = settings.ADMIN_USERNAME or "System Administrator"
        
        async with async_session() as session:
            # ✅ Use the class method directly!
            admin = await UserRepository.create_admin_if_not_exists(
                session=session,  # ← Pass session explicitly
                email=admin_email,
                password_hash=admin_passwd_hash,
                name=admin_name,
                role="admin",
                permissions={
                    # ===== USER SELF-MANAGEMENT =====
                    "user.view.self": True,
                    "user.update.self": True,
                    "user.update.email": True,
                    "user.update.password": True,
                    "user.update.profile": True,
                    "user.delete.self": True,
                    "user.history.view": True,
                    "user.history.delete": True,
                    "user.self.enable": True,
                    "user.disable.self": True,

                    # ===== USER MANAGEMENT =====
                    "user.view.any": True,
                    "user.delete.any": True,
                    "user.enable": True,
                    "user.disable": True,
                    "user.restore": True,
                    "user.promote": True,
                    "user.permission_assign.get": True,

                    # ===== ADMIN PERMISSIONS =====
                    "admin.access": True,
                    "admin.anyuser.password.update": True,
                    "admin.user.enable": True,
                    "admin.users.view": True,
                    "admin.users.disable": True,
                    "admin.users.delete": True,
                    "admin.users.restore": True,
                    "admin.users.promote": True,
                    "admin.users": True,
                    "admin.settings.view": True,
                    "admin.settings.update": True,
                    "admin.view_all": True,
                    "admin.system.kill_switch": True,
                }
            )
            
            if admin:
                print(f"✅ Admin user created or already exists: {admin.email}")
                await session.commit()  
            else:
                print("❌ Failed to create admin")
                
        await engine.dispose()
        
    except Exception as e:
        print(f"❌ Error creating admin: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(create_admin())