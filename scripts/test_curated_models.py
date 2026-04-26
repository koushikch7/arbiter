#!/usr/bin/env python3
"""Probe only the curated hierarchies (post-prune).

Expectation after v1.11.2: every model listed here should return 200 OK
when its vendor key is healthy. 429/503 cooldowns are acceptable; 502
means a model definition is wrong and must be removed.
"""
import json, sys, time, urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"

# Mirrors app/routing/router.py::VENDOR_MODEL_HIERARCHY
CURATED = {
    "gemini": [
        "gemini-2.5-flash-lite",
    ],
    "groq": [
        "llama-3.1-8b-instant", "llama-3.3-70b-versatile",
        "meta-llama/llama-4-scout-17b-16e-instruct", "qwen/qwen3-32b",
        "openai/gpt-oss-20b", "openai/gpt-oss-120b",
    ],
    "openrouter": [
        "nousresearch/hermes-3-llama-3.1-405b:free",
        "google/gemma-3-27b-it:free",
        "mistralai/mistral-small-3.1-24b-instruct:free",
        "google/gemma-3-12b-it:free", "qwen/qwen3-4b:free",
        "meta-llama/llama-3.2-3b-instruct:free",
    ],
    "cohere": [
        "command-r7b-12-2024", "command-r-08-2024",
        "command-r-plus-08-2024", "command-a-03-2025",
    ],
    "cloudflare": [
        "@cf/meta/llama-4-scout-17b-16e-instruct",
        "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        "@cf/mistralai/mistral-small-3.1-24b-instruct",
        "@cf/qwen/qwq-32b", "@cf/qwen/qwen2.5-coder-32b-instruct",
        "@cf/google/gemma-3-12b-it",
        "@cf/meta/llama-3.1-8b-instruct", "@cf/meta/llama-3.2-3b-instruct",
    ],
    "cerebras": ["llama3.1-8b", "qwen-3-235b-a22b-instruct-2507"],
    "huggingface": [
        "Qwen/Qwen2.5-7B-Instruct", "meta-llama/Llama-3.1-8B-Instruct",
        "meta-llama/Llama-3.2-1B-Instruct", "openai/gpt-oss-20b",
    ],
    "pollinations": ["openai-fast"],
    "ollama": [
        "gpt-oss:20b-cloud", "glm-4.6:cloud", "minimax-m2:cloud",
        "qwen3-coder:480b-cloud", "gpt-oss:120b-cloud",
        "deepseek-v3.1:671b-cloud",
    ],
    "routeway": [
        "llama-3.3-70b-instruct:free", "devstral-2512:free",
        "ling-2.6-flash:free", "step-3.5-flash:free",
        "nemotron-nano-9b-v2:free", "llama-3.1-8b-instruct:free",
        "llama-3.2-3b-instruct:free", "llama-3.2-1b-instruct:free",
        "mistral-nemo-instruct:free",
    ],
}


def probe(vendor, model):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
        "temperature": 0.9, "max_tokens": 8,
    }).encode()
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions?vendor={vendor}",
        data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
            return 200, data.get("model", "?"), \
                (data.get("choices", [{}])[0].get("message", {}).get("content", ""))[:30]
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read()).get("detail", "")[:80]
        except Exception:
            err = e.reason
        return e.code, "-", err
    except Exception as e:
        return 0, "-", f"{type(e).__name__}:{e}"


totals = {}
for vendor, models in CURATED.items():
    print(f"\n=== {vendor} ===")
    ok = rate = fail = 0
    for m in models:
        code, used, detail = probe(vendor, m)
        if code == 200:
            ok += 1; tag = "OK  "
        elif code in (429, 503):
            rate += 1; tag = "RATE"
        else:
            fail += 1; tag = "FAIL"
        print(f"  [{tag}] {code:3} {m:55s} -> used={used}")
        time.sleep(0.3)  # don't burst the key pool
    totals[vendor] = (ok, rate, fail, len(models))

print("\n===== SUMMARY =====")
for v, (ok, rate, fail, n) in totals.items():
    print(f"  {v:14s}  ok={ok}/{n}  rate-limited={rate}  broken={fail}")
broken_total = sum(t[2] for t in totals.values())
print(f"\nFAIL TOTAL: {broken_total}  "
      f"(should be 0 for a healthy deployment; RATE is OK — it means the key is simply at its quota)")
