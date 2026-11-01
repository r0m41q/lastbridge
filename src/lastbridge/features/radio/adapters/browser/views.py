def radio_view(services, user):
    radio = services.radio.playlist(user)
    songs = services.library.collection(user["id"])["song"][:100]
    for playlist in services.mixes.playlists(user):
        songs += services.mixes.playlists(user, playlist["id"])["entry"]
    choices = {song["id"]: song for song in songs}
    return {"radio": radio, "choices": list(choices.values())[:100]}
