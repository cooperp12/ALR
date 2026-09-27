def source_priority(source_type):
    return {
        "official_vendor": 100,
        "official_docs": 90,
        "official_repo": 85,
        "secondary": 60
    }.get(source_type, 0)
