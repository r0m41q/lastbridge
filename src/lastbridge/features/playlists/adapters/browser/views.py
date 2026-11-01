def playlists_view(services, user):
    local = services.playlists.local(user)
    choices = local + services.mixes.playlists(user)
    radio = services.radio.playlist(user)
    if radio:
        choices.append(radio)
    return {
        "local": local,
        "downloaded": services.playlists.downloaded(user),
        "choices": choices,
    }
