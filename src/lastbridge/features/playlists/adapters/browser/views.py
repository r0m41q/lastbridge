def playlists_view(services, user):
    local = services.playlists.local(user)
    choices = local + services.mixes.playlists(user)
    imported = services.playlists.imports.rows(user)
    choices += services.playlists.imported(user)
    radio = services.radio.playlist(user)
    if radio:
        choices.append(radio)
    return {
        "local": local,
        "imported": imported,
        "downloaded": services.playlists.downloaded(user),
        "choices": choices,
    }
