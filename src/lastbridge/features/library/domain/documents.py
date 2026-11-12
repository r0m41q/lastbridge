from datetime import UTC, datetime


def decorate(document, stars, plays, recordings=None):
    def visit(value):
        if isinstance(value, list):
            return [visit(v) for v in value]
        if not isinstance(value, dict):
            return value
        if recordings and value.get("id") in recordings:
            value = recordings[value["id"]] | {"id": value["id"]}
        result = {
            k: visit(v)
            for k, v in value.items()
            if k not in ("starred", "playCount", "played")
        }
        if result.get("id") in stars:
            result["starred"] = stars[result["id"]]
        if result.get("id") in plays:
            count, last = plays[result["id"]]
            result["playCount"] = count
            result["played"] = (
                datetime.fromtimestamp(last, UTC)
                .isoformat()
                .replace("+00:00", "Z")
            )
        return result

    return visit(document)
