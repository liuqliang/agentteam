from .records import normalize


def totals(records):
    result = {}
    for record in records:
        item = normalize(record)
        result[item["label"]] = result.get(item["label"], 0) + item["value"]
    return result
