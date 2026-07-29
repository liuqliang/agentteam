def normalize(record):
    return {"label": record.get("label"), "value": int(record["value"])}
