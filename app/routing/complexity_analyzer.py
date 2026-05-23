"""
Request Complexity Analyzer — determines how "hard" a request is.

Maps incoming requests to a complexity tier that the auto-router uses
to match requests with appropriately powerful models.

Complexity Tiers
────────────────
  TRIVIAL  — greetings, yes/no, one-word answers (→ fast/small models)
  SIMPLE   — short factual Q&A, simple lookups, quick translations
  MODERATE — multi-step instructions, moderate code, explanations
  COMPLEX  — deep reasoning, large code generation, creative long-form
  EXPERT   — multi-domain expert analysis, research-level problems,
             very long code architectures, advanced math

The analyzer considers:
  1. Message length and conversation depth
  2. Linguistic complexity (vocabulary, sentence structure)
  3. Task indicators (code complexity markers, reasoning markers)
  4. Explicit quality signals from the user
  5. System prompt sophistication
"""
from __future__ import annotations

import re
from enum import IntEnum
from typing import List, Optional


class Complexity(IntEnum):
    """Request complexity tier — higher = needs more powerful model."""
    TRIVIAL  = 1
    SIMPLE   = 2
    MODERATE = 3
    COMPLEX  = 4
    EXPERT   = 5


# ---------------------------------------------------------------------------
# Signal patterns
# ---------------------------------------------------------------------------

# Trivial / greeting patterns
_RE_GREETING = re.compile(
    r"^(hi|hello|hey|howdy|yo|sup|thanks|thank you|ok|okay|yes|no|bye|goodbye|"
    r"good morning|good night|what's up|how are you|ping|test)\s*[!?.]*$",
    re.IGNORECASE,
)

# Indicators of complex multi-step tasks
_COMPLEX_TASK_MARKERS = re.compile(
    r"\b(step[- ]by[- ]step|comprehensive|in[- ]depth|detailed|thorough|"
    r"analyze|architecture|design pattern|system design|"
    r"compare and contrast|trade-?offs|pros and cons|"
    r"implement .{0,20}complete|full implementation|production[- ]ready|"
    r"enterprise|scalable|optimize|refactor the entire|"
    r"write .{0,10}(complete|full|entire)|build a .{5,}|"
    r"create a (full|complete|production)|develop a .{5,}|"
    r"design a .{5,}|design .{3,} (system|service|cache|api|pipeline|layer)|"
    r"include .{5,} and .{5,}|multiple .{3,} (and|with)|"
    r"end[- ]to[- ]end|from scratch|entire|whole)"
    r"\b",
    re.IGNORECASE,
)

# Expert-level indicators
_EXPERT_MARKERS = re.compile(
    r"\b(prove|formal proof|mathematical proof|theorem|lemma|corollary|"
    r"research paper|academic|peer[- ]review|literature review|"
    r"distributed system|distributed cache|consensus algorithm|type theory|category theory|"
    r"compiler design|operating system|kernel|"
    r"neural network architecture|transformer architecture|"
    r"cryptography|zero[- ]knowledge|homomorphic|"
    r"differential equation|partial differential|fourier|laplace|"
    r"quantum|topology|abstract algebra|"
    r"security audit|penetration test|threat model|"
    r"microservice|event[- ]sourcing|cqrs|saga pattern|"
    r"consistent hashing|hash ring|replication|sharding|partitioning|"
    r"fault tolerance|circuit breaker|load balanc|cap theorem|"
    r"eventual consistency|strong consistency|linearizab|"
    r"raft|paxos|vector clock|crdt|"
    r"riemann|eigenvalue|eigenvector|manifold|hilbert|banach|"
    r"turing machine|halting problem|np[- ]hard|np[- ]complete|"
    r"bayesian|markov chain|monte carlo|gradient descent|"
    r"formal verification|model checking|temporal logic|"
    r"protocol design|distributed consensus|byzantine)\b",
    re.IGNORECASE,
)

# Code complexity markers — beyond simple "write a function"
_CODE_COMPLEXITY_MARKERS = re.compile(
    r"\b(design pattern|decorator pattern|factory|singleton|observer|"
    r"async|concurrent|parallel|thread[- ]safe|race condition|"
    r"generic|polymorphi|inheritance hierarchy|"
    r"dependency injection|inversion of control|"
    r"state machine|parser|lexer|ast|"
    r"database schema|migration|orm|"
    r"websocket|streaming|real[- ]time|"
    r"authentication|authorization|oauth|jwt|rbac|"
    r"ci/cd|deployment|docker|kubernetes|"
    r"unit test|integration test|e2e|coverage|"
    r"performance|benchmark|profil|memory leak|"
    r"api design|rest|graphql|grpc)\b",
    re.IGNORECASE,
)

# Multi-file / multi-component indicators
_MULTI_COMPONENT = re.compile(
    r"\b(multiple files|several (classes|components|modules|files)|"
    r"full stack|frontend and backend|client and server|"
    r"microservice|monorepo|entire (application|project|codebase))\b",
    re.IGNORECASE,
)

# Reasoning depth indicators
_REASONING_DEPTH = re.compile(
    r"\b(why does|explain (how|why|the mechanism)|"
    r"what (would happen|are the implications)|"
    r"derive|deduce|infer from|given that|"
    r"if .{10,} then .{10,}|"  # conditional reasoning
    r"considering .{10,} and .{10,}|"  # multi-factor
    r"first .{5,} then .{5,} finally)\b",  # sequential reasoning
    re.IGNORECASE,
)

# Quality-demanding user signals
_QUALITY_SIGNALS = re.compile(
    r"\b(make sure|ensure|carefully|precisely|accurately|"
    r"don't miss|no mistakes|correct|rigorous|"
    r"best practice|industry standard|professional|"
    r"production|deployment|real[- ]world)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_complexity(request) -> Complexity:
    """
    Analyze a ChatCompletionRequest and return its complexity tier.
    Never raises; defaults to MODERATE on unexpected input.
    """
    messages = getattr(request, "messages", None) or []
    if not messages:
        return Complexity.MODERATE

    # Extract text content
    last_user = _last_user_text(messages)
    all_user_text = _all_user_text(messages)
    system_text = _system_text(messages)
    conversation_turns = sum(1 for m in messages if getattr(m, "role", None) == "user")

    # ── Fast path: trivial detection ──
    if last_user and _RE_GREETING.match(last_user.strip()):
        return Complexity.TRIVIAL

    # Very short single-turn with no system prompt → likely trivial
    # But ONLY if it's truly filler (< 10 chars)
    if len(last_user.strip()) < 10 and conversation_turns <= 1 and not system_text:
        return Complexity.TRIVIAL

    # ── Scoring system ──
    score = 0.0

    # Any non-trivial question starts at a baseline of 2 (SIMPLE floor)
    # This prevents real questions from being scored as TRIVIAL
    if len(last_user.strip()) >= 10:
        score += 2.0

    # Factor 1: Message length (longer = more complex task description)
    user_len = len(last_user)
    if user_len < 50:
        score += 0
    elif user_len < 150:
        score += 1
    elif user_len < 400:
        score += 2
    elif user_len < 1000:
        score += 3
    elif user_len < 3000:
        score += 4
    else:
        score += 5

    # Factor 2: Conversation depth (multi-turn = building on context)
    if conversation_turns >= 5:
        score += 2
    elif conversation_turns >= 3:
        score += 1

    # Factor 3: Total conversation context size
    total_text_len = len(all_user_text) + len(system_text)
    if total_text_len > 10000:
        score += 3
    elif total_text_len > 4000:
        score += 2
    elif total_text_len > 1500:
        score += 1

    # Factor 4: System prompt complexity (sophisticated system prompts
    # indicate the user expects high-quality, nuanced responses)
    if system_text:
        sys_len = len(system_text)
        if sys_len > 2000:
            score += 3
        elif sys_len > 500:
            score += 2
        elif sys_len > 100:
            score += 1

    # Factor 5: Task complexity markers
    complex_hits = len(_COMPLEX_TASK_MARKERS.findall(last_user))
    score += min(complex_hits * 1.5, 4)

    # Factor 6: Expert-level markers
    expert_hits = len(_EXPERT_MARKERS.findall(last_user + " " + system_text))
    score += min(expert_hits * 2.5, 6)

    # Factor 7: Code complexity
    code_hits = len(_CODE_COMPLEXITY_MARKERS.findall(last_user))
    score += min(code_hits * 1.0, 3)

    # Factor 8: Multi-component requests
    if _MULTI_COMPONENT.search(last_user):
        score += 2.5

    # Factor 9: Reasoning depth
    reasoning_hits = len(_REASONING_DEPTH.findall(last_user))
    score += min(reasoning_hits * 1.5, 3)

    # Factor 10: Quality signals from user
    quality_hits = len(_QUALITY_SIGNALS.findall(last_user))
    score += min(quality_hits * 0.5, 2)

    # Factor 11: Code blocks in the message (pasting code for review/fix)
    code_blocks = last_user.count("```")
    if code_blocks >= 4:
        score += 2  # Multiple code blocks = complex context
    elif code_blocks >= 2:
        score += 1

    # Factor 12: Numbered/bulleted lists (multi-requirement tasks)
    numbered_items = len(re.findall(r"^\s*\d+[.)]\s", last_user, re.MULTILINE))
    bullet_items = len(re.findall(r"^\s*[-*•]\s", last_user, re.MULTILINE))
    list_items = numbered_items + bullet_items
    if list_items >= 5:
        score += 2
    elif list_items >= 3:
        score += 1

    # Factor 13: Intent signal boost — if the intent classifier already
    # detected code/reasoning/creative, the request is at least moderate
    # complexity (not trivial/simple filler). Import here to avoid circular.
    from app.routing.intent_classifier import classify as _classify
    try:
        intent = _classify(request)
        if intent in ("code", "reasoning"):
            score += 1.5  # These intents indicate non-trivial tasks
        elif intent == "creative":
            score += 1.0
    except Exception:
        pass

    # ── Map score to tier ──
    if score <= 1.5:
        return Complexity.TRIVIAL
    elif score <= 3.5:
        return Complexity.SIMPLE
    elif score <= 7:
        return Complexity.MODERATE
    elif score <= 12:
        return Complexity.COMPLEX
    else:
        return Complexity.EXPERT


def minimum_quality_for_complexity(complexity: Complexity) -> int:
    """
    Return the minimum model quality rating (1-5) appropriate for
    the given complexity tier. Models below this threshold should be
    deprioritized (not excluded — they're still fallbacks).
    """
    return {
        Complexity.TRIVIAL:  1,  # Any model is fine
        Complexity.SIMPLE:   2,  # At least a decent small model
        Complexity.MODERATE: 3,  # Mid-tier and above
        Complexity.COMPLEX:  4,  # Only high-quality models
        Complexity.EXPERT:   5,  # Flagship models preferred
    }[complexity]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _last_user_text(messages) -> str:
    for msg in reversed(messages):
        if getattr(msg, "role", None) != "user":
            continue
        c = getattr(msg, "content", None)
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(
                p.get("text", "") for p in c
                if isinstance(p, dict) and p.get("type") == "text"
            )
    return ""


def _all_user_text(messages) -> str:
    parts = []
    for msg in messages:
        if getattr(msg, "role", None) != "user":
            continue
        c = getattr(msg, "content", None)
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts.append(" ".join(
                p.get("text", "") for p in c
                if isinstance(p, dict) and p.get("type") == "text"
            ))
    return " ".join(parts)


def _system_text(messages) -> str:
    parts = []
    for msg in messages:
        if getattr(msg, "role", None) != "system":
            continue
        c = getattr(msg, "content", None)
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts.append(" ".join(
                p.get("text", "") for p in c
                if isinstance(p, dict) and p.get("type") == "text"
            ))
    return " ".join(parts)
