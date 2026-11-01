from datetime import UTC, datetime


def decorate(document, stars, plays):
    def visit(value):
        if isinstance(value, list):
            return [visit(v) for v in value]
        if not isinstance(value, dict):
            return value
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
