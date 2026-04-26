"""
Intent classifier — pure-Python heuristics, no LLM call.

Maps a ChatCompletionRequest to a high-level intent that the smart auto-router
uses to pick the best free-tier model.  Designed to be deterministic, fast
(<1ms), and explainable.

Inputs considered (in priority order):
  1. Explicit hint via ``request.metadata.arbiter_intent`` (escape hatch).
  2. Multimodal content (image_url part) → "vision".
  3. Token-count threshold → "long-context".
  4. Regex keyword scan on the last user message.
  5. Fallback → "balanced".
"""
from __future__ import annotations

import re
from typing import List, Literal, Optional

Intent = Literal[
    "code",         # programming / debugging / refactoring
    "reasoning",    # math, logic, analysis, multi-step problems
    "long-context", # huge prompt that needs ≥ 128K context
    "vision",       # image understanding
    "creative",     # storytelling, marketing copy, long-form writing
    "fast",         # quick chat, one-liner replies, low-latency wanted
    "balanced",     # general-purpose default
]

VALID_INTENTS = (
    "code", "reasoning", "long-context", "vision", "creative", "fast", "balanced",
)


# ---------------------------------------------------------------------------
# Keyword sets (lowercased; matched as whole words via regex).
# ---------------------------------------------------------------------------

CODE_KEYWORDS = {
    # languages
    "python", "javascript", "typescript", "java", "golang", "rust", "c++", "cpp",
    "c#", "csharp", "kotlin", "swift", "ruby", "php", "scala", "haskell", "elixir",
    # general programming
    "code", "function", "class", "method", "module", "package", "import",
    "variable", "loop", "iterate", "recursive", "recursion", "algorithm",
    "compile", "syntax", "runtime", "stacktrace", "traceback", "exception",
    "debug", "bug", "fix", "implement", "refactor", "rewrite", "snippet",
    "regex", "regexp", "json", "yaml", "xml", "sql", "query", "sqlite",
    # ecosystems
    "django", "flask", "fastapi", "express", "react", "vue", "angular", "nextjs",
    "node", "npm", "yarn", "pnpm", "pip", "poetry", "cargo", "go mod", "maven",
    "kubernetes", "docker", "dockerfile", "terraform", "ansible",
    "git", "github", "gitlab", "ci/cd", "pipeline", "workflow",
    # APIs & dev concepts
    "endpoint", "rest api", "graphql", "websocket", "grpc",
    "schema", "migration", "test", "unit test", "pytest", "jest", "mocha",
    # shells
    "bash", "shell", "zsh", "powershell", "command line", "cli",
}

REASONING_KEYWORDS = {
    "prove", "proof", "derive", "calculate", "compute", "solve",
    "equation", "theorem", "lemma", "integral", "derivative", "matrix",
    "probability", "statistics", "logic", "logical", "syllogism",
    "deduce", "inference", "reasoning", "step by step", "explain why",
    "analyse", "analyze", "think through", "evaluate",
}

CREATIVE_KEYWORDS = {
    "story", "poem", "poetry", "haiku", "novel", "narrative", "screenplay",
    "lyrics", "song", "blog", "article", "essay", "marketing", "copywriting",
    "brand", "tagline", "slogan", "headline", "ad copy", "advertisement",
    "tweet", "post", "caption", "creative",
}

FAST_KEYWORDS = {
    "quickly", "fast", "asap", "tldr", "tl;dr", "summary in one", "one-liner",
    "yes or no", "y/n", "in one sentence", "in one word",
}

LONG_CONTEXT_KEYWORDS = {
    "summarize", "summarise", "summary of this document", "this document",
    "this transcript", "long document", "extract from", "table of contents",
}

# Compile word-boundary regex (matches whole tokens, case-insensitive).
def _compile(keywords):
    if not keywords:
        return None
    # escape regex metachars then join with |
    escaped = sorted({re.escape(k) for k in keywords}, key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.IGNORECASE)


_RE_CODE        = _compile(CODE_KEYWORDS)
_RE_REASONING   = _compile(REASONING_KEYWORDS)
_RE_CREATIVE    = _compile(CREATIVE_KEYWORDS)
_RE_FAST        = _compile(FAST_KEYWORDS)
_RE_LONG_CTX_KW = _compile(LONG_CONTEXT_KEYWORDS)

# Code-fence / inline-code detection — strong code signal.
_RE_CODE_FENCE  = re.compile(r"```|^\s{4,}\S|`[^`\n]+`", re.MULTILINE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify(request) -> Intent:
    """
    Return the intent for *request*.  Never raises; falls back to "balanced".
    """
    # 1. Explicit hint from caller.
    hint = _extract_hint(request)
    if hint and hint in VALID_INTENTS:
        return hint  # type: ignore[return-value]

    # 2. Vision — any message part with an image.
    if _has_image(request):
        return "vision"

    # 3. Token-count → long-context (above ~16K total).
    token_est = _estimate_tokens(request)
    if token_est > 16_000:
        return "long-context"

    last = _last_user_text(request) or ""

    # 4. Code is the highest-precision signal — fences / inline code first.
    if _RE_CODE_FENCE and _RE_CODE_FENCE.search(last):
        return "code"
    code_hits = _count(_RE_CODE, last)
    reasoning_hits = _count(_RE_REASONING, last)
    creative_hits = _count(_RE_CREATIVE, last)
    fast_hits = _count(_RE_FAST, last)
    longctx_hits = _count(_RE_LONG_CTX_KW, last)

    if longctx_hits and token_est > 8_000:
        return "long-context"

    # Pick the strongest signal.  Code wins on ties (most specific).
    scores = [
        ("code", code_hits * 2),         # code keywords are highly specific
        ("reasoning", reasoning_hits),
        ("creative", creative_hits),
        ("fast", fast_hits),
    ]
    scores.sort(key=lambda kv: (-kv[1], kv[0]))
    if scores[0][1] > 0:
        return scores[0][0]  # type: ignore[return-value]

    # 5. Very short prompts → quick chat (only if no other signal fired).
    #    Use a strict char threshold so genuine questions stay "balanced".
    if fast_hits > 0 or len(last.strip()) < 30:
        return "fast"

    return "balanced"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_hint(request) -> Optional[str]:
    # Pydantic v2 model with extra="allow" stores extras in __pydantic_extra__.
    meta = getattr(request, "metadata", None) or {}
    if isinstance(meta, dict):
        h = meta.get("arbiter_intent") or meta.get("intent")
        if isinstance(h, str):
            return h.lower().strip()
    return None


def _has_image(request) -> bool:
    for msg in getattr(request, "messages", []) or []:
        c = getattr(msg, "content", None)
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") in ("image_url", "image"):
                    return True
    return False


def _last_user_text(request) -> str:
    msgs = getattr(request, "messages", []) or []
    for msg in reversed(msgs):
        if getattr(msg, "role", None) != "user":
            continue
        c = getattr(msg, "content", None)
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(
                p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text"
            )
    return ""


def _estimate_tokens(request) -> int:
    total = 0
    for msg in getattr(request, "messages", []) or []:
        c = getattr(msg, "content", None)
        if isinstance(c, str):
            # Use the larger of word-based and char-based estimate so that
            # giant single-token blobs (e.g. 80k 'A' chars) still register.
            total += max(int(len(c.split()) * 1.3), len(c) // 4)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = str(part.get("text", ""))
                    total += max(int(len(text.split()) * 1.3), len(text) // 4)
    return total


def _count(pattern, text: str) -> int:
    if pattern is None or not text:
        return 0
    return len(pattern.findall(text))
