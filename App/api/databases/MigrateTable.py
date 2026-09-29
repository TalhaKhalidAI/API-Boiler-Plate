# App/models/user.py
from sqlalchemy import Column, Integer, String, Boolean, DateTime, func,JSON
from sqlalchemy.orm import relationship
from App.core.Connector import Base  # Import from new connector

class User(Base):  # Changed from Users to User (singular, PEP8)
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    email = Column(String(255), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)  # Renamed for clarity
    profile_pic = Column(String(500), nullable=True)
    user_role = Column(String(50), default="user", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)  # Better name than 'disable'
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)
    disabled=Column(Boolean,default=False)
    permissions = Column(JSON, default=dict)  # FIX: mutable default avoided
    is_deleted = Column(Boolean, default=False)
    deleted_at = Column(DateTime, nullable=True)
    mfa_enabled=Column(Boolean,default=False,nullable=False)
    mfa_secret_encrypted = Column(String(500), nullable=True)
    mfa_method = Column(String(20), nullable=True)