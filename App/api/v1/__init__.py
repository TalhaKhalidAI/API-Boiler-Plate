from fastapi import APIRouter
from .UserAuth import router as auth_router
from .Admin import admin_router
from .Users import user_router
from .Videos import video_router
from .Channels import chanel_router
from .Comments import router as comment_router
from .Playlists import router as playlist_router
from .Tags import router as tag_router
from .Feed import router as feed_router
from .Search import router as search_router
from .Subscriptions import router as subscription_router
from .WatchHistory import router as watch_history_router
from .Categories import category_router

app_router = APIRouter()
app_router.include_router(auth_router, prefix="/auth")
app_router.include_router(admin_router, prefix="/admin")
app_router.include_router(user_router, prefix="/users")
app_router.include_router(video_router, prefix="/videos")
app_router.include_router(chanel_router, prefix="/channels")
app_router.include_router(comment_router)                                # paths set inside: /videos/{id}/comments
app_router.include_router(playlist_router)                               # prefix set inside: /playlists
app_router.include_router(tag_router)                                    # prefix set inside: /tags
app_router.include_router(feed_router)                                   # prefix set inside: /feed
app_router.include_router(search_router)                                 # prefix set inside: /search
app_router.include_router(subscription_router)                           # prefix set inside: /subscriptions
app_router.include_router(watch_history_router)                          # prefix set inside: /watch-history
app_router.include_router(category_router)                               # prefix set inside: /categories