#!/usr/bin/env python3
"""
Validation script for v1.12 smart auto-routing.

Sends 7 prompts (one per intent class) with ``model="auto"`` to the local
Arbiter gateway and asserts that the chosen ``X-Arbiter-Model-Used`` header
matches the expected capability tag from FREE_TIER_CATALOG.

Usage:
    python3 scripts/test_auto_routing.py [--base http://localhost:8080] [--token <bearer>]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error

# ── Test cases — (intent_label, prompt, expected_required_tag) ───────────────
CASES = [
    ("code",         "Write a python function to compute fibonacci with memoization. Return code.", "code"),
    ("reasoning",    "Step by step, prove that the square root of 2 is irrational.",                "reasoning"),
    ("long-context", "Summarize this document.\n\n" + ("ALPHA " * 5000),                            "long-context"),
    ("creative",     "Write a short poem about autumn leaves and nostalgia.",                       "creative"),
    ("fast",         "Hi",                                                                          "fast"),
    ("balanced",     "What is the capital of France and why is it famous?",                         "balanced"),
    ("vision",       [
        {"type": "text", "text": "What is in this image? Describe briefly."},
        {"type": "image_url", "image_url": {
            "url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
        }},
    ],                                                                                              "vision"),
]


def post_chat(base: str, token: str | None, prompt) -> tuple[int, dict, dict]:
    body = {
        "model": "auto",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 16,
        "temperature": 0.0,
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, dict(r.headers), json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), json.loads(e.read().decode() or "{}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8080")
    parser.add_argument("--token", default=None)
    args = parser.parse_args()

    # Lazy-import the catalog so we can verify the chosen model has the tag.
    sys.path.insert(0, ".")
    try:
        from app.providers._free_tier_catalog import find_spec  # type: ignore
    except Exception:
        find_spec = None  # type: ignore

    print(f"Testing auto-routing against {args.base}")
    print("=" * 78)

    fails = 0
    for label, prompt, required_tag in CASES:
        status, headers, body = post_chat(args.base, args.token, prompt)
        used = headers.get("x-arbiter-model-used") or headers.get("X-Arbiter-Model-Used") or "-"
        ok_status = status == 200
        chosen_provider, _, chosen_model = used.partition("/")
        # Verify capability tag
        tag_ok = True
        if find_spec and chosen_model:
            spec = find_spec(chosen_model)
            if spec is not None:
                tag_ok = (required_tag in spec.tags) or (
                    required_tag == "vision" and spec.modality in ("multimodal", "vision")
                ) or (required_tag == "balanced")  # balanced has no hard tag
        ok = ok_status and (used != "-") and tag_ok
        print(f"{'OK ' if ok else 'FAIL'} [{label:12s}] -> {used}  (status={status})")
        if not ok:
            fails += 1
            print(f"     body: {json.dumps(body)[:200]}")

    print("=" * 78)
    print(f"Result: {len(CASES) - fails}/{len(CASES)} passed")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
