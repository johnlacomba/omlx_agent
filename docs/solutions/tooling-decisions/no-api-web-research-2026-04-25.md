---
title: No-API Web Research Tools for Local Agents
problem_type: architecture_decision
module: web_research
tags:
  - web-research
  - duckduckgo
  - caching
  - sqlite
  - content-extraction
  - no-api
  - security
  - ip-validation
component: omlx_agent
date: 2026-04-25
author: johnlacomba
category: tooling-decisions
---

# No-API Web Research Pattern for Local Agents

## Context

Local LLM coding agents need web research capabilities without requiring external API keys. This is important because:

1. **No claude-computer-use dependency** - keep the agent truly local and portable
2. **No API key management** - users shouldn't need to configure service credentials
3. **Works offline-ish** - cached content remains available
4. **Privacy** - no third-party tracking or logging of search queries

## Problem

Direct web scraping of search engines (Google, DuckDuckGo HTML) is unreliable due to:
- Anti-bot measures and CAPTCHAs
- Rate limiting without proper headers/tokens
- HTML structure changes breaking scrapers
- IP blocking for aggressive crawling

## Solution

Implemented a three-tier approach using public APIs and smart caching:

### 1. Wikipedia API for Search

```python
# DuckDuckGo endpoint fallback to Wikipedia
url = 'https://en.wikipedia.org/w/api.php?' + urllib.parse.urlencode({
    'action': 'query',
    'list': 'search',
    'srsearch': query,
    'srlimit': max_results,
    'format': 'json'
})
```

**Why Wikipedia?**
- Stable, documented JSON API
- No rate limiting for reasonable use
- Excellent for reference/documentation topics
- Rich snippets with context
- Always returns valid JSON

**Limitation:** Limited to Wikipedia's coverage - not a replacement for general web search, but sufficient for most technical research.

### 2. Content Extraction Pipeline

```
fetch_url → readability-lxml → BeautifulSoup fallback → plain text
```

```python
# Try readability-lxml first (best results)
try:
    from readability import Document
    doc = Document(html)
    title = doc.title() or ""
    content_html = doc.summary()
except Exception:
    # Fallback to BeautifulSoup
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    # Remove script/style/nav/footer/header
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
```

### 3. SQLite Cache with TTL

| Source Type | TTL |
|-------------|-----|
| API/Reference docs | 30 days (720h) |
| Default | 7 days (168h) |
| News/Blogs | 24 hours |

```python
def _calculate_ttl_hours(url: str) -> int:
    url_lower = url.lower()
    if any(p in url_lower for p in ["/docs/", "/api/", "developer.", "docs."]):
        return 720  # Reference docs - long TTL
    if any(p in url_lower for p in ["/blog/", "/news/", "/articles/"]):
        return 24   # Freshness matters
    return 168      # Default
```

## Key Decisions & Tradeoffs

### Why not DuckDuckGo Instant Answer API?

- Limited to instant answers (snippets only)
- No full content fetch
- Rate limited to 10 requests/hour
- Doesn't return URLs for deeper fetching

### Why SQLite over file-based cache?

| Approach | Pros | Cons |
|----------|------|------|
| File-based | Simple, human-readable | Hard to query, no expiration tracking |
| SQLite | Full-text search, TTL queries, structured | Requires library, binary format |

### Why not use existing search libraries?

Libraries like `duckduckgo-search` exist but:
- Add dependencies
- Often require API keys for good results
- Less control over caching behavior

### Cache location: `~/.omlx/search_cache.db`

- Persists across sessions
- Shared across all repos
- Doesn't clutter working directory
- Can be safely deleted without breaking anything

## Security Considerations

### IP Address Validation (Critical)

The initial implementation only validated URL scheme:

```python
# BEFORE - VULNERABLE
parsed = urllib.parse.urlparse(url)
if parsed.scheme not in ("http", "https"):
    return {"error": "Invalid scheme"}
```

This allowed requests to internal networks:
- `http://192.168.1.1/admin` → Internal admin panel
- `http://localhost:6379` → Redis instance
- `http://127.0.0.1/debug` → Debug endpoints

**Solution:** DNS resolution with IP blocking:

```python
def _is_safe_url(url: str) -> tuple[bool, str]:
    import ipaddress
    import socket
    
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"Invalid scheme '{parsed.scheme}'"
    
    host = parsed.hostname
    if not host:
        return False, "Invalid URL: no hostname"
    
    # Resolve hostname to catch CNAME bypasses
    try:
        addr_info = socket.getaddrinfo(host, None)
        for family, socktype, proto, canonname, sockaddr in addr_info:
            ip = ipaddress.ip_address(sockaddr[0])
            
            if ip.is_private:
                return False, f"Private IP {ip} blocked"
            if ip.is_loopback:
                return False, f"Loopback {ip} blocked"
            if ip.is_link_local:
                return False, f"Link-local {ip} blocked"
            if ip.is_reserved:
                return False, f"Reserved {ip} blocked"
    except socket.gaierror:
        pass  # Allow, fetch will fail naturally
    
    return True, ""
```

**What this catches:**
- Direct IP access: `http://192.168.1.1`
- Localhost variants: `http://localhost`, `http://127.0.0.1`
- DNS bypass: `http://evil.com` → CNAME → `192.168.1.1`
- Link-local: `http://[fe80::1]`

### Other Security Measures

| Measure | Purpose |
|---------|---------|
| URL scheme validation | Prevent `file://`, `gopher://` attacks |
| Timeouts on requests | Prevent hanging on slow/malicious servers |
| No command execution | Pure library calls only |
| Parameterized SQL | No SQL injection risk |
| Content-Type not enforced | Tradeoff - allows more sources, less secure |

### Tradeoff: No Response Size Limit

Currently no limit on fetched content size. This could:
- Consume memory on large pages
- Fill disk with cache

**Decision:** Accept this risk for now. Most documentation pages are <100KB. Add limit if abuse observed.

## Integration with CE Workflow

The web research tools integrate into each phase:

### Brainstorm Phase
```bash
# Understand problem space, find similar issues
/tool web_search "N+1 query problem Rails" depth=deep
/tool web_search "SQLite TTL cache expiration" cache_only=True
```

### Plan Phase
```bash
# Research implementation approaches
/tool web_search "readability-lxml Python example"
/tool fetch_url "https://docs.python.org/3/library/urllib.html"
```

### Work Phase
```bash
# Look up specific API details while coding
/tool web_search "SQLite julianday function"
/tool fetch_url "https://docs.python.org/3/library/ipaddress.html"
```

### Review Phase
```bash
# Find best practices, security considerations
/tool web_search "Python URL validation security"
/tool web_search "SQLite parameterized queries example"
```

### Compound Phase
```bash
# Cross-reference existing solutions
/tool search_cache "N+1 query"  # Find related docs
```

## Gotchas & Lessons Learned

### 1. SQLite `datetime()` vs `strftime()`

**Problem:** Initial implementation used `datetime(expires_at) > datetime('now')`:

```sql
WHERE expires_at IS NULL OR datetime(expires_at) > datetime('now')
```

This could fail when `expires_at` contains timezone info (from Python's `isoformat()`).

**Solution:** Use Unix timestamps:

```sql
WHERE expires_at IS NULL OR strftime('%s', expires_at) > strftime('%s', 'now')
```

Also store without timezone:
```python
expires_at = (datetime.now() + timedelta(hours=ttl)).isoformat()  # No tz
```

### 2. HTML Entity Cleanup

Wikipedia snippets contain HTML entities that need cleanup:

```python
from html import unescape

snippet = unescape(snippet)  # Convert &quot; → "
snippet = re.sub(r'<[^>]+>', '', snippet)  # Remove tags
snippet = ' '.join(snippet.split())  # Normalize whitespace
```

### 3. CNAME Chain Resolution

Simple hostname validation is bypassed by DNS:
```
http://safe-domain.com → CNAME → 192.168.1.1 (internal)
```

**Solution:** Resolve hostname and validate ALL returned IPs.

### 4. Realistic User-Agent Required

Without proper User-Agent, many sites return 403 or stripped content:

```python
REALISTIC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
```

### 5. Exponential Backoff in Retry Loop

```python
for attempt in range(max_retries + 1):
    if attempt > 0:
        time.sleep(min(2 ** (attempt - 1), 4))  # 1s, 2s, 4s, max 4s
```

## Code Examples

### Basic Search
```python
result = tool_web_search("SQLite ATTACH database", depth="basic", max_results=3)
# Returns formatted output with numbered sources and snippets
```

### Deep Search with Content Fetch
```python
result = tool_web_search("Python ipaddress module", depth="deep", max_results=2)
# Fetches full content from top results
# Caches for future access
```

### Cache-Only Search
```python
result = tool_web_search("previous query", cache_only=True)
# No network access, uses cached results only
```

### Fetch Specific URL
```python
result = tool_fetch_url("https://docs.python.org/3/library/sqlite3.html")
# Returns full article text, caches automatically
```

### Search Cache
```python
result = tool_search_cache("HTTP protocol")
# Searches cached content by URL, title, or text
```

## When to Use

| Scenario | Tool | Parameters |
|----------|------|------------|
| General research | `web_search` | `depth=basic`, `max_results=5` |
| Need full content | `web_search` | `depth=deep` |
| Look up specific page | `fetch_url` | `use_cache=True` |
| Recall earlier info | `search_cache` | any query |
| Offline session | `web_search` | `cache_only=True` |

## When Not to Use

- **News/current events** - 24h TTL may be stale
- **Breaking changes** - Always fetch fresh, don't trust cache
- **Highly specific queries** - Wikipedia may not cover niche topics
- **Production data access** - Not a database query tool

## Testing

```bash
# Test URL validation
python3 -c "
from omlx_agent import _is_safe_url

test_cases = [
    ('https://example.com', True),
    ('http://192.168.1.1', False),  # Private IP
    ('http://localhost', False),     # Loopback
    ('file:///etc/passwd', False),   # Wrong scheme
]

for url, expected in test_cases:
    is_safe, _ = _is_safe_url(url)
    assert is_safe == expected, f'Failed for {url}'
print('All tests passed!')
"
```

## References

- [Wikipedia API Documentation](https://www.mediawiki.org/wiki/API:Main_page)
- [readability-lxml Documentation](https://pypi.org/project/readability-lxml/)
- [Python ipaddress Module](https://docs.python.org/3/library/ipaddress.html)
- [SQLite Date and Time Functions](https://www.sqlite.org/lang_datefunc.html)

## Alternatives Considered

| Approach | Why Rejected |
|----------|--------------|
| Direct web scraping | Unreliable, anti-bot measures |
| Google Custom Search API | Requires API key, quota limits |
| DuckDuckGo Instant API | Rate limited, limited results |
| Bing Search API | Requires API key, paid tiers |
| Existing Python libraries | Added dependencies, less control |

## Future Improvements

1. Add response size limit (currently none)
2. Periodic cache pruning (call `_prune_expired_cache_entries`)
3. Configurable politeness delay
4. Cache statistics/health check function
5. Support for headless browser rendering (for JS-heavy sites)
6. Per-domain TTL overrides
