"""
Image generation via Pollinations.ai (completely free, no API key needed).

Pollinations image endpoint:
  https://image.pollinations.ai/prompt/{encoded_prompt}?model=flux&width=1024&height=1024&nologo=true

Routes
------
POST /v1/images/generations   OpenAI-compatible image generation
GET  /v1/images/models        List available image models
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Images"])

_BASE = "https://image.pollinations.ai/prompt"

# Pollinations image models (March 2026)
_IMAGE_MODELS = {
    "flux":          "FLUX.1 — high quality, versatile (default)",
    "flux-realism":  "FLUX Realism — photorealistic images",
    "flux-anime":    "FLUX Anime — anime/manga style",
    "flux-3d":       "FLUX 3D — 3D rendered style",
    "flux-cablyai":  "FLUX CablyAI — custom fine-tune",
    "turbo":         "SDXL Turbo — fast generation",
}

_VALID_SIZES = {
    "256x256", "512x512", "768x768",
    "1024x1024", "1024x768", "768x1024",
    "1280x720", "720x1280", "1920x1080",
}


class ImageRequest(BaseModel):
    prompt: str = Field(..., max_length=4000)
    model: Optional[str] = "flux"
    n: Optional[int] = Field(1, ge=1, le=4)
    size: Optional[str] = "1024x1024"
    seed: Optional[int] = None
    enhance: Optional[bool] = False
    nologo: Optional[bool] = True
    negative_prompt: Optional[str] = Field(None, max_length=1000)
    response_format: Optional[str] = "url"


def _build_url(prompt: str, model: str, width: int, height: int,
               seed: Optional[int], enhance: bool, nologo: bool,
               negative_prompt: Optional[str]) -> str:
    params = [
        f"width={width}",
        f"height={height}",
        f"model={model}",
        f"nologo={str(nologo).lower()}",
        f"enhance={str(enhance).lower()}",
    ]
    if seed is not None and seed >= 0:
        params.append(f"seed={seed}")
    if negative_prompt:
        params.append(f"negative={quote(negative_prompt)}")
    return f"{_BASE}/{quote(prompt)}?{'&'.join(params)}"


def _parse_size(size: str) -> tuple:
    try:
        w, h = size.lower().split("x")
        return int(w), int(h)
    except Exception:
        return 1024, 1024


@router.post("/v1/images/generations", summary="Generate images — Pollinations.ai (free)")
async def generate_images(body: ImageRequest) -> JSONResponse:
    """
    Generate images using Pollinations.ai.
    - Completely free, no API key required
    - Returns URLs that Pollinations renders on first access
    - Models: flux (default), flux-realism, flux-anime, flux-3d, turbo
    - Size examples: 1024x1024, 1280x720, 768x1024
    """
    model  = body.model if body.model in _IMAGE_MODELS else "flux"
    width, height = _parse_size(body.size or "1024x1024")
    width  = max(64, min(2048, width))
    height = max(64, min(2048, height))
    count  = max(1, min(4, body.n or 1))

    data: List[dict] = []
    for i in range(count):
        seed = (body.seed + i) if body.seed is not None else -1
        url  = _build_url(
            prompt          = body.prompt,
            model           = model,
            width           = width,
            height          = height,
            seed            = seed if seed >= 0 else None,
            enhance         = body.enhance or False,
            nologo          = body.nologo if body.nologo is not None else True,
            negative_prompt = body.negative_prompt,
        )
        data.append({"url": url, "revised_prompt": body.prompt})

    return JSONResponse(content={
        "created": int(time.time()),
        "provider": "pollinations",
        "model": model,
        "data": data,
    })


@router.get("/v1/images/models", summary="List image generation models")
async def list_image_models() -> JSONResponse:
    return JSONResponse(content={
        "provider":   "pollinations",
        "free":       True,
        "signup_url": "https://pollinations.ai/",
        "models": [
            {"id": k, "description": v} for k, v in _IMAGE_MODELS.items()
        ],
        "default": "flux",
        "max_images_per_request": 4,
        "supported_sizes": sorted(_VALID_SIZES),
    })
