# App/api/v1/Categories.py
"""
Category endpoints.

Public (no auth):
    GET  /categories              → all active categories (flat)
    GET  /categories/roots        → top-level categories only
    GET  /categories/{id}         → category + subcategories + recent videos
    GET  /categories/{id}/children → direct sub-categories

Admin only:
    POST   /categories            → create
    PATCH  /categories/{id}       → update
    DELETE /categories/{id}       → delete
    PUT    /categories/{id}/parent → reparent
"""
from typing import Optional
from fastapi import APIRouter, Depends, Body, Query
from sqlalchemy.ext.asyncio import AsyncSession

from App.core.Connector import get_db
from App.api.dependencies.auth import get_current_active_user
from App.services.category_service import CategoryService

category_router = APIRouter(prefix="/categories", tags=["Categories"])


# ============================================================================
# PUBLIC READ
# ============================================================================

@category_router.get("", summary="List all active categories")
async def list_categories(
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """
    Flat list of all active categories.
    No authentication required.
    """
    return await CategoryService(db).list_all(limit=limit, offset=offset)


@category_router.get("/roots", summary="List top-level categories")
async def list_root_categories(db: AsyncSession = Depends(get_db)):
    """
    Top-level categories (no parent). No authentication required.
    """
    return await CategoryService(db).list_roots()


@category_router.get("/{category_id}", summary="Get category with videos")
async def get_category_page(
    category_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Category details + sub-categories + recent public videos.
    No authentication required.
    """
    return await CategoryService(db).get_category_page(category_id)


@category_router.get("/{category_id}/children", summary="List sub-categories")
async def list_children(
    category_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Direct children of a category. No authentication required.
    """
    return await CategoryService(db).list_children(category_id)


# ============================================================================
# ADMIN WRITE
# ============================================================================

@category_router.post("", status_code=201)
async def create_category(
    slug: str = Body(...),
    name: str = Body(...),
    description: Optional[str] = Body(None),
    icon_key: Optional[str] = Body(None),
    parent_id: Optional[int] = Body(None),
    display_order: int = Body(0),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a category (admin only)."""
    cat = await CategoryService(db).create_category(
        current_user,
        slug=slug, name=name, description=description,
        icon_key=icon_key, parent_id=parent_id, display_order=display_order,
    )
    return {"id": cat.id, "slug": cat.slug, "name": cat.name}


@category_router.patch("/{category_id}")
async def update_category(
    category_id: int,
    name: Optional[str] = Body(None),
    description: Optional[str] = Body(None),
    icon_key: Optional[str] = Body(None),
    display_order: Optional[int] = Body(None),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a category (admin only)."""
    cat = await CategoryService(db).update_category(
        category_id, current_user,
        name=name, description=description,
        icon_key=icon_key, display_order=display_order,
    )
    return {"id": cat.id, "updated": True}


@category_router.put("/{category_id}/parent")
async def reparent_category(
    category_id: int,
    new_parent_id: Optional[int] = Body(None, embed=True),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Move a category under a different parent (admin only). Send null to make it a root."""
    cat = await CategoryService(db).set_parent(category_id, new_parent_id, current_user)
    return {"id": cat.id, "parent_id": new_parent_id}


@category_router.delete("/{category_id}", status_code=204)
async def delete_category(
    category_id: int,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a category (admin only)."""
    await CategoryService(db).delete_category(category_id, current_user)
