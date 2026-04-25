# Implementation Plan: No-API Web Research for omlx_agent

**Date:** 2026-04-24
**Requires:** docs/brainstorms/2026-04-24-no-api-web-research-requirements.md

---

## Overview

Add web search and content fetching capabilities to omlx_agent using DuckDuckGo's JSON API and Python's urllib. No external API keys required.

**Estimated effort:** 1-2 hours

---

## Pre-flight Checks

- [x] Verify omlx_agent.py imports (confirm `urllib.request`, `urllib.parse`, `json`, `sqlite3` available)
- [x] Confirm Python packages not yet installed: `beautifulsoup4`, `lxml`, `readability-lxml`

---

## Implementation Steps

### Step 1: Add Dependencies Documentation

- [ ] Document required packages in README.md or a requirements.txt equivalent
- [ ] Add install command: `pip install beautifulsoup4 lxml readability-lxml`

**Files modified:** `README.md`

---

### Step 2: Add SQLite Cache Layer

Create the cache module/functions in omlx_agent.py:

**New constants:**
```
SEARCH_CACHE_PATH = os.path.expanduser("~/.omlx/search_cache.db")
```

**New functions to add:**
- `_init_search_cache()` — Create database and tables if not exist
- `_get_cached_result(url)` — Fetch cached entry by URL, check TTL
- `_cache_result(url, ...)` — Store search/fetch result
- `_search_cache_db(query)` — Search cached entries by query string
- `_prune_expired_cache_entries()` — Delete expired entries (run on startup)

**Database schema:**
```sql
CREATE TABLE IF NOT EXISTS cached_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    search_query TEXT,
    raw_html BLOB,
    extracted_text TEXT,
    title TEXT,
    source_site TEXT,
    fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME,
    fetch_status INTEGER,
    content_length INTEGER,
    metadata JSON
);
CREATE INDEX IF NOT EXISTS idx_url ON cached_results(url);
CREATE INDEX IF NOT EXISTS idx_expires ON cached_results(expires_at);
CREATE INDEX IF NOT EXISTS idx_source_site ON cached_results(source_site);
```

**Files modified:** `omlx_agent.py` (~150 lines)

---

### Step 3: Implement DuckDuckGo Search Function

**New internal function:** `_duckduckgo_search(query, max_results=5)`

**Implementation details:**
```
Endpoint: https://duckduckgo.com/api/v1/web?q=<query>
Headers: User-Agent (realistic browser)
Timeout: 10 seconds

Parse JSON response:
- Extract results array
- For each result: title, url, body (snippet)
- Handle empty results, errors, rate limits
```

**Error handling:**
- Network timeout → return error message
- Empty results → return "No results found"
- Rate limited → suggest cache_only mode or retry

**Files modified:** `omlx_agent.py` (~60 lines)

---

### Step 4: Implement URL Fetch + Content Extraction

**New internal function:** `_fetch_and_extract(url, store_raw_html=False)`

**Implementation details:**
```
1. urllib.request with realistic User-Agent
2. Read HTML response, decode utf-8
3. Use readability-lxml to extract main article content
4. Fallback to BeautifulSoup cleanup if readability fails
5. Return: {title, text, word_count, source_site}
```

**User-Agent:**
```
Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36
```

**TTL calculation helper:** `_calculate_ttl(url)` → returns hours based on URL patterns

**Files modified:** `omlx_agent.py` (~80 lines)

---

### Step 5: Implement Tool Functions

**Tool 1: `tool_web_search(query, depth="basic", cache_only=False, max_results=5)`**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `query` | required | Search query string |
| `depth` | "basic" | "basic"=snippets only, "deep"=fetch full content |
| `cache_only` | False | If True, only search cache (no network) |
| `max_results` | 5 | Max results to return |

**Logic:**
1. If cache_only: search cache DB, return formatted results
2. Query DuckDuckGo, get results with snippets
3. If depth="deep": fetch full content for top results (with politeness delay)
4. Cache fetched content with TTL
5. Return formatted text with numbered sources, URLs, timestamps

**Files modified:** `omlx_agent.py` (~50 lines)

---

**Tool 2: `tool_fetch_url(url, use_cache=True, cache_bust=False)`**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `url` | required | URL to fetch |
| `use_cache` | True | If True, return cached if available and not expired |
| `cache_bust` | False | If True, skip cache and always fetch fresh content |

**Logic:**
1. If cache_bust=True: skip cache, always fetch fresh
2. Else if use_cache=True: check cache first
3. Fetch URL, extract content
4. Cache result (unless cache_bust)
5. Return title, extracted text, word count, source site

**Files modified:** `omlx_agent.py` (~35 lines)

---

**Tool 3: `tool_search_cache(query)`**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `query` | required | Search query (matches against URL, title, search_query, extracted_text) |

**Logic:**
1. Search cache DB using SQLite LIKE or FTS if available
2. Return formatted list of cached entries matching query
3. Include freshness indicators (e.g., "2 days ago")

**Files modified:** `omlx_agent.py` (~30 lines)

---

### Step 6: Register Tools in TOOLS List

Add tool definitions to the global `TOOLS` list in omlx_agent.py:

```python
{
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web using DuckDuckGo. Returns search results with snippets. Use depth='deep' to fetch full content from top results.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "depth": {"type": "string", "enum": ["basic", "deep"], "default": "basic"},
                "cache_only": {"type": "boolean", "default": False},
                "max_results": {"type": "integer", "default": 5}
            },
            "required": ["query"]
        }
    }
},
{
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": "Fetch and extract text content from a specific URL. Returns cleaned article/documentation text. Use cache_bust=True to force a fresh fetch even if cached.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
                "use_cache": {"type": "boolean", "default": True},
                "cache_bust": {"type": "boolean", "default": False}
            },
            "required": ["url"]
        }
    }
},
{
    "type": "function",
    "function": {
        "name": "search_cache",
        "description": "Search cached web content without making network requests. Use this to recall previously fetched pages.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"}
            },
            "required": ["query"]
        }
    }
}
```

**Files modified:** `omlx_agent.py` (~40 lines for tool definitions)

---

### Step 7: Add Tool Function Dispatch

Add dispatch handlers in the tool execution section:

```python
elif function_name == "web_search":
    result = tool_web_search(**arguments)
elif function_name == "fetch_url":
    result = tool_fetch_url(**arguments)
elif function_name == "search_cache":
    result = tool_search_cache(**arguments)
```

**Files modified:** `omlx_agent.py` (~10 lines)

---

### Step 8: Add Cache Initialization on Startup

Add call to `_init_search_cache()` and `_prune_expired_cache_entries()` during agent startup.

**Files modified:** `omlx_agent.py` (~5 lines)

---

### Step 9: Testing

**Manual test cases:**

| Test | Command | Expected |
|------|---------|----------|
| Basic search | `web_search("python requests library")` | 5 results with snippets |
| Deep search | `web_search("asyncio tutorial", depth="deep")` | Results with full content fetched |
| Cache lookup | `search_cache("requests")` | Returns previously cached results |
| URL fetch | `fetch_url("https://docs.python.org/3/library/http.client.html")` | Extracted docs text |
| Cache hit | `fetch_url("...", use_cache=True)` after previous fetch | Returns cached, no network |
| Empty search | `web_search("xyz123nonexistent")` | "No results found" message |

---

## Line Count Estimates

| Component | Lines |
|-----------|-------|
| SQLite cache layer | ~150 |
| DuckDuckGo search | ~60 |
| URL fetch + extraction | ~80 |
| tool_web_search | ~50 |
| tool_fetch_url | ~35 |
| tool_search_cache | ~30 |
| Tool definitions | ~40 |
| Dispatch handlers | ~10 |
| **Total** | **~455 lines** |

---

## Rollback Plan

If issues arise:
1. Comment out new tool definitions from TOOLS list
2. Comment out dispatch handlers
3. Cache database is isolated at `~/.omlx/search_cache.db` — safe to delete

---

## CE Workflow Integration

This section documents how web research tools (`web_search`, `fetch_url`, `search_cache`) integrate into each Compound Engineering workflow phase. The goal is to maximize research value while minimizing unnecessary network calls.

**CE Workflow Phases referenced here:**
1. **Brainstorm** — Domain research, requirement gathering
2. **Plan** — Technical approach selection, library choices
3. **Deepen Plan** — Architectural deep dive, risk identification
4. **Doc Review** — Adversarial review for vulnerabilities and completeness
5. **Work** — Implementation with documentation lookups
6. **Review** — Verification of claims and architectural decisions
7. **Compound** — Documenting learnings and related work

---

### Tool Selection Quick Reference

| Tool | Use When |
|------|----------|
| `web_search(query, depth="basic")` | Exploring a topic, finding relevant sources |
| `web_search(query, depth="deep")` | Need full content from top results for analysis |
| `web_search(query, cache_only=True)` | Recall previously searched content (no network) |
| `fetch_url(url)` | You have a specific URL to read |
| `fetch_url(url, cache_bust=True)` | Force fresh fetch even if cached |
| `search_cache(query)` | Search cached results only (no network) |

---

### Research Constraints & Resilience

**Duration expectations:**
- `depth="basic"`: ~2-3 seconds (search only)
- `depth="deep"`: ~15-45 seconds (search + fetch 5 URLs sequentially)
- Individual URL fetch: ~2-15 seconds (up to 15s timeout)

**Partial failure handling:**
- `web_search(depth="deep")` returns partial results if some URLs fail
- Output includes summary: "Sources: X results, Y fetched, Z failed"
- Failed fetches are logged but don't block the tool from returning available content

**Rate limit behavior:**
- If rate limited mid-session, switch to `cache_only=True` mode
- Notify user: "Rate limited — using cached results only"
- politeness delay: 0.5-1s between requests to same domain

**Cache search behavior:**
- `search_cache` uses SQLite LIKE matching against URL, title, search_query, and extracted_text
- Results ordered by recency (most recent first)
- For follow-up questions, maintain a session context of cached URLs for reliable retrieval

**Upstream failure recovery:**
- If DuckDuckGo JSON API is unavailable, `web_search` returns error message
- Agent should notify user: "Web research unavailable — try cache_only mode or check network"
- Graceful degradation: cached content remains accessible via `search_cache`

---

### 1. Brainstorm Phase

**Purpose:** Research domain context, existing solutions, gather requirements inspiration

**Typical queries:**
- `[problem domain] existing solutions` — e.g., "no-api web search python libraries"
- `[feature] best practices` — e.g., "web scraping best practices 2024"
- `[technology] vs [technology]` — e.g., "beautifulsoup4 vs scrapy"
- `how to [goal]` — e.g., "how to cache api responses python"

**Depth guidance:**
- `depth="basic"` — Initial exploration, scoping the problem space
- `depth="deep"` — When analyzing a specific existing solution in detail

**Integration pattern:**
```
1. User describes problem → Agent identifies knowledge gaps
2. web_search for domain context (basic) 
3. If specific solutions identified → fetch_url their documentation (deep)
4. Synthesize findings into requirements document
5. Document sources in "External References" section of brainstorm
```

**Documentation:** Include key sources found in the brainstorm document:
```
### External Research
- [Source] — Key insight or recommendation
- [Source] — Alternative approach considered
```

---

### 2. Plan Phase

**Purpose:** Research technical approaches, library choices, architecture patterns

**Typical queries:**
- `[library] tutorial` or `[library] documentation` — e.g., "sqlalchemy async tutorial"
- `[pattern] implementation [language]` — e.g., "repository pattern python"
- `[library1] vs [library2] comparison` — e.g., "requests vs httpx comparison"
- `[technology] migration guide` — when upgrading or replacing

**Depth guidance:**
- `depth="basic"` — Comparing multiple options, library selection
- `depth="deep"` — Reading official docs for chosen approach, understanding APIs

**Integration pattern:**
```
1. Identify technical decisions needing research
2. web_search for options/approaches (basic)
3. fetch_url official docs for top contenders (deep)
4. Compare findings, make decisions
5. Document decisions with source citations in plan
```

**Documentation:** Cite sources in "Context & Research" section:
```
### External References
- [Library docs] — API signatures, behavior guarantees
- [Tutorial/Article] — Architectural pattern implementation
```

**Example decision with citation:**
> **Decision:** Use SQLite with TTL-based expiration for caching
> **Rationale:** Lightweight, no external dependencies, supports our TTL requirements.
> As noted in the [SQLite documentation], WAL mode provides better concurrency...

---

### 3. Deepen Plan Phase

**Purpose:** Deep dive research on architectural decisions (already designed for research)

**Typical queries:**
- `[architectural decision] pitfalls` or `[decision] gotchas`
- `[integration] edge cases`
- `[library] performance benchmarks`
- `[pattern] scalability considerations`

**Depth guidance:**
- Primarily `depth="deep"` — This phase is about thoroughness
- Multiple targeted searches to stress-test decisions

**Integration pattern:**
```
1. Identify high-risk or uncertain decisions from plan
2. Targeted web_search for failure modes, edge cases (deep)
3. fetch_url relevant warnings, postmortems, benchmarks
4. Update plan with discovered risks and mitigations
```

**Documentation:** Add findings to "Risks & Dependencies" or "Open Questions":
```
| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Cache stampede on TTL expiration | Medium | Medium | Add jitter to TTL; see [Source] |
```

---

### 4. Doc Review Phase

**Purpose:** Adversarial reviewer research for vulnerabilities, security concerns, completeness

**Typical queries:**
- `[library/feature] security vulnerabilities` or `[library] CVE`
- `[pattern] security implications`
- `[API] rate limiting best practices`
- `[technology] compliance requirements` (GDPR, etc.)

**Depth guidance:**
- `depth="deep"` — Security research requires full context
- Multiple sources for cross-verification

**Integration pattern:**
```
1. Reviewer identifies potential security/risk concerns
2. web_search for vulnerabilities, advisories (deep)
3. fetch_url security documentation, CVE databases
4. Add findings to review document
```

**Documentation:** Include in review findings:
```
### Security Considerations (Research-Backed)
- [Issue]: Research indicates [finding]. See [Source].
  **Recommendation:** [Mitigation]
```

---

### 5. Work Phase

**Purpose:** Look up documentation or solutions during implementation

**Typical queries:**
- `[library] [specific method]` — e.g., "sqlalchemy session merge"
- `[error message]` — Paste exact error for solutions
- `[feature] example code` — e.g., "python contextmanager example"
- `how to [specific task] [language]`

**Depth guidance:**
- `depth="basic"` — Quick API lookup, error troubleshooting
- `depth="deep"` — Complex feature implementation, unfamiliar patterns

**Integration pattern:**
```
1. Encounter uncertainty during implementation
2. web_search for specific solution (basic)
3. fetch_url docs or Stack Overflow if needed (deep)
4. Apply solution, verify
5. Optionally document learnings for future reference
```

**Caching benefit:** Heavy use of `search_cache` to avoid re-fetching docs mid-implementation:
```
1. First lookup: web_search → fetch_url (network)
2. Follow-up questions: search_cache → fetch_url(use_cache=True) (no network)
```

---

### 6. Review Phase

**Purpose:** Verify claims, research best practices, validate architectural decisions

**Typical queries:**
- `[claim being made] verification` or `[claim] source`
- `[pattern] industry adoption` — Validate that a pattern is widely used
- `[decision] retrospective` or `[decision] lessons learned`

**Depth guidance:**
- `depth="deep"` — Verification requires authoritative sources
- Prioritize official docs, peer-reviewed sources

**Integration pattern:**
```
1. Reviewer identifies claims needing verification
2. web_search for authoritative sources (deep)
3. fetch_url primary sources (official docs, papers)
4. Cross-reference multiple sources
5. Document verification status in review
```

**Documentation:**
```
### Claim Verification
- [Claim]: VERIFIED — [Source] confirms this approach.
- [Claim]: PARTIALLY VERIFIED — [Source] supports X but Y remains uncertain.
```

---

### 7. Compound Phase

**Purpose:** Research related work when documenting learnings

**Typical queries:**
- `[problem solved] similar solutions`
- `[approach taken] alternatives`
- `[library/pattern] community discussion`

**Depth guidance:**
- `depth="basic"` — Finding related discussions, alternative approaches
- `depth="deep"` — Reading detailed postmortems or case studies

**Integration pattern:**
```
1. Identify key insights from completed work
2. web_search for related work, similar problems (basic)
3. fetch_url relevant articles, blog posts (deep if valuable)
4. Enrich learnings document with context from broader community
```

**Documentation:** Add to learnings:
```
### Related Work
- [Team/Project] faced similar issue: [Summary]. See [Source].
- Alternative approach used by [Project]: [Summary]
```

---

### General Research Guidelines

**When to use `depth="basic"` vs `depth="deep"`:**

| Use `basic` when... | Use `deep` when... |
|---------------------|--------------------|
| Exploring a topic broadly | Need full content to analyze |
| Comparing multiple options | Reading official documentation |
| Quick fact-checking | Security/research verification |
| Session time is constrained | Architectural decisions being made |

**Caching strategy:**
- Use `search_cache` liberally within a session — it's instant and avoids rate limits
- Cache persists across sessions — valuable for multi-day projects
- `fetch_url(url, use_cache=True)` automatically uses cache when available
- Cache entries have TTL-based expiration (API docs: 30 days, tutorials: 7 days, news: 24 hours)

**Source quality hierarchy:**
1. Official documentation (highest trust)
2. Peer-reviewed papers, RFCs
3. Well-maintained open source project docs
4. Reputable technical blogs (Real Python, etc.)
5. Community discussions (Stack Overflow, forums)

**Rate limiting considerations:**
- Politeness delay (0.5-1s) between requests to same domain
- Prefer `depth="basic"` when possible — fewer fetches
- Use `cache_only=True` when network is unreliable or rate-limited
- Note: Cache busting for stale entries is not yet implemented (see [Open Questions](#open-questions))

---

## Security Considerations

This section addresses security architecture decisions for the web research feature.

### Threat Model Summary

**Assumptions:**
- The omlx_agent runs locally on a developer's machine
- Users have administrative access to their own machine
- The cache file location (`~/.omlx/`) is under user control

**Threats addressed:**
- SSRF via malicious URLs passed to `fetch_url`
- Prompt injection via malicious content from fetched websites
- Data exposure via cached content containing PII or sensitive data
- Abuse via rate limit bypass or resource exhaustion

---

### URL Scheme Validation (Critical)

**Risk:** Without validation, `fetch_url` could be abused to:
- Read local files via `file:///etc/passwd`
- Access internal network resources via SSRF
- Inject payloads via `data:` URLs

**Mitigation:** Implement strict URL scheme allowlist:

```
ALLOWED_SCHEMES = {"http", "https"}
```

Validation function requirements:
1. Parse URL and extract scheme
2. Reject any scheme not in allowlist
3. Log blocked attempts with URL and reason
4. Return clear error: `"Error: URL scheme 'file' not allowed. Only http:// and https:// URLs are permitted."`

**Implementation note:** Add validation as first step in `_fetch_and_extract()` before any network activity.

---

### Output Sanitization (Critical)

**Risk:** Content from malicious websites could:
- Inject prompt injection attacks into agent context
- Include scripts, event handlers, or dangerous HTML
- Embed URLs that could be clicked or followed

**Mitigation:** Sanitize all fetched content before returning:

1. **Strip dangerous elements:** `<script>`, `<iframe>`, `<object>`, `<embed>`, `<link>`
2. **Strip event handlers:** Remove all `on*` attributes (`onclick`, `onload`, etc.)
3. **Neutralize dangerous protocols:** Strip or encode `javascript:`, `data:`, `vbscript:` in hrefs
4. **Limit output size:** Truncate excessively long responses (>50KB text)

**Implementation approach:**
- Use `readability-lxml` for article extraction (already planned)
- Additional sanitization pass using BeautifulSoup to strip remaining dangerous content
- Consider adding `html_sanitizer` or `bleach` package if deeper sanitization needed

---

### Input Validation

**Search query (`web_search`):**
- Max length: 500 characters
- Trim whitespace
- No special validation needed (passed to DuckDuckGo as-is)

**URL (`fetch_url`):**
- Must pass scheme validation (see above)
- Max length: 2048 characters
- Must parse as valid URL (use `urllib.parse.urlparse`)
- Reject malformed URLs with clear error

**SQL injection prevention:**
- All SQLite queries MUST use parameterized queries
- Example: `_search_cache_db` uses `"WHERE search_query LIKE ?"` not string concatenation

---

### Cache Security

**File permissions:**
- Set database file permissions to `0600` (owner read/write only) on creation
- Use `os.chmod(SEARCH_CACHE_PATH, 0o600)` after `_init_search_cache()`

**Raw HTML storage:**
- Default: Do NOT store raw HTML (`store_raw_html=False`)
- Extracted text only reduces PII exposure and storage footprint
- If raw HTML storage is needed, document PII handling separately

**Cache location:**
- `~/.omlx/search_cache.db` is in user home directory
- Users should have exclusive access
- No network shares or mounted drives assumed

**Data retention:**
- TTL-based expiration (30 days docs, 7 days tutorials, 24 hours news)
- No mechanism for permanent deletion currently planned
- Consider adding `clear_cache()` tool for user-initiated cache cleanup

---

### HTTP Request Security

**Redirect handling:**
- urllib follows redirects by default
- Risk: Redirect to internal/malicious sites after user intends safe URL
- Mitigation: After redirect, re-validate final URL scheme
- Alternatively: Disable redirect following and return error, user retries with final URL

**HTTPS preference:**
- No HTTPS-enforcement mode (would break legitimate HTTP sites)
- Users should be aware of plaintext HTTP risks

**Request headers:**
- User-Agent: Browser-mimicking string (already defined)
- No additional headers that leak system information
- No authentication headers

---

### Source Citation Security

**Source credibility:**
- No automated credibility scoring in scope
- Agent should prefer official documentation sources
- Multiple sources recommended for critical claims

**Malicious sources:**
- No domain blacklist/whitelist in scope
- Relies on agent judgment and user verification
- Citations include full URL for transparency

---

### Compliance Considerations

**GDPR:**
- Cache may contain PII from scraped websites
- TTL-based expiration provides automatic retention limit
- User should manually delete cache (`rm ~/.omlx/search_cache.db`) if privacy required
- No user data collected by the tools themselves

**Data in transit:**
- HTTPS preferred when available
- No man-in-the-middle protection (user's responsibility)

**Data at rest:**
- Cache is plaintext SQLite
- No encryption (assumes user-controlled local storage)

---

### Rate Limiting

**Self-imposed:**
- Politeness delay: 0.5-1s between requests to same domain
- Prevents accidental rate limit triggering

**External:**
- No rate limiting imposed on tool usage (agent controls calls)
- If DuckDuckGo rate limits, fallback to `cache_only=True`

---

### Plan-Level Threat Model: Top 3 Exploits

| # | Exploit | Mitigation |
|---|---------|------------|
| 1 | Attacker provides `file:///etc/passwd` to `fetch_url`, agent returns local file contents | URL scheme allowlist (http/https only) |
| 2 | Malicious website injected into search results; HTML contains prompt injection that influences agent behavior | Output sanitization before returning to agent context |
| 3 | Cached content contains PII/session tokens; accessible to other processes or persists beyond intended retention | File permissions (0600), TTL expiration, avoid storing raw HTML |

---

### Verification Checklist

Before merging implementation, verify:
- [ ] URL scheme validation blocks `file://`, `data:`, `gopher://`
- [ ] Output is stripped of `<script>`, event handlers, `<iframe>`
- [ ] All SQLite queries use parameterized statements
- [ ] Cache file created with 0600 permissions
- [ ] Error messages don't leak internal paths or stack traces

---

## Notes

- Follow existing omlx_agent.py style (docstrings, error handling patterns)
- Use `urllib.request` which is already imported
- Error messages should be user-friendly (agent-facing, not raw exceptions)
- Cache pruning should be non-blocking (log but don't fail on errors)
- Security considerations above must be implemented before this feature is production-ready
