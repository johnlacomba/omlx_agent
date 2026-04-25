# Requirements: No-API Web Research for omlx_agent

**Date:** 2026-04-24
**Status:** Draft — ready for planning

---

## Problem Statement

omlx_agent lacks the ability to research online documentation, APIs, architectural patterns, and general knowledge. The agent must rely on pre-existing local knowledge or require the user to manually fetch and paste web content. This limits autonomy when working with unfamiliar libraries, debugging obscure errors, or exploring new technologies.

---

## Goals

1. **Search the web** — Query search engines to find relevant documentation and information
2. **Fetch content** — Retrieve actual page content from search results, not just snippets
3. **Cache intelligently** — Store fetched content with source attribution for later reference without re-downloading
4. **Cite sources** — Enable the agent to reference where information came from, supporting its "cite sources for key assertions" requirement
5. **No external API dependencies** — Use only HTTP requests with user-agent spoofing, no API keys or paid services

---

## Scope Boundaries

### In Scope
- Web search via DuckDuckGo (no API key required)
- HTTP content fetching with realistic browser user-agent
- HTML-to-text extraction for documentation and articles
- SQLite-based local cache with TTL-based expiration
- Tool functions integrated into omlx_agent's existing tool pattern
- Citation support (numbered sources, timestamps, URLs)

### Deferred for Later
- Browser automation for JavaScript-heavy sites (selenium/playswright)
- OAuth/authentication for login-walled content
- Advanced rate-limiting/backoff orchestration
- Parallel/concurrent fetching

### Outside This Product's Identity
- A general-purpose web scraper or downloader
- Competing with dedicated research tools (Perplexity, etc.)
- Real-time news monitoring or RSS feeds

---

## Non-Goals

- Perfect extraction from all websites (many will fail)
- Bypassing paywalls, CAPTCHAs, or authentication
- Replacing human judgment about source credibility

---

## User Stories

1. **As a developer**, I want the agent to look up API documentation for unfamiliar libraries so I don't have to manually search and paste docs.

2. **As a developer**, I want the agent to cite sources for its architectural recommendations so I can verify the information and understand the reasoning.

3. **As a developer**, I want cached search results so the agent doesn't re-fetch the same pages mid-session when I ask follow-up questions.

4. **As a developer**, I want the agent to research error messages I paste so it can find solutions without me having to explain the context.

---

## Technical Recommendations

### Search Engine: DuckDuckGo JSON API

**Endpoint:** `https://duckduckgo.com/api/v1/web?q=<query>`

**Why:**
- No API key required
- Returns structured JSON with results, snippets, and related queries
- Privacy-focused, less aggressive bot detection
- Works reliably with HTTP requests + user-agent header

**Example response structure:**
```json
{
  "results": [
    {
      "title": "Python Requests Documentation",
      "url": "https://docs.python-requests.org/",
      "body": "Python Requests is an elegant and simple HTTP library..."
    }
  ],
  "related": ["python http library", "rest api python"]
}
```

---

### Content Fetching: Python urllib + User-Agent Spoofing

**Approach:** Use `urllib.request` (already in omlx_agent's imports) with a realistic desktop browser user-agent.

**User-Agent string:**
```
Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36
```

**Key parameters:**
- Timeout: 15 seconds
- Follow redirects: enabled (default)
- Politeness delay: 0.5-1s between requests to same domain

---

### HTML Parsing Stack

| Library | Purpose |
|---------|--------|
| `beautifulsoup4` + `lxml` | Parse HTML, extract search results, basic cleanup |
| `readability-lxml` | Article extraction — pulls main content from documentation/tutorial pages |

**Installation:** `pip install beautifulsoup4 lxml readability-lxml`

**Why this combo:**
- BeautifulSoup handles the brittle HTML parsing
- readability-lxml (Mozilla's algorithm) extracts the actual article/documentation text, filtering out nav bars, ads, footers
- Both are mature, well-documented, actively maintained

---

### Storage Design

**Location:** `~/.omlx/search_cache.db` (SQLite)

**Schema:**
```sql
CREATE TABLE cached_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    search_query TEXT,              -- What query led to this URL
    raw_html BLOB,                  -- Optional full HTML (can be large)
    extracted_text TEXT,            -- Cleaned article text
    title TEXT,
    source_site TEXT,               -- Domain (e.g., "docs.python.org")
    fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME,            -- When this entry expires
    fetch_status INTEGER,           -- HTTP status code
    content_length INTEGER,         -- Bytes downloaded
    metadata JSON                   -- {language, word_count, has_ads, etc.}
);

CREATE INDEX idx_url ON cached_results(url);
CREATE INDEX idx_expires ON cached_results(expires_at);
CREATE INDEX idx_source_site ON cached_results(source_site);
```

---

### TTL Policy

| Content Type | TTL | Detection |
|--------------|-----|------------|
| API/Reference docs | 30 days | URL contains `/docs/`, `/api/`, `developer.`, `docs.` |
| Tutorials/How-tos | 7 days | Default |
| News/Blogs | 24 hours | URL contains `/blog/`, `/news/`, `/articles/` |

**Smart features:**
- Check HTTP `Cache-Control` header when available
- Configurable via environment variable or config file

---

### Citation Support

**Tool output format example:**
```
Search Results for: "python async await tutorial"

[1] Real Python — "Python Asyncio Tutorial"
    https://realpython.com/async-io-python/
    Snippet: "Python's asyncio library enables concurrent programming..."
    [CACHED: 2 days ago]

[2] Python Docs — "Asyncio — Asynchronous programming"
    https://docs.python.org/3/library/asyncio.html
    Snippet: "The asyncio library provides infrastructure for writing..."
    [CACHED: 6 hours ago]

[3] FastAPI Docs — "Additional documentation on Path Operations"
    https://fastapi.tiangolo.com/tutorial/...
    Snippet: "The 'async def' syntax allows defining asynchronous functions..."
    [FRESH]

---
Sources: 3 results, 2 cached, 1 fetched (1.2s)
```

**Agent citation pattern:**
- "According to Source [1], ..."
- "As documented in the Python docs [2], ..."
- Include raw URL when appropriate for verification

---

## New Tool Functions

| Tool | Purpose |
|------|--------|
| `web_search(query, depth="basic", cache_only=False, max_results=5)` | Search DuckDuckGo, optionally fetch full content from top results |
| `fetch_url(url, use_cache=True)` | Fetch and extract text from a specific URL |
| `search_cache(query)` | Search cached results only (no network call) |

---

## Integration with Existing Patterns

**Follows omlx_agent conventions:**
- Tool functions prefixed with `tool_`
- Registered in global `TOOLS` list with JSON schema
- Returns structured text the agent can parse
- State stored in `~/.omlx/` alongside other agent data

**Relates to existing tools:**
- `search_files` — local file search; `web_search` is the online equivalent
- `recall_context` — retrieves archived conversation; `search_cache` retrieves archived web content

---

## Practical Limitations (Known and Accepted)

| Limitation | Impact | Mitigation |
|------------|--------|------------|
| JavaScript-heavy sites | Content won't render without browser | Warn when extracted text is suspiciously short |
| CAPTCHAs | Requests blocked | Retry with delay; fallback to cached only |
| Rate limiting | Some sites block frequent requests | Politeness delays, configurable retry backoff |
| Login-walled content | Not accessible | Out of scope; user must provide credentials or content |
| Dynamic/SPA pages | No server-side rendered HTML | Same as JS-heavy limitation |

---

## Success Criteria

1. Agent can search for documentation and return relevant results within 5 seconds
2. Agent can fetch and extract readable text from at least 70% of documentation/tutorial sites
3. Cache prevents duplicate fetches within a session
4. Agent can reference sources by number or URL in its responses
5. No API keys or paid services required

---

## Dependencies

**Python packages to add:**
- `beautifulsoup4` — HTML parsing
- `lxml` — Fast HTML/XML parser (BeautifulSoup backend)
- `readability-lxml` — Article content extraction

**No external APIs or services.**

---

## Assumptions

1. DuckDuckGo's JSON endpoint will remain available without authentication
2. Most documentation sites serve static HTML or server-side rendered content
3. Users accept that some sites won't be parseable (JS-heavy, bot-protected)
4. Local SQLite database is acceptable for caching (no distributed requirements)

---

## Open Questions

1. Should raw HTML be stored by default, or only on-demand? (recommendation: opt-in only, can be large)
2. What's the maximum cache size? Should we auto-prune old entries? (recommendation: TTL-based pruning on startup)
3. Should there be a "force refresh" option to re-fetch cached URLs? (recommendation: yes, via `fetch_url(cache_bust=True)`)
4. How many results to return by default? (recommendation: 5, configurable)

---

## Next Steps

1. **Plan phase** — Detailed implementation plan covering:
   - Database migration/creation strategy
   - Tool function signatures and error handling
   - Integration points in omlx_agent.py
   - Testing strategy

2. **Implementation** — Estimated 1-2 hours:
   - Add dependencies to requirements
   - Implement SQLite cache layer
   - Implement `tool_web_search`, `tool_fetch_url`, `tool_search_cache`
   - Register tools in TOOLS list
   - Add TTL-based cache pruning

3. **Testing** — Verify:
   - Search returns relevant results
   - Content extraction works on common doc sites
   - Cache prevents duplicate fetches
   - Citations are accurate
