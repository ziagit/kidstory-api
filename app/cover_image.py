"""Deterministic AI cover images via Pollinations.ai.

A direct port of the Nuxt app's `server/utils/cover-image.ts` so a story created
through this API gets exactly the same cover URL it would get from /studio.
"""

from urllib.parse import quote, urlencode


def _hash_seed(value: str) -> int:
    digest = 0
    for char in value:
        digest = (digest * 31 + ord(char)) % 4_294_967_296
    return digest % 1_000_000


def build_cover_image_url(title: str, category: str, slug: str) -> str:
    prompt = ", ".join(
        [
            "flat 2D cartoon illustration",
            "children's picture book art style",
            "vector illustration, cel shaded",
            f"{category} theme",
            title,
            "cute soft rounded character design with big expressive eyes",
            "vibrant limited color palette",
            "bold flat colors, minimal shading",
            "simple background",
            "friendly and wholesome",
            "full bleed illustration",
            "no text",
            "no words",
            "no watermark",
            "no border",
            "no frame",
            "not 3D, not CGI, not a 3D render, not photorealistic",
            "no scary or dark elements",
        ]
    )
    query = urlencode(
        {
            "width": "800",
            "height": "600",
            "seed": str(_hash_seed(slug)),
            "model": "sana",
            "nologo": "true",
            "safe": "true",
        }
    )
    return f"https://image.pollinations.ai/prompt/{quote(prompt, safe='')}?{query}"
