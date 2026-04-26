#!/usr/bin/env bash
# Exhaustive probe of every provider × free-model combination.
# Prints OK / RATE / FAIL for each so we can prune dead models.
set -u
BASE="${1:-http://localhost:8080}"
PROMPT='{"messages":[{"role":"user","content":"Reply with exactly: pong"}],"temperature":0.9,"max_tokens":8}'

probe() {
  local vendor="$1" model="$2"
  local body
  body=$(jq -nc --argjson base "$PROMPT" --arg m "$model" '$base + {model:$m}')
  local out code
  out=$(curl -s -m 60 -w "|%{http_code}" -X POST "$BASE/v1/chat/completions?vendor=$vendor" \
        -H "Content-Type: application/json" -d "$body")
  code="${out##*|}"
  json="${out%|*}"
  local used
  used=$(echo "$json" | jq -r '.model // .choices[0].message.content[0:40] // .detail[0:80] // "?"' 2>/dev/null)
  printf "  %-8s %-45s http=%s used=%s\n" "$vendor" "$model" "$code" "$used"
}

echo "=== Gemini ==="
for m in gemini-3.1-flash-lite-preview gemini-3-flash-preview gemini-2.5-flash-lite gemini-2.5-flash; do
  probe gemini "$m"
done

echo "=== Groq ==="
for m in llama-3.1-8b-instant llama-3.3-70b-versatile meta-llama/llama-4-scout-17b-16e-instruct qwen/qwen3-32b moonshotai/kimi-k2-instruct moonshotai/kimi-k2-instruct-0905 openai/gpt-oss-20b openai/gpt-oss-120b; do
  probe groq "$m"
done

echo "=== OpenRouter ==="
for m in meta-llama/llama-3.3-70b-instruct:free nousresearch/hermes-3-llama-3.1-405b:free google/gemma-3-27b-it:free mistralai/mistral-small-3.1-24b-instruct:free google/gemma-3-12b-it:free qwen/qwen3-4b:free meta-llama/llama-3.2-3b-instruct:free; do
  probe openrouter "$m"
done

echo "=== Cohere ==="
for m in command-r7b-12-2024 command-r-08-2024 command-r-plus-08-2024 command-a-03-2025; do
  probe cohere "$m"
done

echo "=== Cloudflare ==="
for m in @cf/meta/llama-4-scout-17b-16e-instruct @cf/meta/llama-3.3-70b-instruct-fp8-fast @cf/moonshot/kimi-k2.5 @cf/qwen/qwen3-30b-a3b-fp8 @cf/mistralai/mistral-small-3.1-24b-instruct @cf/deepseek/deepseek-r1-distill-qwen-32b @cf/qwen/qwq-32b @cf/qwen/qwen2.5-coder-32b-instruct @cf/google/gemma-3-12b-it @cf/meta/llama-3.1-8b-instruct @cf/meta/llama-3.2-3b-instruct; do
  probe cloudflare "$m"
done

echo "=== Cerebras ==="
for m in llama3.1-8b gpt-oss-120b qwen-3-235b-a22b-instruct-2507 zai-glm-4.7; do
  probe cerebras "$m"
done

echo "=== HuggingFace ==="
for m in Qwen/Qwen2.5-7B-Instruct mistralai/Mistral-7B-Instruct-v0.3 HuggingFaceH4/zephyr-7b-beta google/gemma-2-2b-it; do
  probe huggingface "$m"
done

echo "=== Pollinations ==="
for m in mistral mistral-large openai; do
  probe pollinations "$m"
done

echo "=== Routeway (15 :free) ==="
for m in llama-3.3-70b-instruct:free gpt-oss-120b:free kimi-k2-0905:free glm-4.5-air:free minimax-m2:free devstral-2512:free ling-2.6-flash:free step-3.5-flash:free gemma-4-31b-it:free nemotron-3-nano-30b-a3b:free nemotron-nano-9b-v2:free llama-3.1-8b-instruct:free llama-3.2-3b-instruct:free llama-3.2-1b-instruct:free mistral-nemo-instruct:free; do
  probe routeway "$m"
done
