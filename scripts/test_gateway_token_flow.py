#!/usr/bin/env python3
"""
End-to-end test for gateway-token lifecycle + AI capabilities.

Flow:
  1. CREATE a new gateway token (with and without expiry).
  2. LIST tokens — newly created token must appear with correct fields.
  3. Simulate page-refresh: reload list and confirm token persists.
  4. USE the token's plaintext key as Bearer for /v1/* endpoints:
        a) /v1/models
        b) /v1/chat/completions (auto-routing)
        c) Tool / function-calling
        d) JSON-mode (response_format)
        e) Multi-turn conversation
        f) Vision (multimodal)
  5. PATCH (rename + add expiry) and verify list reflects update.
  6. REVOKE (active=false) and confirm token is rejected by /v1/*.
  7. DELETE and confirm 404 on subsequent fetch.

Usage:
    ADMIN_BEARER=<existing-admin-token> python3 scripts/test_gateway_token_flow.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any
import urllib.request
import urllib.error

BASE = os.environ.get("BASE", "http://localhost:8080").rstrip("/")
ADMIN = os.environ.get("ADMIN_BEARER", "").strip()

GREEN = "\033[32m"; RED = "\033[31m"; DIM = "\033[2m"; BOLD = "\033[1m"; END = "\033[0m"

results: list[tuple[str, bool, str]] = []


def log(name: str, ok: bool, detail: str = "") -> None:
    icon = f"{GREEN}OK {END}" if ok else f"{RED}FAIL{END}"
    extra = f"  {DIM}{detail}{END}" if detail else ""
    print(f"  {icon}  {name}{extra}")
    results.append((name, ok, detail))


def section(t: str) -> None:
    print(f"\n{BOLD}── {t} ──{END}")


def http(method: str, path: str, *, token: str = "", body: Any = None,
         headers: dict | None = None, timeout: int = 60) -> tuple[int, dict, bytes]:
    h = {"Accept": "application/json"}
    if headers:
        h.update(headers)
    if token:
        h["Authorization"] = f"Bearer {token}"
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read() or b""


def main() -> int:
    if not ADMIN:
        print(f"{RED}Set ADMIN_BEARER=<existing-admin-token>{END}")
        return 2

    print(f"{BOLD}Gateway token + AI capability test → {BASE}{END}")
    print(f"{DIM}admin: {ADMIN[:14]}…{END}")

    # ── 1. CREATE ─────────────────────────────────────────────────────────────
    section("1. Create new token")

    # 1a. No expiry
    sc, _, body = http("POST", "/api/gateway/tokens", token=ADMIN,
                       body={"name": "e2e-no-expiry"})
    ok = sc == 201
    new_a = json.loads(body) if ok else {}
    key_a = new_a.get("key", "")
    id_a  = new_a.get("id", "")
    log("create token (no expiry)", ok and key_a.startswith("arbiter-sk-"),
        f"sc={sc} id={id_a} key={key_a[:18]}…")

    # 1b. With future expiry (24h from now)
    expiry = int(time.time()) + 86400
    sc, _, body = http("POST", "/api/gateway/tokens", token=ADMIN,
                       body={"name": "e2e-with-expiry", "expires_at": expiry})
    ok = sc == 201
    new_b = json.loads(body) if ok else {}
    key_b = new_b.get("key", "")
    id_b  = new_b.get("id", "")
    log("create token (with expiry)", ok and new_b.get("expires_at") == expiry,
        f"sc={sc} id={id_b} expires={new_b.get('expires_at')}")

    # ── 2. LIST after creation ────────────────────────────────────────────────
    section("2. List tokens immediately after creation")
    sc, _, body = http("GET", "/api/gateway/tokens", token=ADMIN)
    data = json.loads(body) if sc == 200 else {}
    tokens = data.get("tokens", [])
    ids = {t["id"] for t in tokens}
    log("both new tokens visible in list", id_a in ids and id_b in ids,
        f"count={len(tokens)} env_keys={data.get('env_keys_count', 0)}")

    a_in_list = next((t for t in tokens if t["id"] == id_a), None)
    b_in_list = next((t for t in tokens if t["id"] == id_b), None)
    log("no-expiry token: expires_at == null", bool(a_in_list and a_in_list.get("expires_at") is None),
        f"value={a_in_list.get('expires_at') if a_in_list else 'missing'}")
    log("with-expiry token: expires_at preserved", bool(b_in_list and b_in_list.get("expires_at") == expiry),
        f"value={b_in_list.get('expires_at') if b_in_list else 'missing'}")
    log("masked key returned (not plaintext)",
        bool(a_in_list and "*" in a_in_list.get("key", "") and len(a_in_list.get("key", "")) < 30),
        f"masked={a_in_list.get('key') if a_in_list else '-'}")

    # ── 3. Simulate page refresh — re-fetch ──────────────────────────────────
    section("3. Page refresh — tokens persist")
    time.sleep(0.5)
    sc, _, body = http("GET", "/api/gateway/tokens", token=ADMIN)
    data = json.loads(body) if sc == 200 else {}
    refreshed_ids = {t["id"] for t in data.get("tokens", [])}
    log("tokens persist after refresh", id_a in refreshed_ids and id_b in refreshed_ids,
        f"refreshed_count={len(refreshed_ids)}")

    # ── 4. AI capabilities using the freshly-created token ────────────────────
    section("4. AI capabilities — using newly created token as Bearer")

    # 4a. /v1/models
    sc, _, body = http("GET", "/v1/models", token=key_a)
    n_models = len(json.loads(body).get("data", [])) if sc == 200 else 0
    log("/v1/models with new bearer", sc == 200 and n_models > 5, f"sc={sc} n={n_models}")

    # 4b. Basic chat completion (auto-routing)
    sc, h, body = http("POST", "/v1/chat/completions", token=key_a, body={
        "model": "auto",
        "messages": [{"role": "user", "content": "Reply with the single word: PONG"}],
        "max_tokens": 5,
        "temperature": 0,
    })
    used = h.get("X-Arbiter-Model-Used") or h.get("x-arbiter-model-used") or ""
    txt = ""
    if sc == 200:
        try:
            txt = json.loads(body)["choices"][0]["message"]["content"]
        except Exception:
            pass
    log("chat: auto-routing returned content", sc == 200 and len(txt) > 0,
        f"sc={sc} used={used} reply={txt[:30]!r}")

    # 4c. Multi-turn conversation
    sc, _, body = http("POST", "/v1/chat/completions", token=key_a, body={
        "model": "auto",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant. Reply briefly."},
            {"role": "user", "content": "My name is Alex."},
            {"role": "assistant", "content": "Nice to meet you, Alex."},
            {"role": "user", "content": "What is my name? Reply with just the name."},
        ],
        "max_tokens": 10,
        "temperature": 0,
    })
    txt = ""
    if sc == 200:
        try:
            txt = json.loads(body)["choices"][0]["message"]["content"].strip()
        except Exception:
            pass
    log("chat: multi-turn context preserved", sc == 200 and "alex" in txt.lower(),
        f"sc={sc} reply={txt[:40]!r}")

    # 4d. Tool / function calling
    tool_def = [{
        "type": "function",
        "function": {
            "name": "get_current_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city":  {"type": "string", "description": "City name"},
                    "units": {"type": "string", "enum": ["celsius", "fahrenheit"]},
                },
                "required": ["city"],
            },
        },
    }]
    sc, _, body = http("POST", "/v1/chat/completions", token=key_a, body={
        "model": "llama-3.3-70b-versatile",  # groq model, plain id
        "messages": [
            {"role": "user", "content": "Call get_current_weather for Paris in celsius."},
        ],
        "tools": tool_def,
        "tool_choice": "required",  # force the model to invoke a tool
        "max_tokens": 150,
        "temperature": 0,
    })
    has_tool_call = False
    tool_args: Any = None
    if sc == 200:
        try:
            choice = json.loads(body)["choices"][0]["message"]
            tcs = choice.get("tool_calls") or []
            if tcs:
                has_tool_call = tcs[0].get("function", {}).get("name") == "get_current_weather"
                raw_args = tcs[0].get("function", {}).get("arguments", "{}")
                tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except Exception as exc:
            tool_args = f"parse-error: {exc}"
    log("chat: function-calling works", sc == 200 and has_tool_call,
        f"sc={sc} args={tool_args}")

    # 4e. JSON mode / response_format
    sc, _, body = http("POST", "/v1/chat/completions", token=key_a, body={
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": "You output strictly valid JSON only."},
            {"role": "user", "content": 'Return JSON: {"city":"Tokyo","country":"Japan"}'},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 60,
        "temperature": 0,
    })
    parsed = None
    if sc == 200:
        try:
            content = json.loads(body)["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except Exception:
            pass
    log("chat: JSON mode produces valid JSON",
        sc == 200 and isinstance(parsed, dict) and "city" in parsed,
        f"sc={sc} parsed={parsed}")

    # 4f. Vision / multimodal
    # 1×1 transparent PNG
    px = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    sc, h, body = http("POST", "/v1/chat/completions", token=key_a, body={
        "model": "auto",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Describe this image in 5 words."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{px}"}},
        ]}],
        "max_tokens": 30,
        "temperature": 0,
    })
    used = h.get("X-Arbiter-Model-Used", "") or h.get("x-arbiter-model-used", "")
    txt = ""
    if sc == 200:
        try:
            txt = json.loads(body)["choices"][0]["message"]["content"]
        except Exception:
            pass
    log("chat: vision request routed to multimodal model",
        sc == 200 and len(txt) > 0,
        f"sc={sc} used={used} chars={len(txt)}")

    # ── 5. PATCH — rename + add/remove expiry ─────────────────────────────────
    section("5. Update token (PATCH)")
    new_expiry = int(time.time()) + 7 * 86400
    sc, _, body = http("PATCH", f"/api/gateway/tokens/{id_a}", token=ADMIN,
                       body={"name": "e2e-renamed", "expires_at": new_expiry})
    updated = json.loads(body) if sc == 200 else {}
    log("PATCH: name + expiry update",
        sc == 200 and updated.get("name") == "e2e-renamed" and updated.get("expires_at") == new_expiry,
        f"sc={sc} name={updated.get('name')} expires={updated.get('expires_at')}")

    # Verify list reflects update
    sc, _, body = http("GET", "/api/gateway/tokens", token=ADMIN)
    after = next((t for t in json.loads(body).get("tokens", []) if t["id"] == id_a), None)
    log("list reflects PATCH",
        bool(after and after.get("name") == "e2e-renamed" and after.get("expires_at") == new_expiry),
        f"name={after.get('name') if after else '-'}")

    # ── 6. REVOKE (active=false) ─────────────────────────────────────────────
    section("6. Revoke token — confirm /v1/* is rejected")
    sc, _, _ = http("PATCH", f"/api/gateway/tokens/{id_a}", token=ADMIN, body={"active": False})
    log("PATCH active=false", sc == 200, f"sc={sc}")

    # Token A should now fail on /v1/*
    sc, _, _ = http("POST", "/v1/chat/completions", token=key_a, body={
        "model": "auto",
        "messages": [{"role": "user", "content": "test"}],
        "max_tokens": 5,
    }, timeout=15)
    log("revoked token blocked on /v1/*", sc == 401, f"sc={sc} (want 401)")

    # ── 6b. Per-token counters incremented ────────────────────────────────────
    section("6b. Per-token usage tracking")
    sc, _, body = http("GET", "/api/gateway/tokens", token=ADMIN)
    tokens_list = json.loads(body).get("tokens", []) if sc == 200 else []
    a_after = next((t for t in tokens_list if t["id"] == id_a), None)
    log("token A request_count incremented from chat calls",
        bool(a_after and (a_after.get("request_count", 0) >= 1)),
        f"request_count={a_after.get('request_count') if a_after else None}")
    log("token A last_used_at populated",
        bool(a_after and a_after.get("last_used_at")),
        f"last_used_at={a_after.get('last_used_at') if a_after else None}")

    # Detailed stats endpoint
    sc, _, body = http("GET", f"/api/gateway/tokens/{id_a}/stats", token=ADMIN)
    stats = json.loads(body) if sc == 200 else {}
    log("/tokens/{id}/stats returns summary",
        sc == 200 and stats.get("summary", {}).get("requests", 0) >= 1,
        f"sc={sc} summary={stats.get('summary')}")

    # ── 7. DELETE both test tokens ───────────────────────────────────────────
    section("7. Cleanup (DELETE)")
    for tid in (id_a, id_b):
        sc, _, _ = http("DELETE", f"/api/gateway/tokens/{tid}", token=ADMIN)
        log(f"DELETE {tid}", sc == 200, f"sc={sc}")

    # 404 on subsequent fetch
    sc, _, _ = http("DELETE", f"/api/gateway/tokens/{id_a}", token=ADMIN)
    log("re-DELETE returns 404", sc == 404, f"sc={sc}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = total - passed
    color = GREEN if failed == 0 else RED
    print(f"{BOLD}{color}{'='*70}")
    print(f"Result: {passed}/{total} passed   ({failed} failed){END}")
    if failed:
        print(f"\n{RED}Failures:{END}")
        for n, ok, d in results:
            if not ok:
                print(f"  - {n}  ({d})")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
