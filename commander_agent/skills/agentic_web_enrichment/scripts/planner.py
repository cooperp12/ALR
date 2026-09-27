def plan_enrichment(vendor, version, field):
    return {
        "vendor": vendor,
        "version": version,
        "required_field": field,
        "source_policy": "official_first"
    }
