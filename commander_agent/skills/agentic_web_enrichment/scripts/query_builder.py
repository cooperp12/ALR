def build_queries(vendor, version, field):
    return [
        f'site:{vendor.lower()}.com {version} {field}',
        f'{vendor} {version} release {field}'
    ]
