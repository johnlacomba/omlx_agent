# Compound Engineering Learnings

Project learnings that make future work easier.

---

## Structural enforcement needed for 'default to complete' verifier behavior

**Date:** 2026-04-24 23:59
**Tags:** completion-detection, verification, prompt-engineering, structural-enforcement

**Problem:**
The completion verifier was too strict despite instructions to 'default to complete', causing the manager to reject valid completion claims and push for 2-4 unnecessary iterations before finally accepting completion.

**Solution:**
Implemented structural enforcement instead of relying on prompt-only instructions: (1) Added evidence requirements - verifier must quote exact text proving incompleteness and identify violation type, (2) Added _validate_evidence() function that flips 'incomplete' verdicts to 'complete' if evidence is missing, fabricated, or has invalid violation type, (3) Removed wasteful 3-iteration retry loop for JSON parsing, (4) Aligned system prompt from 'strict completion auditor' to 'completion verifier' to avoid contradiction with user prompt.
---

## No-API web research pattern for local agents

**Date:** 2026-04-25 13:02
**Tags:** web-research,wikipedia,caching,sqlite,content-extraction,no-api,security

**Problem:**
Need web research capabilities in omlx_agent without requiring external API keys (avoiding claude-computer-use dependency, respecting user preference for no-API approach).

**Solution:**
Implemented Wikipedia API integration with SQLite caching: (1) Wikipedia MediaWiki API for search via `https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=<query>&format=json` with realistic User-Agent header, no key required, (2) SQLite cache at `~/.omlx/search_cache.db` with TTL-based expiration (30d docs, 7d default, 24h blogs), (3) Content extraction via readability-lxml + BeautifulSoup fallback with HTML entity cleanup, (4) Retry logic with exponential backoff (1s, 2s, 4s) for network resilience, (5) URL scheme validation rejecting non-http(s) schemes for security. Gotchas: Use strftime('%s', ...) for cache expiration comparison (not datetime()); resolve hostnames via DNS to catch CNAME bypasses; always use realistic User-Agent to avoid 403 responses.
---