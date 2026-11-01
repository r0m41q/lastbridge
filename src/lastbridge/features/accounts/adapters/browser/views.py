def overview(services, user):
    downloads = services.library.downloads(user["id"])
    saved = sum(item["state"] == "complete" for item in downloads)
    return {
        "linked": user["link_status"] == "linked",
        "lastfm_configured": services.lastfm.configured,
        "stats": [
            ("Saved recordings", saved, "library"),
            ("Download jobs", len(downloads) - saved, "sync"),
            ("Your playlists", len(services.playlists.local(user)), "list"),
        ],
    }
