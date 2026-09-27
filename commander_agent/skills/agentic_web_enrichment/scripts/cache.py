import time

def cache_record(query, result):
    return {"query": query, "result": result, "timestamp": time.time()}
