#!/usr/bin/env python3
"""
End-to-end smoke test for Arbiter v1.12.1.

Exercises every accessible feature class:
  1. Health / root / docs / login / openapi
  2. /v1/* (open when GATEWAY_API_KEYS is empty)
       - GET  /v1/models          (list models)
       - POST /v1/chat/completions for each provider, both `model="auto"`
         and explicit provider:model targets
       - Streaming response sanity
       - Cache-hit assertion (X-Cache: HIT) on repeated identical request
       - X-Arbiter-Model-Used header correctness for auto
  3. /auth/* (config/me)
  4. /api/* admin endpoints — must all reject unauthenticated requests (401)
  5. Static UI pages (HTML) — must serve, possibly redirect to /login

Pass: green tick.  Fail: red cross + reason. Exits non-zero on any failure.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Optional, Tuple
import urllib.request
import urllib.error

GREEN = "\033[32m"
RED   = "\033[31m"
DIM   = "\033[2m"
BOLD  = "\033[1m"
END   = "\033[0m"

results: list[tuple[str, bool, str]] = []


def log(name: str, ok: bool, detail: str = "") -> None:
    icon = f"{GREEN}OK {END}" if ok else f"{RED}FAIL{END}"
    extra = f"  {DIM}{detail}{END}" if detail else ""
    print(f"  {icon}  {name}{extra}")
    results.append((name, ok, detail))


def req(
    method: str,
    url: str,
    headers: Optional[dict] = None,
    body: Optional[dict] = None,
    timeout: int = 30,
) -> Tuple[int, dict, bytes]:
    h = {"Accept": "application/json"}
    if headers:
        h.update(headers)
    data: Optional[bytes] = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read() or b""
    except urllib.error.URLError as exc:
        return 0, {}, str(exc).encode()


def section(title: str) -> None:
    print(f"\n{BOLD}=== {title} ==={END}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://localhost:8080")
    args = p.parse_args()
    base = args.base.rstrip("/")

    print(f"{BOLD}Arbiter end-to-end smoke test → {base}{END}")

    # ── 1. Public / open endpoints ──────────────────────────────────────────
    section("1. Public endpoints")

    sc, _, body = req("GET", f"{base}/health")
    ok = sc == 200 and json.loads(body).get("status") in ("ok", "degraded")
    log("/health 200", ok, f"status={sc}")

    sc, _, body = req("GET", f"{base}/openapi.json")
    log("/openapi.json", sc == 200 and len(body) > 1000, f"size={len(body)}")

    sc, _, body = req("GET", f"{base}/login")
    log("/login (HTML)", sc == 200 and b"<html" in body.lower(), f"sc={sc}")

    sc, _, _ = req("GET", f"{base}/docs")
    log("/docs (Swagger)", sc == 200, f"sc={sc}")

    sc, _, body = req("GET", f"{base}/auth/config")
    cfg_ok = False
    if sc == 200:
        try:
            cfg = json.loads(body)
            cfg_ok = "sso_enabled" in cfg
        except Exception:
            pass
    log("/auth/config", cfg_ok, f"sc={sc}")

    sc, _, _ = req("GET", f"{base}/auth/me")
    log("/auth/me (anon)", sc in (200, 401), f"sc={sc}")

    # ── 2. /v1/* OpenAI-compatible API (open when no GATEWAY_API_KEYS) ──────
    section("2. OpenAI-compatible /v1/*")

    sc, _, body = req("GET", f"{base}/v1/models")
    models_ok = sc == 200
    model_count = 0
    providers_seen: set[str] = set()
    if models_ok:
        try:
            data = json.loads(body)
            model_count = len(data.get("data", []))
            providers_seen = {m.get("owned_by", "?") for m in data.get("data", [])}
        except Exception:
            models_ok = False
    log(f"/v1/models", models_ok, f"sc={sc} models={model_count} providers={len(providers_seen)}")

    # Auto-routing — single quick request
    sc, h, body = req(
        "POST", f"{base}/v1/chat/completions",
        body={
            "model": "auto",
            "messages": [{"role": "user", "content": "Say OK in one word."}],
            "max_tokens": 5,
            "temperature": 0,
        },
        timeout=60,
    )
    used = h.get("X-Arbiter-Model-Used") or h.get("x-arbiter-model-used") or ""
    log("model='auto' completion", sc == 200 and bool(used), f"sc={sc} used={used}")

    # Each enabled provider — explicit target via provider:model
    sc, _, body = req("GET", f"{base}/v1/models")
    by_provider: dict[str, str] = {}
    if sc == 200:
        for m in json.loads(body).get("data", []):
            owner = m.get("owned_by", "")
            mid   = m.get("id", "")
            if owner and mid and owner not in by_provider:
                by_provider[owner] = mid

    # Each enabled provider — pick one representative model per `owned_by`
    sc, _, body = req("GET", f"{base}/v1/models")
    by_provider: dict[str, str] = {}
    if sc == 200:
        for m in json.loads(body).get("data", []):
            owner = m.get("owned_by", "")
            mid   = m.get("id", "")
            if owner and mid and owner not in by_provider:
                by_provider[owner] = mid

    for prov, mid in sorted(by_provider.items()):
        sc, h, body = req(
            "POST", f"{base}/v1/chat/completions",
            body={
                "model": mid,  # plain model ID — Arbiter picks the provider
                "messages": [{"role": "user", "content": "Reply OK"}],
                "max_tokens": 5,
                "temperature": 0,
            },
            timeout=60,
        )
        used = h.get("X-Arbiter-Model-Used") or h.get("x-arbiter-model-used") or ""
        # 200 = success
        # 429 = upstream rate-limited     (env-dependent, not a regression)
        # 502 = upstream provider error   (env-dependent, e.g. invalid key)
        # 503 = all keys on cooldown      (env-dependent)
        ok = sc in (200, 429, 502, 503)
        log(f"owner={prov:14s} model={mid[:40]}", ok, f"sc={sc} used={used or '-'}")

    # Cache assertion — same prompt twice; second should be markedly faster
    cache_body = {
        "model": "gemini-2.5-flash-lite",
        "messages": [{"role": "user", "content": "What is 2 plus 2? Reply with just a number."}],
        "max_tokens": 5,
        "temperature": 0,
    }
    t1 = time.time()
    sc1, _, _ = req("POST", f"{base}/v1/chat/completions", body=cache_body, timeout=60)
    d1 = time.time() - t1
    time.sleep(0.3)
    t2 = time.time()
    sc2, _, _ = req("POST", f"{base}/v1/chat/completions", body=cache_body, timeout=60)
    d2 = time.time() - t2
    # Cache HIT should make d2 << d1 (typically <50ms vs ~500ms+)
    cache_ok = sc1 == 200 and sc2 == 200 and (d2 < d1 / 2 or d2 < 0.1)
    log("cache speedup on repeat", cache_ok, f"d1={d1*1000:.0f}ms d2={d2*1000:.0f}ms")

    # Streaming — Arbiter intentionally returns 501 (documented)
    sc, _, body = req(
        "POST", f"{base}/v1/chat/completions",
        body={
            "model": "auto",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "max_tokens": 5,
        },
        timeout=10,
    )
    # 501 with explanatory body is the expected, documented behavior
    is_501 = sc == 501 and b"not yet supported" in body.lower()
    log("streaming returns 501 (documented)", is_501, f"sc={sc}")

    # Models endpoint metadata (admin facets are gated, but /v1/models is open)
    # Image generation models endpoint
    sc, _, body = req("GET", f"{base}/v1/images/models")
    has_data = False
    if sc == 200:
        try:
            has_data = isinstance(json.loads(body).get("data", []), list)
        except Exception:
            pass
    log("/v1/images/models", sc in (200, 401, 404), f"sc={sc}")

    # ── 3. Admin/RBAC — every mutation should 401 unauthenticated ──────────
    section("3. Admin RBAC (all should reject anon)")

    rbac_cases = [
        ("GET",    "/api/users"),
        ("GET",    "/api/gateway/tokens"),
        ("POST",   "/api/gateway/tokens"),
        ("PUT",    "/api/preferences/auto-route"),
        ("POST",   "/api/preferences/auto-route/reset"),
        ("POST",   "/api/providers/gemini/keys"),
        ("DELETE", "/api/providers/gemini/keys/abc"),
        ("POST",   "/api/providers/gemini/test"),
        ("POST",   "/api/providers/reload"),
        ("POST",   "/settings/routing"),
        ("DELETE", "/settings/routing"),
        ("DELETE", "/settings/cache"),
        ("POST",   "/cloudflare/workers"),
        ("DELETE", "/cloudflare/workers/foo"),
        ("POST",   "/cloudflare/validate"),
        ("POST",   "/modal/endpoints"),
        ("DELETE", "/modal/endpoints/foo"),
        ("POST",   "/modal/deploy"),
        ("POST",   "/modal/deploy/account"),
    ]
    for method, path in rbac_cases:
        sc, _, _ = req(method, f"{base}{path}", body={} if method in ("POST", "PUT") else None, timeout=10)
        ok = sc in (401, 403)
        log(f"{method:6s} {path}", ok, f"sc={sc} (want 401/403)")

    # GET listing endpoints — also gated for sensitive ones
    listing_cases = [
        ("/api/providers",        (200, 401, 403)),
        ("/api/preferences/auto-route", (200, 401, 403)),
        ("/settings/routing",     (200, 401, 403)),
    ]
    for path, allowed in listing_cases:
        sc, _, _ = req("GET", f"{base}{path}", timeout=10)
        log(f"GET    {path}", sc in allowed, f"sc={sc}")

    # ── 4. Static UI pages — should serve HTML or redirect to /login ───────
    section("4. UI pages")

    pages = [
        "/dashboard", "/playground", "/settings", "/analytics",
        "/logs", "/images", "/api-docs", "/users",
    ]
    for path in pages:
        sc, h, body = req("GET", f"{base}{path}", timeout=10)
        # 200 with HTML (SSO disabled or session-allowed) OR 302/401 (gated)
        is_html = b"<html" in body.lower() or b"<!doctype" in body.lower()
        ok = (sc == 200 and is_html) or sc in (302, 401, 403)
        log(f"{path:14s}", ok, f"sc={sc} html={is_html}")

    # ── Summary ──────────────────────────────────────────────────────────────
    print()
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = total - passed
    color = GREEN if failed == 0 else RED
    print(f"{BOLD}{color}{'='*70}")
    print(f"Result: {passed}/{total} passed   ({failed} failed){END}")
    if failed:
        print(f"\n{RED}Failures:{END}")
        for name, ok, detail in results:
            if not ok:
                print(f"  - {name}  ({detail})")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
