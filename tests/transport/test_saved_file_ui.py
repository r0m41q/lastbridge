from support.catalog import SONG, get
from support.imports import browser


def save_song(bundle):
    service, enrichment, audio, record, path = bundle
    song = service[1].state.services.catalog.song(SONG)
    get(service, "star", id=song["id"])
    return song, browser(service)


def test_saved_file_page_repair_and_private_status(enrichment_service):
    service, enrichment, audio, record, path = enrichment_service
    song, csrf = save_song(enrichment_service)
    url = f"/library/files/{song['id']}"
    page = service[0].get(url)
    assert page.status_code == 200 and "unchecked" in page.text
    assert "&lt;studio&gt;" in page.text
    assert not enrichment.lyrics.calls
    response = service[0].post(
        url + "/repair",
        data={"csrf": csrf, "embed_cover": "yes", "embed_lyrics": "yes"},
    )
    assert response.status_code == 200 and "present" in response.text
    assert path.with_suffix(".lrc").is_file()
    library = service[0].get("/library")
    assert "Tags: present" in library.text
    assert "Lyrics: present" in library.text
    browser(service, "alice")
    assert service[0].get(url).status_code == 404
    assert "Tags: present" not in service[0].get("/library").text


def test_saved_file_repair_csrf_origin_head_and_ownership(enrichment_service):
    service, enrichment, audio, record, path = enrichment_service
    song, csrf = save_song(enrichment_service)
    url = f"/library/files/{song['id']}"
    before = path.read_bytes()
    client = service[0]
    assert client.head(url + "/repair").status_code == 405
    assert (
        client.post(url + "/repair", data={"csrf": "bad"}).status_code == 403
    )
    assert (
        client.post(
            url + "/repair",
            data={"csrf": csrf},
            headers={"Origin": "https://other.test"},
        ).status_code
        == 403
    )
    csrf = browser(service, "alice")
    assert client.post(url + "/repair", data={"csrf": csrf}).status_code == 404
    client.cookies.clear()
    assert client.get(url, follow_redirects=False).status_code == 303
    assert client.post(url + "/repair", data={"csrf": csrf}).status_code == 401
    assert path.read_bytes() == before and not enrichment.lyrics.calls


def test_missing_audio_and_failures_have_safe_messages(enrichment_service):
    service, enrichment, audio, record, path = enrichment_service
    song, csrf = save_song(enrichment_service)
    url = f"/library/files/{song['id']}"

    def fail(*args, **kwargs):
        raise RuntimeError("private upstream URL and credentials")

    enrichment.files.writer.write = fail
    response = service[0].post(url + "/repair", data={"csrf": csrf})
    assert response.status_code == 409
    assert "private upstream" not in response.text
    assert "check storage" in response.text
    path.unlink()
    response = service[0].get(url)
    assert response.status_code == 200
    assert "Repair saved file</button>" not in response.text
