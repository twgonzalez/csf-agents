#!/usr/bin/env python3
"""
image_generator.py — Nano Banana Pro Image Generator
California Stewardship Fund

Takes an image_brief dict (from social_writer.py) and calls
gemini-3-pro-image-preview twice (1:1 and 16:9) to generate policy advocacy
graphics. Saves PNGs to outputs/social/images/YYYY-WNN/.

Usage (standalone, for testing):
    python agents/social/image_generator.py

Requires:
    GEMINI_API_KEY in environment or .env
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env", override=True)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CSF brand defaults — used when no client brand is provided (e.g. standalone test)
# ---------------------------------------------------------------------------
_CSF_BRAND_DEFAULTS: dict = {
    "client_name": "California Stewardship Fund",
    "colors": {
        "background": {"name": "deep navy blue"},
        "accent":     {"name": "warm gold"},
    },
    "image": {
        "accent_stripe": "A warm gold horizontal accent stripe across the lower third",
        "bill_context":  "California legislative bill",
    },
}

# ---------------------------------------------------------------------------
# Sizes: (aspect_ratio, slug, description)
# ---------------------------------------------------------------------------
_SIZES = [
    ("1:1",  "square",    "2048x2048 square (Instagram/Facebook)"),
    ("16:9", "landscape", "2752x1536 landscape (X/Facebook)"),
]


def build_prompt(brief: dict, aspect_ratio: str, brand: dict | None = None) -> str:
    """Build the Nano Banana Pro prompt from an image_brief dict.

    Handles two post types:
    - Bill-specific (bill_number present): large accent-color bill number is the dominant element.
    - Thematic / mission-frame (no bill_number): headline is the dominant element;
      typographic_element description is used for layout guidance.

    aspect_ratio is "1:1" or "16:9" — used only for the size descriptor in the
    prompt text; the actual API aspect ratio is passed separately.

    brand is a client config dict (from clients/<id>/client.yml). Falls back to
    _CSF_BRAND_DEFAULTS when None so the standalone test requires no changes.
    """
    import re

    b = brand if brand is not None else _CSF_BRAND_DEFAULTS

    # Resolve brand fragments
    bg_name       = b["colors"]["background"]["name"]
    accent_name   = b["colors"]["accent"]["name"]
    accent_stripe = b["image"]["accent_stripe"]
    bill_context  = b["image"]["bill_context"]
    org_name      = b["client_name"]

    headline            = brief.get("headline", "")
    subtext             = brief.get("subtext", "")
    optional_graphic    = brief.get("optional_graphic", "")
    typographic_element = brief.get("typographic_element", "")

    # Resolve bill_number — guard against JSON null coming through as Python None
    bill_number = brief.get("bill_number") or ""

    # If no explicit bill_number, try to extract one from typographic_element
    # e.g. "Bill number 'AB1751' as large oversized display type" → "AB1751"
    if not bill_number and typographic_element:
        m = re.search(r"['\"]([A-Z]{1,3}\d+)['\"]", typographic_element)
        if m:
            bill_number = m.group(1)

    size_label = "square (1:1)" if aspect_ratio == "1:1" else "landscape (16:9)"

    optional_graphic_line = (
        f"- {optional_graphic}"
        if optional_graphic and optional_graphic.lower() not in ("none", "")
        else "- No additional graphic element"
    )

    if bill_number:
        # Bill-specific post: bill number is the dominant typographic element
        intro      = f"Create a {size_label} policy advocacy graphic for the {bill_context} {bill_number}."
        typo_line  = f'- Large oversized bold display text "{bill_number}" in {accent_name}, upper-left — dominant element'
        hierarchy  = f"Text hierarchy: {bill_number} (largest, {accent_name}) → headline (medium-large, white bold) → subtext (smaller, white)"
    else:
        # Thematic / mission-frame post: headline drives the layout
        intro     = f"Create a {size_label} policy advocacy graphic for {org_name}."
        # Use typographic_element description as layout guidance if present
        te_note = f"\n- Typographic layout: {typographic_element}" if typographic_element else ""
        typo_line = f"- Bold headline is the dominant visual element, large white display type, upper portion{te_note}"
        hierarchy = "Text hierarchy: headline (largest, white bold) → subtext (smaller, white)"

    return f"""{intro}

Layout and design:
- {bg_name.capitalize()} background, flat solid color, no gradients
{typo_line}
- {accent_stripe}
{optional_graphic_line}

Text to render exactly as written:
- Headline (large, white, bold): {headline}
- Subtext (smaller, white): {subtext}

{hierarchy}
Style: Clean, minimal, professional policy-advocacy. No photos. No people. No logos."""


def generate_images(
    brief: dict,
    output_dir: Path,
    post_slug: str,
    brand: dict | None = None,
) -> dict[str, Path]:
    """Generate square and landscape PNGs for a single post.

    Args:
        brief:      image_brief dict from Claude's JSON output.
        output_dir: Directory to save PNGs (created if needed).
        post_slug:  Short slug for the post, e.g. "post_1".
        brand:      Client config dict (from clients/<id>/client.yml). Falls back
                    to _CSF_BRAND_DEFAULTS when None.

    Returns:
        {"square": Path, "landscape": Path}
        On error, logs and returns an empty dict so the caller can continue.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.error("GEMINI_API_KEY not set — skipping image generation")
        return {}

    try:
        from google import genai
        from google.genai import types
        from PIL import Image
        import io
    except ImportError as e:
        log.error(f"Missing dependency: {e}. Run: pip install google-genai Pillow")
        return {}

    client = genai.Client(api_key=api_key)
    output_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, Path] = {}

    for aspect_ratio, slug, description in _SIZES:
        prompt = build_prompt(brief, aspect_ratio, brand=brand)
        out_path = output_dir / f"{post_slug}_{slug}.png"

        log.info(f"   → Generating {slug} ({description})...")
        try:
            response = client.models.generate_content(
                model="gemini-3-pro-image-preview",
                contents=[prompt],
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    image_config=types.ImageConfig(
                        aspect_ratio=aspect_ratio,
                        image_size="2K",
                    ),
                ),
            )

            # Extract the first image part from the response
            image_data = None
            for part in response.candidates[0].content.parts:
                if part.inline_data and part.inline_data.data:
                    image_data = part.inline_data.data
                    break

            if image_data is None:
                log.warning(f"   ✗ No image data in response for {slug}")
                continue

            # Save via Pillow (handles both bytes and base64 strings)
            if isinstance(image_data, str):
                import base64
                image_data = base64.b64decode(image_data)

            img = Image.open(io.BytesIO(image_data))
            img.save(out_path, format="PNG")
            try:
                display_path = out_path.relative_to(_PROJECT_ROOT)
            except ValueError:
                display_path = out_path
            log.info(f"   ✓ Saved {display_path}")
            results[slug] = out_path

        except Exception as e:
            log.error(f"   ✗ Failed to generate {slug}: {e}")

    return results


# ---------------------------------------------------------------------------
# Standalone test — uses Post 1 (AB1751) brief from W08
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    from datetime import date

    logging.basicConfig(level=logging.INFO, format="  %(message)s")

    test_brief = {
        "headline":           "AB1751 Bans Cities from Charging Impact Fees",
        "subtext":            "AB1751 eliminates impact fees and all local discretionary review",
        "background_color":   "#1a3a5c",
        "text_color":         "#ffffff",
        "accent_color":       "#c9a227",
        "typographic_element": "Bill number 'AB1751' as large oversized display type, upper-left, in gold accent color",
        "optional_graphic":   "California state outline, minimal, faint white, bottom-right corner",
        "bill_number":        "AB1751",
        "sizes": [
            "1080x1080 (Instagram/Facebook square)",
            "1600x900 (X/Facebook landscape)",
        ],
    }

    iso_week  = date.today().strftime("%G-W%V")   # ISO 8601 — matches workflow %V
    out_dir   = _PROJECT_ROOT / "outputs" / "clients" / "csf" / "social" / "images" / iso_week
    post_slug = "post_1"

    print(f"\n  Nano Banana Pro — standalone test")
    print(f"  Brief: {test_brief['headline']}")
    print(f"  Output: {out_dir}\n")

    paths = generate_images(test_brief, out_dir, post_slug)

    if paths:
        print(f"\n  Generated images:")
        for k, p in paths.items():
            print(f"    {k}: {p}")
    else:
        print("\n  No images generated (check GEMINI_API_KEY and logs above)")
