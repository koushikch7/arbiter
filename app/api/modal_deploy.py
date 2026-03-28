"""
Modal.com vLLM deployment API — deploy serverless GPU inference from the UI.

Routes
──────
GET  /modal/account                        Check / get Modal account token status
POST /modal/account                        Save Modal account token (ak-id:secret)
GET  /modal/deploy/models                  Curated model catalog with GPU recommendations
POST /modal/deploy                         Start a deployment (async background)
GET  /modal/deploy                         List all deployments
GET  /modal/deploy/{deploy_id}             Deployment status + live logs
DELETE /modal/deploy/{deploy_id}           Stop + remove a deployment

How deployment works
────────────────────
1. User picks model + config in the UI
2. Arbiter generates an optimised vLLM Python script (model volume caching,
   @modal.concurrent for batching, configurable idle timeout)
3. Saves the script to a temp file and runs `modal deploy` as a subprocess
   with MODAL_TOKEN_ID / MODAL_TOKEN_SECRET env vars
4. Deployment log lines are streamed to Redis; the frontend polls every 2 s
5. On success the workers.dev URL is extracted and auto-registered as a
   Modal endpoint (goes straight into the routing pool — no manual step)
6. On the next chat request Arbiter routes it through the new endpoint

Cost efficiency features
────────────────────────
• modal.Volume  — model weights cached on first cold start, reused forever
• container_idle_timeout — container auto-shuts after N seconds of silence
• @modal.concurrent — one GPU instance handles M parallel requests
• gpu_memory_utilization=0.90 — maximise throughput on the chosen GPU
• Optional AWQ 4-bit quantisation for larger models on smaller (cheaper) GPUs
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/modal/deploy", tags=["Modal Deploy"])

# Redis keys
_KEY_TOKEN       = "arbiter:modal:account_token"      # ak-id:secret
_KEY_DEPLOYMENTS = "arbiter:modal:deployments"         # JSON dict
_KEY_LOG_PFX     = "arbiter:modal:deploy_log:"         # + deploy_id → JSON list of lines

# ---------------------------------------------------------------------------
# Curated model catalog
# ---------------------------------------------------------------------------

MODELS = [
    {
        "id":          "meta-llama/Llama-3.1-8B-Instruct",
        "label":       "Llama 3.1 8B Instruct",
        "gpu":         "A10G",
        "vram_gb":     18,
        "max_model_len": 32768,
        "cost_per_hr": 0.72,
        "est_rpm":     60,
        "tier":        "fast",
        "notes":       "Best price/performance — recommended starting point",
        "requires_hf_token": True,
    },
    {
        "id":          "meta-llama/Llama-3.2-3B-Instruct",
        "label":       "Llama 3.2 3B Instruct",
        "gpu":         "T4",
        "vram_gb":     8,
        "max_model_len": 32768,
        "cost_per_hr": 0.36,
        "est_rpm":     100,
        "tier":        "cheapest",
        "notes":       "Cheapest option — great for testing and high-volume tasks",
        "requires_hf_token": True,
    },
    {
        "id":          "mistralai/Mistral-7B-Instruct-v0.3",
        "label":       "Mistral 7B Instruct v0.3",
        "gpu":         "A10G",
        "vram_gb":     16,
        "max_model_len": 32768,
        "cost_per_hr": 0.72,
        "est_rpm":     60,
        "tier":        "fast",
        "notes":       "Strong 7B model, no token needed",
        "requires_hf_token": False,
    },
    {
        "id":          "Qwen/Qwen2.5-7B-Instruct",
        "label":       "Qwen 2.5 7B Instruct",
        "gpu":         "A10G",
        "vram_gb":     16,
        "max_model_len": 32768,
        "cost_per_hr": 0.72,
        "est_rpm":     60,
        "tier":        "fast",
        "notes":       "Excellent multilingual + coding performance",
        "requires_hf_token": False,
    },
    {
        "id":          "Qwen/Qwen2.5-14B-Instruct",
        "label":       "Qwen 2.5 14B Instruct",
        "gpu":         "A10G",
        "vram_gb":     22,
        "max_model_len": 16384,
        "cost_per_hr": 0.72,
        "est_rpm":     40,
        "tier":        "balanced",
        "notes":       "14B fits A10G with quantisation disabled",
        "requires_hf_token": False,
    },
    {
        "id":          "google/gemma-2-9b-it",
        "label":       "Gemma 2 9B Instruct",
        "gpu":         "A10G",
        "vram_gb":     20,
        "max_model_len": 8192,
        "cost_per_hr": 0.72,
        "est_rpm":     50,
        "tier":        "fast",
        "notes":       "Google's efficient 9B — no token needed",
        "requires_hf_token": False,
    },
    {
        "id":          "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
        "label":       "DeepSeek R1 Distill 7B (Reasoning)",
        "gpu":         "A10G",
        "vram_gb":     16,
        "max_model_len": 32768,
        "cost_per_hr": 0.72,
        "est_rpm":     40,
        "tier":        "reasoning",
        "notes":       "Reasoning model distilled from R1 — surprisingly capable",
        "requires_hf_token": False,
    },
    {
        "id":          "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B",
        "label":       "DeepSeek R1 Distill 32B (Reasoning)",
        "gpu":         "A100-40GB",
        "vram_gb":     35,
        "max_model_len": 32768,
        "cost_per_hr": 2.16,
        "est_rpm":     20,
        "tier":        "reasoning",
        "notes":       "Best open reasoning model — requires A100",
        "requires_hf_token": False,
    },
    {
        "id":          "meta-llama/Llama-3.3-70B-Instruct",
        "label":       "Llama 3.3 70B Instruct",
        "gpu":         "A100-80GB",
        "vram_gb":     70,
        "max_model_len": 32768,
        "cost_per_hr": 3.40,
        "est_rpm":     15,
        "tier":        "high-quality",
        "notes":       "State-of-the-art open model — requires A100 80GB",
        "requires_hf_token": True,
    },
    {
        "id":          "Qwen/Qwen2.5-72B-Instruct",
        "label":       "Qwen 2.5 72B Instruct",
        "gpu":         "A100-80GB",
        "vram_gb":     70,
        "max_model_len": 32768,
        "cost_per_hr": 3.40,
        "est_rpm":     15,
        "tier":        "high-quality",
        "notes":       "Best Qwen model — requires A100 80GB",
        "requires_hf_token": False,
    },
]

# GPU → Modal GPU string
_GPU_MAP = {
    "T4":        "T4",
    "A10G":      "A10G",
    "A100-40GB": "A100-40GB",
    "A100-80GB": "A100-80GB",
    "H100":      "H100",
}

# ---------------------------------------------------------------------------
# vLLM deployment script template
# ---------------------------------------------------------------------------

_VLLM_TEMPLATE = '''\
"""
Auto-generated vLLM inference script — deployed by Arbiter.
Model: {model_id}
App:   {app_name}
"""
import time
import modal

MODEL_ID    = "{model_id}"
APP_NAME    = "{app_name}"
GPU         = "{gpu}"
MAX_LEN     = {max_model_len}
IDLE_SECS   = {idle_timeout}
MAX_CONCURRENT = {concurrent_inputs}

app = modal.App(APP_NAME)

# Persistent volume — model weights are cached here across cold starts.
# First cold start downloads the model; subsequent ones reuse the cache.
model_vol = modal.Volume.from_name("arbiter-model-cache", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "vllm>=0.6.0",
        "transformers>=4.44.0",
        "huggingface_hub[hf_transfer]",
        "hf_transfer",
    )
    .env({{
        "HF_HOME":                   "/model-cache",
        "HF_HUB_ENABLE_HF_TRANSFER": "1",
    }})
)

{secret_block}

@app.cls(
    gpu=GPU,
    image=image,
    volumes={{"/model-cache": model_vol}},
    container_idle_timeout=IDLE_SECS,
    timeout=900,
    {secrets_arg}
)
@modal.concurrent(max_inputs=MAX_CONCURRENT)
class VLLMServer:
    @modal.enter()
    def load(self):
        """Called once per container lifecycle — keeps model in GPU memory."""
        from vllm import LLM
        from transformers import AutoTokenizer
        self.llm = LLM(
            model=MODEL_ID,
            gpu_memory_utilization=0.90,
            max_model_len=MAX_LEN,
            trust_remote_code=True,
            {quantization_line}
        )
        self.tokenizer = AutoTokenizer.from_pretrained(
            MODEL_ID, trust_remote_code=True
        )
        has_tmpl = bool(getattr(self.tokenizer, "chat_template", None))
        self._has_chat_template = has_tmpl
        print(f"[arbiter] Model loaded: {{MODEL_ID}}, chat_template={{has_tmpl}}")

    def _build_prompt(self, messages: list) -> str:
        if self._has_chat_template:
            return self.tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
        # Fallback for models without a chat template
        out = ""
        for m in messages:
            r, c = m.get("role", "user"), m.get("content", "")
            if r == "system":
                out += f"{{c}}\\n\\n"
            elif r == "user":
                out += f"Human: {{c}}\\n"
            elif r == "assistant":
                out += f"Assistant: {{c}}\\n"
        return out + "Assistant: "

    @modal.web_endpoint(method="POST", label="{label}")
    def serve(self, request: dict) -> dict:
        from vllm import SamplingParams
        messages = request.get("messages", [])
        if not messages:
            return {{"error": "messages array is required"}}, 400
        prompt = self._build_prompt(messages)
        params = SamplingParams(
            temperature=float(request.get("temperature", 0.7)),
            max_tokens=min(int(request.get("max_tokens", 512)), MAX_LEN // 2),
            top_p=float(request.get("top_p", 0.95)),
            stop=request.get("stop") or None,
        )
        t0 = time.perf_counter()
        outputs = self.llm.generate([prompt], params)
        latency = round((time.perf_counter() - t0) * 1000)
        result   = outputs[0]
        text     = result.outputs[0].text
        finish   = result.outputs[0].finish_reason or "stop"
        c_tokens = len(result.outputs[0].token_ids)
        return {{
            "id":      f"chatcmpl-{{int(time.time())}}",
            "object":  "chat.completion",
            "created": int(time.time()),
            "model":   MODEL_ID,
            "choices": [{{
                "index":         0,
                "message":       {{"role": "assistant", "content": text}},
                "finish_reason": finish,
            }}],
            "usage": {{
                "prompt_tokens":     0,
                "completion_tokens": c_tokens,
                "total_tokens":      c_tokens,
            }},
            "x_arbiter": {{"latency_ms": latency}},
        }}
'''


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class SaveTokenBody(BaseModel):
    token: str   # ak-XXXXX:as-YYYYY  or  MODAL_TOKEN_ID:MODAL_TOKEN_SECRET


class DeployBody(BaseModel):
    model_id:          str
    name:              str                # friendly label, used as app/endpoint name
    gpu:               Optional[str] = None     # override GPU recommendation
    max_model_len:     Optional[int] = None
    idle_timeout:      int  = 300         # seconds — cost optimisation
    concurrent_inputs: int  = 8           # requests per container — throughput
    hf_token:          Optional[str] = None   # HuggingFace token for gated models


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------

async def _load_deployments(redis) -> dict:
    raw = await redis.get(_KEY_DEPLOYMENTS)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


async def _save_deployments(redis, data: dict) -> None:
    await redis.set(_KEY_DEPLOYMENTS, json.dumps(data))


async def _append_log(redis, deploy_id: str, line: str) -> None:
    key = f"{_KEY_LOG_PFX}{deploy_id}"
    raw = await redis.get(key)
    lines = json.loads(raw) if raw else []
    lines.append(line)
    await redis.set(key, json.dumps(lines[-200:]))  # keep last 200 lines


async def _get_logs(redis, deploy_id: str) -> list:
    raw = await redis.get(f"{_KEY_LOG_PFX}{deploy_id}")
    return json.loads(raw) if raw else []


def _parse_token(token: str) -> tuple[str, str]:
    """Split  ak-XXX:as-YYY  →  (ak-XXX, as-YYY)."""
    token = token.strip()
    idx = token.find(":")
    if idx == -1:
        raise ValueError("Token must be in format  token_id:token_secret")
    return token[:idx].strip(), token[idx+1:].strip()


def _extract_url(text: str) -> Optional[str]:
    """Find the first .modal.run URL in deployment output."""
    m = re.search(r'https://[\w\-]+\.modal\.run\S*', text)
    return m.group(0).rstrip("/.,)") if m else None


def _sanitize_name(name: str) -> str:
    """Convert user input to a valid Modal app/label name."""
    name = name.lower().strip()
    name = re.sub(r'[^a-z0-9\-]', '-', name)
    name = re.sub(r'-+', '-', name).strip('-')
    return name[:40] or "arbiter-llm"


# ---------------------------------------------------------------------------
# Background deployment task
# ---------------------------------------------------------------------------

async def _run_deployment(
    deploy_id: str,
    script: str,
    token_id: str,
    token_secret: str,
    app_state,
) -> None:
    """
    Run `modal deploy` in a subprocess, stream logs to Redis,
    extract the deployed URL, and auto-register as a Modal endpoint.
    """
    redis = app_state.redis
    deployments = await _load_deployments(redis)
    if deploy_id not in deployments:
        return

    deployments[deploy_id]["status"] = "building"
    await _save_deployments(redis, deployments)

    tmp_path = None
    try:
        # Write script to temp file
        with tempfile.NamedTemporaryFile(
            suffix=".py", mode="w", delete=False, prefix="arbiter_modal_"
        ) as f:
            f.write(script)
            tmp_path = f.name

        await _append_log(redis, deploy_id, f"[arbiter] Script written to {tmp_path}")
        await _append_log(redis, deploy_id, "[arbiter] Running: modal deploy ...")

        deployments[deploy_id]["status"] = "deploying"
        await _save_deployments(redis, deployments)

        env = {
            **os.environ,
            "MODAL_TOKEN_ID":     token_id,
            "MODAL_TOKEN_SECRET": token_secret,
            # Suppress interactive prompts
            "MODAL_LOGLEVEL":     "WARNING",
        }

        proc = await asyncio.create_subprocess_exec(
            "modal", "deploy", tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

        url_found = None
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            await _append_log(redis, deploy_id, line)
            # Pick up URL as soon as it appears
            if not url_found and ".modal.run" in line:
                url_found = _extract_url(line)
                if url_found:
                    deployments = await _load_deployments(redis)
                    deployments[deploy_id]["url"] = url_found
                    await _save_deployments(redis, deployments)
                    await _append_log(redis, deploy_id,
                                      f"[arbiter] URL detected: {url_found}")

        await proc.wait()
        exit_code = proc.returncode

        deployments = await _load_deployments(redis)

        if exit_code == 0:
            await _append_log(redis, deploy_id,
                              f"[arbiter] Deployment successful (exit 0)")
            deployments[deploy_id]["status"] = "active"
            deployments[deploy_id]["deployed_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )

            # Auto-register as Modal endpoint if we got a URL
            if url_found:
                token_str = f"{token_id}:{token_secret}"
                composite  = f"{url_found}|{token_str}"
                try:
                    from app.api.keys_api import _redis_keys, _save_redis_keys, _reload_provider
                    existing = await _redis_keys(redis, "modal")
                    if composite not in existing:
                        existing.append(composite)
                        await _save_redis_keys(redis, "modal", existing)

                    # Also add to modal endpoints registry
                    from app.api.modal_manager import _load_endpoints, _save_endpoints
                    endpoints = await _load_endpoints(redis)
                    ep_name   = deployments[deploy_id].get("name", deploy_id)
                    if not any(e["name"] == ep_name for e in endpoints):
                        import time as _t
                        endpoints.append({
                            "name":        ep_name,
                            "url":         url_found,
                            "token":       token_str,
                            "models":      [deployments[deploy_id].get("model_id", "")],
                            "description": f"Auto-deployed via Arbiter",
                            "registered_at": int(_t.time()),
                        })
                        await _save_endpoints(redis, endpoints)

                    # Reload provider pool
                    class _FakeRequest:
                        class app:
                            class state:
                                pass
                    _FakeRequest.app.state.redis    = redis
                    _FakeRequest.app.state.providers = app_state.providers
                    _FakeRequest.app.state.key_pools = app_state.key_pools

                    await _reload_provider("modal", _FakeRequest())
                    await _append_log(redis, deploy_id,
                                      f"[arbiter] Endpoint auto-registered: {ep_name}")
                except Exception as exc:
                    logger.warning("Auto-register failed: %s", exc)
                    await _append_log(redis, deploy_id,
                                      f"[arbiter] Warning: auto-register failed: {exc}")
        else:
            await _append_log(redis, deploy_id,
                              f"[arbiter] Deployment failed (exit {exit_code})")
            deployments[deploy_id]["status"] = "failed"
            deployments[deploy_id]["error"]  = f"modal deploy exited {exit_code}"

        await _save_deployments(redis, deployments)

    except Exception as exc:
        logger.error("Deployment %s crashed: %s", deploy_id, exc)
        await _append_log(redis, deploy_id, f"[arbiter] Internal error: {exc}")
        deployments = await _load_deployments(redis)
        if deploy_id in deployments:
            deployments[deploy_id]["status"] = "failed"
            deployments[deploy_id]["error"]  = str(exc)
            await _save_deployments(redis, deployments)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Account token endpoints
# ---------------------------------------------------------------------------

@router.get("/account", summary="Check Modal account token")
async def get_account(request: Request) -> JSONResponse:
    redis = request.app.state.redis
    raw   = await redis.get(_KEY_TOKEN)
    if not raw:
        return JSONResponse({"configured": False})
    try:
        tid, _ = _parse_token(raw)
        masked = tid[:8] + "..." if len(tid) > 8 else tid
        return JSONResponse({"configured": True, "token_id_masked": masked})
    except Exception:
        return JSONResponse({"configured": False, "error": "Invalid stored token"})


@router.post("/account", summary="Save Modal account token")
async def save_account(body: SaveTokenBody, request: Request) -> JSONResponse:
    try:
        tid, tsec = _parse_token(body.token)
    except ValueError as e:
        raise HTTPException(400, str(e))
    redis = request.app.state.redis
    await redis.set(_KEY_TOKEN, body.token.strip())
    masked = tid[:8] + "..." if len(tid) > 8 else tid
    return JSONResponse({"success": True, "token_id_masked": masked})


# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------

@router.get("/models", summary="Curated model catalog with GPU recommendations")
async def list_models() -> JSONResponse:
    return JSONResponse(content={"models": MODELS})


# ---------------------------------------------------------------------------
# Deployment endpoints
# ---------------------------------------------------------------------------

@router.get("", summary="List all deployments")
async def list_deployments(request: Request) -> JSONResponse:
    deployments = await _load_deployments(request.app.state.redis)
    return JSONResponse(content={"deployments": list(deployments.values())})


@router.get("/check", summary="Check Modal CLI availability")
async def check_modal_cli(request: Request) -> JSONResponse:
    """
    Verify that the Modal CLI is installed and account token is configured.

    Returns:
    - `cli_found`: True if `modal` binary is in PATH
    - `cli_path`: full path to the modal binary
    - `token_configured`: True if an account token is stored
    """
    import shutil
    cli_path = shutil.which("modal")
    redis = request.app.state.redis
    token_raw = await redis.get(_KEY_TOKEN)
    token_ok = False
    token_masked = None
    if token_raw:
        try:
            tid, _ = _parse_token(token_raw)
            token_ok = True
            token_masked = tid[:8] + "..." if len(tid) > 8 else tid
        except Exception:
            pass
    return JSONResponse(content={
        "cli_found":        bool(cli_path),
        "cli_path":         cli_path or None,
        "token_configured": token_ok,
        "token_id_masked":  token_masked,
        "ready":            bool(cli_path) and token_ok,
        "issues": (
            (["Modal CLI not found — run: pip install modal && modal setup"] if not cli_path else []) +
            (["No account token — set it in Settings → Modal GPU tab"] if not token_ok else [])
        ),
    })


@router.post("", summary="Deploy a vLLM model on Modal", status_code=202)
async def start_deployment(body: DeployBody, request: Request) -> JSONResponse:
    """
    Generate and deploy an optimised vLLM inference script on Modal.
    Returns immediately with a deploy_id — poll GET /modal/deploy/{id} for status.

    Requires:
    - Modal CLI installed (`pip install modal && modal setup`)
    - Account token saved via POST /modal/deploy/account
    """
    import shutil
    # Pre-flight: check modal CLI is available
    if not shutil.which("modal"):
        raise HTTPException(
            400,
            "Modal CLI not found in PATH. Install it: pip install modal && modal setup\n"
            "Then set your token in Settings → Modal GPU tab."
        )

    redis = request.app.state.redis

    # Get token
    token_raw = await redis.get(_KEY_TOKEN)
    if not token_raw:
        raise HTTPException(400, "No Modal account token configured. Set it in the Modal GPU tab first.")
    try:
        token_id, token_secret = _parse_token(token_raw)
    except ValueError as e:
        raise HTTPException(400, f"Invalid stored token: {e}")

    # Resolve model metadata
    model_meta = next((m for m in MODELS if m["id"] == body.model_id), None)

    name       = _sanitize_name(body.name or body.model_id.split("/")[-1])
    app_name   = f"arbiter-{name}"
    label      = name
    gpu        = body.gpu or (model_meta["gpu"] if model_meta else "A10G")
    max_len    = body.max_model_len or (model_meta["max_model_len"] if model_meta else 8192)
    idle_to    = max(60, body.idle_timeout)
    concurrent = max(1, min(body.concurrent_inputs, 32))

    # HuggingFace token secret block
    if body.hf_token:
        secret_block = (
            f'_hf_secret = modal.Secret.from_dict({{"HF_TOKEN": "{body.hf_token}"}})'
        )
        secrets_arg  = "secrets=[_hf_secret],"
    else:
        secret_block = ""
        secrets_arg  = ""

    script = _VLLM_TEMPLATE.format(
        model_id          = body.model_id,
        app_name          = app_name,
        label             = label,
        gpu               = _GPU_MAP.get(gpu, gpu),
        max_model_len     = max_len,
        idle_timeout      = idle_to,
        concurrent_inputs = concurrent,
        secret_block      = secret_block,
        secrets_arg       = secrets_arg,
        quantization_line = "",
    )

    deploy_id = f"dep-{uuid.uuid4().hex[:8]}"
    deployments = await _load_deployments(redis)
    deployments[deploy_id] = {
        "deploy_id":   deploy_id,
        "name":        name,
        "app_name":    app_name,
        "model_id":    body.model_id,
        "gpu":         gpu,
        "max_model_len": max_len,
        "idle_timeout": idle_to,
        "concurrent_inputs": concurrent,
        "status":      "pending",
        "url":         "",
        "error":       "",
        "created_at":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "deployed_at": "",
    }
    await _save_deployments(redis, deployments)
    await _append_log(redis, deploy_id, f"[arbiter] Deployment queued: {app_name}")
    await _append_log(redis, deploy_id, f"[arbiter] Model: {body.model_id} | GPU: {gpu}")
    await _append_log(redis, deploy_id, f"[arbiter] Idle timeout: {idle_to}s | Concurrent: {concurrent}")

    # Launch background deployment task
    asyncio.create_task(
        _run_deployment(deploy_id, script, token_id, token_secret,
                        request.app.state)
    )

    cost_hr = (model_meta or {}).get("cost_per_hr", 0)
    return JSONResponse(
        status_code=202,
        content={
            "deploy_id":  deploy_id,
            "app_name":   app_name,
            "model_id":   body.model_id,
            "gpu":        gpu,
            "status":     "pending",
            "cost_info":  {
                "gpu_cost_per_hr":    cost_hr,
                "idle_timeout_secs":  idle_to,
                "concurrent_inputs":  concurrent,
                "note": "You only pay while requests are running. Container shuts down after idle timeout.",
            },
            "message": "Deployment started. Poll GET /modal/deploy/{} for status.".format(deploy_id),
        },
    )


@router.get("/{deploy_id}", summary="Get deployment status and logs")
async def get_deployment(deploy_id: str, request: Request) -> JSONResponse:
    redis = request.app.state.redis
    deployments = await _load_deployments(redis)
    dep = deployments.get(deploy_id)
    if not dep:
        raise HTTPException(404, f"Deployment '{deploy_id}' not found")
    logs = await _get_logs(redis, deploy_id)
    return JSONResponse(content={**dep, "logs": logs})


@router.delete("/{deploy_id}", summary="Stop a Modal deployment")
async def delete_deployment(deploy_id: str, request: Request) -> JSONResponse:
    """
    Stops the Modal app (via `modal app stop`) and removes it from the registry.
    Also removes the endpoint from the Modal endpoint pool.
    """
    redis = request.app.state.redis
    deployments = await _load_deployments(redis)
    dep = deployments.get(deploy_id)
    if not dep:
        raise HTTPException(404, f"Deployment '{deploy_id}' not found")

    app_name = dep.get("app_name", "")
    url      = dep.get("url", "")

    # Try to stop the Modal app via CLI
    token_raw = await redis.get(_KEY_TOKEN)
    stop_msg  = "No token — skipped app stop"
    if token_raw and app_name:
        try:
            tid, tsec = _parse_token(token_raw)
            env = {
                **os.environ,
                "MODAL_TOKEN_ID":     tid,
                "MODAL_TOKEN_SECRET": tsec,
            }
            proc = await asyncio.create_subprocess_exec(
                "modal", "app", "stop", app_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            stop_msg = stdout.decode("utf-8", errors="replace").strip() or "stopped"
        except Exception as exc:
            stop_msg = f"stop command error: {exc}"

    # Remove from deployments
    del deployments[deploy_id]
    await _save_deployments(redis, deployments)

    # Remove from endpoint pool
    if url:
        try:
            from app.api.keys_api import _redis_keys, _save_redis_keys
            existing = await _redis_keys(redis, "modal")
            existing = [k for k in existing if not k.startswith(url)]
            await _save_redis_keys(redis, "modal", existing)
        except Exception:
            pass

        try:
            from app.api.modal_manager import _load_endpoints, _save_endpoints
            endpoints = await _load_endpoints(redis)
            name = dep.get("name", "")
            endpoints = [e for e in endpoints if e.get("name") != name]
            await _save_endpoints(redis, endpoints)
        except Exception:
            pass

    return JSONResponse(content={
        "success":  True,
        "deleted":  deploy_id,
        "app_name": app_name,
        "stop_msg": stop_msg,
    })
