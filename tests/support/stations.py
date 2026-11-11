STATION = {
    "name": "Ambient <&>",
    "streamUrl": "https://radio.example/live.aac?channel=1&quality=high",
    "homepageUrl": "https://radio.example/",
}


def station_call(
    service, method, *, http_method="GET", account="admin", **values
):
    arguments = {
        "apiKey": service[3][account],
        "v": "1.16.1",
        "c": "test",
        "f": "json",
    } | values
    return service[0].request(
        http_method,
        "/rest/" + method,
        **{"data" if http_method == "POST" else "params": arguments},
    )
