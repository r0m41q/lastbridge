from fastapi import HTTPException


def require_playlist_access(user):
    if not user["admin"] and user["link_status"] not in ("linked", "relink"):
        raise HTTPException(403, "Link Last.fm first")
