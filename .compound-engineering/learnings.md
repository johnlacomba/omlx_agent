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

**Date:** 2026-04-25 10:43
**Tags:** web-research, duckduckgo, caching, sqlite, content-extraction, no-api

**Problem:**
Need web research capabilities in omlx_agent without requiring external API keys (avoiding claude-computer-use dependency, respecting user preference for no-API approach).

**Solution:**
Implemented DuckDuckGo JSON API integration with SQLite caching: (1) DuckDuckGo endpoint `https://duckduckgo.com/api/v1/web?q=<query>` with realistic User-Agent header, no key required, (2) SQLite cache at `~/.omlx/search_cache.db` with URL-based keys and TTL expiration (30d docs, 7d default, 24h blogs), (3) Content extraction via readability-lxml + BeautifulSoup fallback with HTML entity cleanup, (4) Retry logic with exponential backoff (1s, 2s, 4s) for network resilience, (5) URL scheme validation rejecting non-http(s) schemes for security. Gotchas: SQL query WHERE clause needs parentheses around OR chain before AND condition; use parameterized queries to prevent injection; inline imports hurt maintainability.
---