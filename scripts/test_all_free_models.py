#!/usr/bin/env python3
"""Exhaustive probe of every provider × free-model combination.
Prints OK/RATE/FAIL for each so we can prune dead models."""
import json, sys, urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"

MATRIX = {
    "gemini": [
        "gemini-3.1-flash-lite-preview", "gemini-3-flash-preview",
        "gemini-2.5-flash-lite", "gemini-2.5-flash",
    ],
    "groq": [
        "llama-3.1-8b-instant", "llama-3.3-70b-versatile",
        "meta-llama/llama-4-scout-17b-16e-instruct", "qwen/qwen3-32b",
        "moonshotai/kimi-k2-instruct", "moonshotai/kimi-k2-instruct-0905",
        "openai/gpt-oss-20b", "openai/gpt-oss-120b",
    ],
    "openrouter": [
        "meta-llama/llama-3.3-70b-instruct:free",
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
        "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "@cf/moonshot/kimi-k2.5",
        "@cf/qwen/qwen3-30b-a3b-fp8", "@cf/mistralai/mistral-small-3.1-24b-instruct",
        "@cf/deepseek/deepseek-r1-distill-qwen-32b", "@cf/qwen/qwq-32b",
        "@cf/qwen/qwen2.5-coder-32b-instruct", "@cf/google/gemma-3-12b-it",
        "@cf/meta/llama-3.1-8b-instruct", "@cf/meta/llama-3.2-3b-instruct",
    ],
    "cerebras": [
        "llama3.1-8b", "gpt-oss-120b",
        "qwen-3-235b-a22b-instruct-2507", "zai-glm-4.7",
    ],
    "huggingface": [
        "Qwen/Qwen2.5-7B-Instruct", "mistralai/Mistral-7B-Instruct-v0.3",
        "HuggingFaceH4/zephyr-7b-beta", "google/gemma-2-2b-it",
    ],
    "pollinations": ["mistral", "mistral-large", "openai"],
    "routeway": [
        "llama-3.3-70b-instruct:free", "gpt-oss-120b:free",
        "kimi-k2-0905:free", "glm-4.5-air:free", "minimax-m2:free",
        "devstral-2512:free", "ling-2.6-flash:free", "step-3.5-flash:free",
        "gemma-4-31b-it:free", "nemotron-3-nano-30b-a3b:free",
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
            used = data.get("model", "?")
            content = (data.get("choices", [{}])[0]
                       .get("message", {}).get("content", ""))[:30]
            return 200, used, content
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read()).get("detail", "")[:80]
        except Exception:
            err = e.reason
        return e.code, "-", err
    except Exception as e:
        return 0, "-", f"{type(e).__name__}:{e}"

results = {}
for vendor, models in MATRIX.items():
    print(f"\n=== {vendor} ===")
    results[vendor] = []
    for m in models:
        code, used, detail = probe(vendor, m)
        tag = "OK  " if code == 200 else ("RATE" if code in (429, 503) else "FAIL")
        print(f"  [{tag}] {code:3} {m:55s} -> used={used:40s}  {detail}")
        results[vendor].append({"model": m, "code": code, "used": used, "detail": detail})

# Summary of broken models per provider
print("\n\n===== BROKEN (prune from hierarchy) =====")
for vendor, rs in results.items():
    broken = [r for r in rs if r["code"] not in (200, 429, 503)]
    if broken:
        print(f"\n{vendor}:")
        for r in broken:
            print(f"  - {r['model']}  ({r['code']} {r['detail']})")

print("\n===== WORKING =====")
for vendor, rs in results.items():
    ok = [r for r in rs if r["code"] == 200]
    print(f"{vendor}: {len(ok)}/{len(rs)} OK")
