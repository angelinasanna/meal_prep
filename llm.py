"""Gemini client and the single text-generation helper."""

from google import genai as google_genai
from google.genai import types as google_genai_types
import asyncio
import os


_gemini_client = None


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        key = os.getenv("GEMINI_API_KEY", "")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set in .env")
        _gemini_client = google_genai.Client(api_key=key)
    return _gemini_client


# ── General helpers ──────────────────────────────────────────────────────────
async def _gemini(prompt: str, system: str = None) -> str:
    config = google_genai_types.GenerateContentConfig(
        system_instruction=system,
        max_output_tokens=8192,
    )
    response = await asyncio.to_thread(
        _get_gemini_client().models.generate_content,
        model="gemini-3.6-flash",
        contents=prompt,
        config=config,
    )
    return response.text or ""
