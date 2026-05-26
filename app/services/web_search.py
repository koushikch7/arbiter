"""
Tavily-powered real-time web search for Arbiter.

Used when a request opts into web search via the X-Arbiter-Realtime: true
header (or sets metadata.realtime = true). Search results are fetched
synchronously, ranked, and prepended to the system prompt as a structured
context block with numbered citations. The chosen model then generates a
grounded answer.

Quick start
-----------
1. Get a Tavily API key from https://tavily.com (free tier: 1 000 searches/mo).
2. Add TAVILY_API_KEY=tvly-... to .env.
3. Send a request with header X-Arbiter-Realtime: true:

   curl -H 'Authorization: Bearer arbiter-sk-...' \
        -H 'X-Arbiter-Realtime: true' \
        -d '{"model":"auto","messages":[{"role":"user","content":"what is btc price today?"}]}' \
        https://arbiter.chkoushik.com/v1/chat/completions

The response will include header X-Arbiter-Realtime-Sources listing the
URLs that were used to ground the answer.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

TAVILY_ENDPOINT = "https://api.tavily.com/search"
DEFAULT_TIMEOUT_SEC = 8.0
DEFAULT_MAX_RESULTS = 5
DEFAULT_SEARCH_DEPTH = "basic"  # "basic" (1 credit) | "advanced" (2 credits)
RESULT_CACHE_TTL_SEC = 5 * 60  # 5 minutes — keeps near-realtime but reduces credit burn


@dataclass
class WebSearchResult:
    title: str
    url: str
    content: str
    score: float = 0.0
    published_date: Optional[str] = None


@dataclass
class WebSearchResponse:
    query: str
    answer: Optional[str] = None
    results: List[WebSearchResult] = field(default_factory=list)
    latency_ms: int = 0
    credits_used: int = 0

    def as_context_block(self) -> str:
        """Render results as a structured system-prompt block with citations."""
        if not self.results and not self.answer:
            return ""
        lines: List[str] = [
            "## Real-Time Web Search Context",
            f"Query: {self.query}",
            f"Retrieved: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
            "",
        ]
        if self.answer:
            lines.append("### Answer summary")
            lines.append(self.answer)
            lines.append("")
        if self.results:
            lines.append("### Sources")
            for idx, r in enumerate(self.results, start=1):
                date = f" — {r.published_date}" if r.published_date else ""
                lines.append(f"[{idx}] **{r.title}**{date}")
                lines.append(f"     URL: {r.url}")
                # Trim content to ~600 chars per source to keep prompt lean.
                snippet = (r.content or "").strip().replace("\n", " ")[:600]
                lines.append(f"     {snippet}")
                lines.append("")
            lines.append(
                "Use these sources to ground your answer. Cite them inline as [1], [2], etc. "
                "If the answer requires data not in the sources, say so explicitly."
            )
        return "\n".join(lines)

    def source_urls(self) -> List[str]:
        return [r.url for r in self.results if r.url]


class TavilyClient:
    """Minimal async Tavily client with in-process LRU caching."""

    def __init__(self, api_key: Optional[str] = None, redis_client=None):
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "").strip()
        self.redis = redis_client

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def _cache_key(query: str, max_results: int, search_depth: str, topic: str) -> str:
        h = hashlib.sha256(f"{query}|{max_results}|{search_depth}|{topic}".encode()).hexdigest()
        return f"arbiter:websearch:{h[:32]}"

    async def search(
        self,
        query: str,
        *,
        max_results: int = DEFAULT_MAX_RESULTS,
        search_depth: str = DEFAULT_SEARCH_DEPTH,
        topic: str = "general",
        include_answer: bool = True,
        include_domains: Optional[List[str]] = None,
        exclude_domains: Optional[List[str]] = None,
        time_range: Optional[str] = None,
    ) -> Optional[WebSearchResponse]:
        if not self.enabled:
            logger.debug("TavilyClient.search called but no API key configured — skipping")
            return None

        # ── Redis-backed cache (5 min TTL) ──
        cache_key = self._cache_key(query, max_results, search_depth, topic)
        if self.redis is not None:
            try:
                cached = await self.redis.get(cache_key)
                if cached:
                    obj = json.loads(cached)
                    logger.info(f"[web_search] cache hit for query={query[:60]!r}")
                    return WebSearchResponse(
                        query=obj["query"],
                        answer=obj.get("answer"),
                        results=[WebSearchResult(**r) for r in obj.get("results", [])],
                        latency_ms=0,
                        credits_used=0,
                    )
            except Exception as exc:
                logger.debug(f"[web_search] cache lookup error: {exc}")

        payload = {
            "api_key":      self.api_key,
            "query":        query,
            "max_results":  max_results,
            "search_depth": search_depth,
            "topic":        topic,
            "include_answer": include_answer,
        }
        if include_domains:
            payload["include_domains"] = include_domains
        if exclude_domains:
            payload["exclude_domains"] = exclude_domains
        if time_range:
            payload["time_range"] = time_range

        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SEC) as client:
                resp = await client.post(TAVILY_ENDPOINT, json=payload)
        except httpx.RequestError as exc:
            logger.warning(f"[web_search] Tavily network error: {exc}")
            return None

        if resp.status_code != 200:
            logger.warning(f"[web_search] Tavily {resp.status_code}: {resp.text[:200]}")
            return None

        body = resp.json()
        latency_ms = int((time.perf_counter() - t0) * 1000)
        results = [
            WebSearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                content=item.get("content", ""),
                score=float(item.get("score", 0.0) or 0.0),
                published_date=item.get("published_date"),
            )
            for item in body.get("results", [])
            if item.get("url")
        ]
        out = WebSearchResponse(
            query=query,
            answer=body.get("answer"),
            results=results,
            latency_ms=latency_ms,
            credits_used=1 if search_depth == "basic" else 2,
        )
        logger.info(
            f"[web_search] tavily query={query[:60]!r} "
            f"results={len(results)} latency={latency_ms}ms credits={out.credits_used}"
        )
        # ── Write-through cache ──
        if self.redis is not None and results:
            try:
                obj = {
                    "query": out.query,
                    "answer": out.answer,
                    "results": [r.__dict__ for r in out.results],
                }
                await self.redis.set(cache_key, json.dumps(obj), ex=RESULT_CACHE_TTL_SEC)
            except Exception as exc:
                logger.debug(f"[web_search] cache write error: {exc}")
        return out


# ---------------------------------------------------------------------------
# Auto-detection — when X-Arbiter-Realtime is *not* explicitly set, the
# router may auto-enable search for time-sensitive queries. Conservative
# keyword heuristic; misses are OK because the user can always force it
# with the header.
# ---------------------------------------------------------------------------

import re

_REALTIME_KEYWORDS = re.compile(
    r"\b(today|right now|currently|latest|breaking|just now|this week|this month|"
    r"yesterday|tomorrow|tonight|live|real[- ]time|up[- ]to[- ]date|"
    r"current price|stock price|crypto price|btc price|eth price|exchange rate|"
    r"weather|score|election|news|trending|update|status)\b",
    re.IGNORECASE,
)
_DATE_PATTERN = re.compile(r"\b20[2-9]\d\b")  # any year >=2020 mentioned


def looks_time_sensitive(text: str) -> bool:
    if not text:
        return False
    if _REALTIME_KEYWORDS.search(text):
        return True
    if _DATE_PATTERN.search(text):
        return True
    return False
