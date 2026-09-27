def validate_enrichment(record):
    required = ("value", "source", "evidence")
    return all(k in record for k in required)
