import html
import ipaddress
import json
import re
import socket
import urllib.parse
import urllib.request
from commander_agent.mcp.results import clip_text
def host_is_public(host):
    if not host:
        return False

    low = host.lower()

    if low in ("localhost", "localhost.localdomain"):
        return False

    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False

    for info in infos:
        address = info[4][0].split("%")[0]

        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False

    return True

def validate_public_url(url):
    parsed = urllib.parse.urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only public http/https URLs can be fetched.")

    if not host_is_public(parsed.hostname):
        raise ValueError("The URL does not resolve to a public Internet host.")

def strip_html_tags(value):
    value = re.sub(r"(?is)<script.*?>.*?</script>", " ", value)
    value = re.sub(r"(?is)<style.*?>.*?</style>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()

def fetch_public_url(url):
    try:
        validate_public_url(url)
    except Exception as exc:
        return json.dumps({"error": str(exc), "url": url}, indent=2)

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 BOTSv3-Lab-Agent/1.0",
            "Accept": "text/plain,text/html,application/json,*/*",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            final_url = response.geturl()
            validate_public_url(final_url)
            body = response.read(250000)
            content_type = response.headers.get("Content-Type", "")

    except Exception as exc:
        return json.dumps(
            {
                "error": f"{type(exc).__name__}: {exc}",
                "url": url,
            },
            indent=2,
        )

    decoded = body.decode("utf-8", errors="replace")

    if "html" in content_type.lower():
        display = strip_html_tags(decoded)
    else:
        display = decoded

    return json.dumps(
        {
            "url": final_url,
            "content_type": content_type,
            "text": clip_text(display, 10000),
        },
        indent=2,
        ensure_ascii=False,
    )

def web_search_public(query):
    """
    Lightweight best-effort DuckDuckGo HTML search for lab enrichment.
    """
    url = (
        "https://html.duckduckgo.com/html/?q="
        + urllib.parse.quote_plus(query)
    )

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 BOTSv3-Lab-Agent/1.0"
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(300000).decode("utf-8", errors="replace")

    except Exception as exc:
        return json.dumps(
            {
                "error": f"{type(exc).__name__}: {exc}",
                "query": query,
            },
            indent=2,
        )

    results = []

    for match in re.finditer(
        r'(?is)<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        body,
    ):
        href = html.unescape(match.group(1))
        title = strip_html_tags(match.group(2))

        parsed = urllib.parse.urlparse(href)
        qs = urllib.parse.parse_qs(parsed.query)

        if "uddg" in qs:
            href = qs["uddg"][0]

        if title and href:
            results.append({"title": title, "url": href})

        if len(results) >= 8:
            break

    return json.dumps(
        {
            "query": query,
            "results": results,
            "page_excerpt": clip_text(strip_html_tags(body), 4000),
        },
        indent=2,
        ensure_ascii=False,
    )

