#!/usr/bin/env python3
"""
visual_director.py — Visual Creative Director Agent
California Stewardship Fund / Multi-client

Sits between social_writer.py and image_generator.py. Takes the Social Writer's
image briefs and uses Claude to reason about composition, typographic hierarchy,
emotional register, and visual differentiation across post types — then writes
enriched, self-contained Gemini image prompts that go well beyond color templating.

Pipeline position:
    social_writer.py  ──→  data/social/<client>/social_posts.json
                     ──→  visual_director.py
                     ──→  data/social/<client>/visual_director_briefs.json
                     ──→  outputs/clients/<slug>/social/images/<week>/

Usage (standalone — requires social_posts.json written by social_writer.py):
    python agents/social/visual_director.py --client csf
    python agents/social/visual_director.py --client cma --compare
    python agents/social/visual_director.py --client csf --images
    python agents/social/visual_director.py --client csf --images --compare

    # Point to a specific posts file:
    python agents/social/visual_director.py --client csf --posts path/to/posts.json

    # List available clients / voices:
    python agents/social/visual_director.py --list-clients
    python agents/social/visual_director.py --client csf --list-voices

Called automatically from social_writer.py when --visual-director flag is used.

Requires:
    ANTHROPIC_API_KEY in environment or .env
    GEMINI_API_KEY in environment or .env (only for --images)
    data/social/<client>/social_posts.json (written by social_writer.py --visual-director)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env", override=True)

import anthropic

from agents.shared.client_utils import (
    CLIENTS_DIR, DEFAULT_CLIENT, DEFAULT_VOICE,
    _load_client, _list_clients, _load_voice, _list_voices,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="  %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DATA_SOCIAL_DIR = _PROJECT_ROOT / "data" / "social"

# ---------------------------------------------------------------------------
# Post type compositional metadata
#
# Each post type gets a different emotional register and compositional intent.
# These descriptions are injected into the Claude prompt so it can reason
# about what visual choices serve each post's strategic purpose.
# ---------------------------------------------------------------------------

_POST_TYPE_META: dict[str, dict[str, str]] = {
    "bill_spotlight": {
        "label": "Bill Spotlight",
        "emotional_register": "alarm and authority",
        "compositional_intent": (
            "The viewer has just discovered something important happening right now. "
            "The bill number is the hook — it dominates the frame, large and confrontational. "
            "This is the most data-dense post: specific, authoritative, precise. "
            "The typography carries the message; layout reinforces urgency without hysteria."
        ),
        "differentiation_note": (
            "This post should feel like a breaking news card — the most specific and factual "
            "of the three. Its composition is tight, left-anchored, data-driven. "
            "If the other posts use open negative space, this one fills it with purpose."
        ),
    },
    "action_alert": {
        "label": "Action Alert",
        "emotional_register": "urgency and agency",
        "compositional_intent": (
            "The viewer must feel compelled to act — now, not later. This is the most "
            "time-sensitive post: a hearing date, a deadline, a direct ask. "
            "The composition should feel kinetic and forward-moving, like a rally poster. "
            "Urgency is communicated through tight spacing, forward-leaning elements, "
            "and a clear visual imperative."
        ),
        "differentiation_note": (
            "This post is the most urgent of the three. Its visual should feel different "
            "from the Bill Spotlight — less informational, more imperative. "
            "Where the Bill Spotlight informs, this one mobilizes. "
            "Consider asymmetric tension, diagonal energy, or bold accent deployment "
            "that creates a sense of motion or imminence."
        ),
    },
    "mission_frame": {
        "label": "Mission Frame",
        "emotional_register": "resolve and purpose",
        "compositional_intent": (
            "The viewer should feel connected to something larger than a single bill. "
            "This post synthesizes — it zooms out to the 'who decides' question. "
            "The composition should feel expansive and philosophical, less data-driven. "
            "Negative space is an asset here: breathing room communicates perspective and resolve. "
            "This is the manifesto, not the memo."
        ),
        "differentiation_note": (
            "This post closes the week on resolve, not alarm. Its visual should feel "
            "noticeably more spacious and thematic than the Bill Spotlight or Action Alert. "
            "Where those posts are tight and specific, this one breathes. "
            "The headline should feel like a declarative truth, not a data point."
        ),
    },
}

# Gemini API sizes
_SIZES = [
    ("1:1",  "square",    "square (1:1) — 2048×2048 Instagram/Facebook"),
    ("16:9", "landscape", "landscape (16:9) — 2752×1536 X/Facebook"),
]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _build_system_prompt(client: dict, voice_text: str = "") -> str:
    """Build the Visual Director system prompt for Claude.

    Frames Claude as a visual creative director — not an image prompter —
    who reasons about composition, audience, and emotional register before
    writing enriched Gemini prompts.
    """
    img_cfg = client.get("image", {})

    org_name        = client["client_name"]
    org_desc        = client["identity"]["org_description"].strip()
    audience        = client["identity"]["audience"].strip()
    bg_name         = client["colors"]["background"]["name"]
    bg_hex          = client["colors"]["background"]["hex"]
    text_name       = client["colors"]["text"]["name"]
    text_hex        = client["colors"]["text"]["hex"]
    acc_name        = client["colors"]["accent"]["name"]
    acc_hex         = client["colors"]["accent"]["hex"]
    style_notes     = img_cfg.get("style_notes", "").strip()
    acc_stripe      = img_cfg.get("accent_stripe", "").strip()
    layout_approach = img_cfg.get("layout_approach", "").strip()
    typography_style = img_cfg.get("typography_style", "").strip()

    voice_section = ""
    if voice_text:
        voice_section = f"\n\n## Campaign Voice & Tone\n\n{voice_text}"

    # Optional extended brand section — only rendered when client.yml provides these fields
    extended_brand = ""
    if layout_approach:
        extended_brand += f"\n\n### Layout Approach (this client)\n\n{layout_approach}"
    if typography_style:
        extended_brand += f"\n\n### Typography Style (this client)\n\n{typography_style}"

    return f"""\
You are the visual creative director for {org_name} — {org_desc}

Your audience is: {audience}

## Your Role

You are NOT an image prompt generator. You are a visual creative director who thinks \
strategically about how graphics communicate for this specific organization — then writes \
precise, complete Gemini image generation prompts that execute that strategy.

Before writing any prompt, reason through:
1. What does this specific audience respond to visually and emotionally?
2. What emotional register does this post type need to achieve?
3. What compositional structure serves that emotional arc?
4. How should this image feel *different* from the other two posts in this weekly set?
5. What typographic scale relationships create the correct reading order at a glance?

## Brand Identity

- Background: {bg_name} ({bg_hex}) — flat solid color, no gradients, ever
- Text: {text_name} ({text_hex}) — must always be legible against the background
- Accent: {acc_name} ({acc_hex}) — the typographic anchor, rule, stripe, or highlight element
- Accent element default: {acc_stripe if acc_stripe else "a horizontal accent stripe in the accent color"}
- Style: {style_notes if style_notes else "Clean, minimal, professional. No photos. No people. No logos."}{extended_brand}

## Compositional Principles for Policy Advocacy Graphics

**Typography IS the design.** Policy advocacy graphics communicate through text. \
Treat typography as the primary visual element — not an afterthought added to a background.

**Hierarchy creates reading order.** The viewer's eye must move through information in \
exactly the order you intend: dominant anchor → secondary message → supporting detail. \
Never let layout fight the message.

**Emotional register is compositional.** Urgency uses tight, compressed layouts with \
forward-leaning or asymmetric tension. Resolve uses open negative space that communicates \
perspective. Alarm uses bold, confrontational placement. Match the layout to the feeling.

**Scale relationships signal importance.** The dominant typographic element should be \
dramatically larger than the secondary — not merely bigger. When the bill number dominates, \
it should fill a significant portion of the frame. When the headline dominates, \
it should command the space unambiguously.

**Differentiation across the set is non-negotiable.** Three posts with identical composition \
feel like a template, not a voice. The Bill Spotlight, Action Alert, and Mission Frame \
must each feel distinctly themselves — same brand, different personality, different emotional \
weight.

## Format Requirements for Prompts You Write

Each prompt must:
- Begin with a clear statement of what is being created (size, purpose, client context)
- Specify background color ({bg_name}, {bg_hex}) and text color ({text_name}, {text_hex})
- Specify exact compositional structure (where each element lives in the frame and why)
- Specify typographic hierarchy (what is largest, by roughly how much, in what color)
- Specify any accent stripe, geometric element, or abstract structural element
- Name any optional graphic element from the brief
- Include ALL text to render, exactly as provided — no paraphrasing, no invention
- End with style constraints (no photos, no people, no logos)

The square (1:1) and landscape (16:9) prompts share the same creative direction but \
adapt composition for format:
- Square (1:1): Elements stack vertically; compositions work within a compact, \
  centered or anchored frame without horizontal expanse
- Landscape (16:9): Can use horizontal zones — left-anchor typography with right-side \
  breathing room, strong horizontal visual bands, or split-zone layouts that \
  would feel awkward in square format{voice_section}

## Output Format

Return a JSON object with exactly this structure:

{{
  "reasoning": "2-3 sentences of your creative direction thinking: what emotional register \
this post type needs, what compositional choice serves it, and how it differentiates \
from the other two posts",
  "square_prompt": "Complete self-contained Gemini image generation prompt for 1:1 format",
  "landscape_prompt": "Complete self-contained Gemini image generation prompt for 16:9 format"
}}

Return ONLY valid JSON. No markdown fences. No commentary outside the JSON object. \
No placeholders — write complete, specific prompts that are ready to pass directly to Gemini."""


# ---------------------------------------------------------------------------
# Per-post Claude call
# ---------------------------------------------------------------------------

def _enrich_brief(
    post: dict,
    all_posts: list[dict],
    claude: anthropic.Anthropic,
    client_cfg: dict,
    voice_text: str = "",
) -> dict:
    """Call Claude to generate enriched compositional prompts for one post.

    Returns a dict with: reasoning, square_prompt, landscape_prompt
    """
    post_type = post.get("post_type", "bill_spotlight")
    meta      = _POST_TYPE_META.get(post_type, _POST_TYPE_META["bill_spotlight"])
    brief     = post.get("image_brief", {})
    num       = post.get("post_number", "?")

    # Build sibling context so Claude knows how to differentiate
    sibling_lines = []
    for p in all_posts:
        if p.get("post_number") == num:
            continue
        pb    = p.get("image_brief", {})
        pmeta = _POST_TYPE_META.get(p.get("post_type", ""), {})
        sibling_lines.append(
            f"  Post {p.get('post_number')} ({pmeta.get('label', p.get('post_type',''))}):\n"
            f"    Headline: \"{pb.get('headline','')}\"\n"
            f"    Emotional register: {pmeta.get('emotional_register', 'unknown')}"
        )
    sibling_str = "\n".join(sibling_lines) if sibling_lines else "  (none)"

    bill_number = post.get("bill_number") or brief.get("bill_number") or ""
    acc_name    = client_cfg["colors"]["accent"]["name"]
    org_name    = client_cfg["client_name"]

    user_prompt = f"""\
Generate enriched Gemini image prompts for this post.

## Post Details

- Post number: {num}
- Post type: {post_type} ({meta["label"]})
- Organization: {org_name}
- Bill number: {bill_number or "N/A — thematic/mission post, no specific bill"}
- Headline (render on graphic, exactly): {brief.get("headline", "")}
- Subtext (render on graphic, exactly): {brief.get("subtext", "")}
- Typographic element guidance from Social Writer: {brief.get("typographic_element", "none")}
- Optional graphic element: {brief.get("optional_graphic", "none")}

## This Post Type's Emotional Register

{meta["emotional_register"].capitalize()}

## Compositional Intent for This Post Type

{meta["compositional_intent"]}

## Differentiation Requirement

{meta["differentiation_note"]}

## The Other Two Posts in This Weekly Set (differentiate from these)

{sibling_str}

## Your Task

Think as a visual creative director:

1. What composition serves a {meta["label"]}?
2. How should the {acc_name} accent be deployed — as the dominant typographic element, \
a structural stripe, an asymmetric block, a background element?
3. What makes this post's visual personality distinct from the other two above?
4. What typographic scale relationships create the right reading order?
5. For the landscape format: how do you use the horizontal expanse differently \
than the square format?

Then write two complete, specific Gemini image prompts.

All text must appear exactly as written in the post details above. \
Do not invent, paraphrase, or abbreviate the headline or subtext.

Return ONLY valid JSON with the structure from your system prompt."""

    system_prompt = _build_system_prompt(client_cfg, voice_text)

    log.info(f"   → Calling Claude for Post {num} ({meta['label']})...")
    message = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]

    return json.loads(raw)


# ---------------------------------------------------------------------------
# Comparison output
# ---------------------------------------------------------------------------

def _print_comparison(post: dict, enriched: dict, client_cfg: dict) -> None:
    """Print side-by-side comparison of original brief vs enriched prompts."""
    from agents.social.image_generator import build_prompt

    post_type = post.get("post_type", "")
    meta      = _POST_TYPE_META.get(post_type, {})
    brief     = post.get("image_brief", {})
    num       = post.get("post_number", "?")

    orig_square    = build_prompt(brief, "1:1",  brand=client_cfg)
    orig_landscape = build_prompt(brief, "16:9", brand=client_cfg)

    divider = "─" * 72
    print(f"\n{divider}")
    print(f"  POST {num} — {meta.get('label', post_type).upper()}")
    print(divider)
    print(f"\n  Headline : {brief.get('headline', '')}")
    print(f"  Subtext  : {brief.get('subtext', '')}")
    print(f"  Bill     : {post.get('bill_number') or 'N/A'}")
    print(f"\n  ── DIRECTOR REASONING {'─' * 46}")
    reasoning = enriched.get("reasoning", "")
    for line in reasoning.splitlines():
        print(f"  {line}")

    def _block(title: str, text: str) -> None:
        print(f"\n  ── {title} {'─' * (65 - len(title))}")
        for line in text.splitlines():
            print(f"  {line}")

    _block("ORIGINAL (Nano Banana Pro template) — SQUARE", orig_square)
    _block("ENRICHED (Visual Director) — SQUARE",          enriched.get("square_prompt", ""))
    _block("ORIGINAL (Nano Banana Pro template) — LANDSCAPE", orig_landscape)
    _block("ENRICHED (Visual Director) — LANDSCAPE",       enriched.get("landscape_prompt", ""))


# ---------------------------------------------------------------------------
# Main enrichment pipeline
# ---------------------------------------------------------------------------

def enrich_briefs(
    posts: list[dict],
    client_cfg: dict,
    voice_text: str = "",
    compare: bool = False,
) -> list[dict]:
    """Enrich all posts' image briefs using Claude.

    Calls Claude once per post, in sequence. Each call receives the full
    sibling context so Claude can reason about differentiation.

    Returns a list of enriched brief dicts (one per post), each containing:
        post_number, post_type, bill_number, original_brief,
        reasoning, square_prompt, landscape_prompt
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set")
        sys.exit(1)

    claude          = anthropic.Anthropic(api_key=api_key)
    enriched_briefs = []

    log.info(f"→ Visual Director: enriching {len(posts)} image briefs...")

    for post in posts:
        post_type = post.get("post_type", "bill_spotlight")
        meta      = _POST_TYPE_META.get(post_type, {})
        num       = post.get("post_number", "?")
        brief     = post.get("image_brief", {})

        enriched = _enrich_brief(post, posts, claude, client_cfg, voice_text)

        enriched_brief = {
            "post_number":      num,
            "post_type":        post_type,
            "bill_number":      post.get("bill_number") or brief.get("bill_number") or "",
            "original_brief":   brief,
            "reasoning":        enriched.get("reasoning", ""),
            "square_prompt":    enriched.get("square_prompt", ""),
            "landscape_prompt": enriched.get("landscape_prompt", ""),
        }
        enriched_briefs.append(enriched_brief)

        if compare:
            _print_comparison(post, enriched, client_cfg)
        else:
            log.info(f"   ✓ Post {num} enriched ({meta.get('label', post_type)})")

    return enriched_briefs


# ---------------------------------------------------------------------------
# Image generation via enriched prompts (bypasses build_prompt() template)
# ---------------------------------------------------------------------------

def generate_images_from_enriched(
    enriched_briefs: list[dict],
    output_dir: Path,
) -> dict[int, dict[str, Path]]:
    """Generate images using enriched prompts from the Visual Director.

    Uses the square_prompt and landscape_prompt directly — bypasses build_prompt().

    Returns {post_number: {"square": Path, "landscape": Path}}
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
    except ImportError as exc:
        log.error(f"Missing dependency: {exc}. Run: pip install google-genai Pillow")
        return {}

    gemini = genai.Client(api_key=api_key)
    output_dir.mkdir(parents=True, exist_ok=True)
    results: dict[int, dict[str, Path]] = {}

    log.info("→ Generating images from enriched Visual Director prompts...")

    for eb in enriched_briefs:
        num      = eb["post_number"]
        results[num] = {}

        for aspect_ratio, slug, description in _SIZES:
            prompt_key = "square_prompt" if aspect_ratio == "1:1" else "landscape_prompt"
            prompt     = eb.get(prompt_key, "")

            if not prompt:
                log.warning(f"   ✗ No {slug} prompt for post {num} — skipping")
                continue

            out_path = output_dir / f"post_{num}_{slug}.png"
            log.info(f"   → Post {num}: generating {slug} ({description})...")

            try:
                response = gemini.models.generate_content(
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

                image_data = None
                for part in response.candidates[0].content.parts:
                    if part.inline_data and part.inline_data.data:
                        image_data = part.inline_data.data
                        break

                if image_data is None:
                    log.warning(f"   ✗ No image data in response for post {num} {slug}")
                    continue

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
                results[num][slug] = out_path

            except Exception as exc:
                log.error(f"   ✗ Failed to generate post {num} {slug}: {exc}")

    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Visual Director — uses Claude to generate compositionally-aware Gemini image\n"
            "prompts from the Social Writer's image briefs. Sits between social_writer.py\n"
            "and image_generator.py. Run social_writer.py --visual-director first to\n"
            "produce the social_posts.json input file."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--client", default=DEFAULT_CLIENT,
        help="Client slug (default: csf). Run --list-clients to see available clients.",
    )
    p.add_argument(
        "--voice", default=None,
        help="Voice name (default: client's default_voice). Run --list-voices to see options.",
    )
    p.add_argument(
        "--posts", type=Path, default=None,
        help="Path to social_posts.json (default: data/social/<client>/social_posts.json)",
    )
    p.add_argument(
        "--output", type=Path, default=None,
        help="Output path for visual_director_briefs.json (default: data/social/<client>/visual_director_briefs.json)",
    )
    p.add_argument(
        "--images", action="store_true", default=False,
        help="Generate PNG images using enriched prompts via Gemini. Requires GEMINI_API_KEY.",
    )
    p.add_argument(
        "--compare", action="store_true", default=False,
        help="Print side-by-side comparison of original template prompts vs enriched prompts.",
    )
    p.add_argument(
        "--list-clients", action="store_true", default=False,
        help="Print all available client slugs and exit.",
    )
    p.add_argument(
        "--list-voices", action="store_true", default=False,
        help="Print all available voices for the selected client and exit.",
    )
    args = p.parse_args()

    # ── --list-clients ────────────────────────────────────────────────────────
    if args.list_clients:
        clients = _list_clients()
        if clients:
            print("\n  Available clients (clients/):\n")
            for c in clients:
                marker = " ← default" if c == DEFAULT_CLIENT else ""
                print(f"    {c}{marker}")
            print(f"\n  Usage: --client <slug>   e.g. --client cma\n")
        else:
            print(f"\n  No client directories found in {CLIENTS_DIR}\n")
        sys.exit(0)

    # ── Load client config ────────────────────────────────────────────────────
    client_cfg  = _load_client(args.client)
    client_id   = client_cfg["client_id"]
    client_name = client_cfg["client_name"]
    voices_dir  = CLIENTS_DIR / client_id / "voices"

    # ── --list-voices ─────────────────────────────────────────────────────────
    if args.list_voices:
        voices = _list_voices(voices_dir)
        if voices:
            default_v = client_cfg.get("default_voice", DEFAULT_VOICE)
            print(f"\n  Available voices for '{client_id}' (clients/{client_id}/voices/):\n")
            for v in voices:
                marker = " ← default" if v == default_v else ""
                print(f"    {v}{marker}")
            print(f"\n  Usage: --voice <name>   e.g. --voice urgent\n")
        else:
            print(f"\n  No voice files found in {voices_dir}\n")
        sys.exit(0)

    # ── Load voice ────────────────────────────────────────────────────────────
    voice_name = args.voice or client_cfg.get("default_voice", DEFAULT_VOICE)
    voice_text = _load_voice(voice_name, voices_dir)
    if voice_text and not (voices_dir / f"{voice_name}.md").exists():
        voice_name = client_cfg.get("default_voice", DEFAULT_VOICE)

    # ── Load posts JSON ───────────────────────────────────────────────────────
    posts_path = args.posts or (DATA_SOCIAL_DIR / client_id / "social_posts.json")

    if not posts_path.exists():
        log.error(f"Posts file not found: {posts_path}")
        log.error("Run social_writer.py with --visual-director first to generate social_posts.json")
        sys.exit(1)

    posts_data = json.loads(posts_path.read_text(encoding="utf-8"))
    posts      = posts_data.get("posts", [])
    week       = posts_data.get("week", date.today().strftime("%G-W%V"))

    log.info(f"→ Loaded {len(posts)} posts from {posts_path.name} (week {week})")

    if not posts:
        log.error("No posts found in social_posts.json")
        sys.exit(1)

    # ── Print run banner ──────────────────────────────────────────────────────
    banner = f"{client_name} — Visual Director"
    print(f"\n  {banner}")
    print("  " + "─" * len(banner))
    print(f"\n  Client:  {client_name}")
    print(f"  Week:    {week}")
    print(f"  Voice:   {voice_name}")
    print(f"  Compare: {'yes' if args.compare else 'no'}")
    print(f"  Images:  {'yes' if args.images else 'no'}\n")

    # ── Enrich briefs via Claude ──────────────────────────────────────────────
    enriched_briefs = enrich_briefs(posts, client_cfg, voice_text, compare=args.compare)

    # ── Write enriched briefs JSON ────────────────────────────────────────────
    output_path = args.output or (DATA_SOCIAL_DIR / client_id / "visual_director_briefs.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    output_data = {
        "generated": datetime.now().isoformat(),
        "client":    client_id,
        "week":      week,
        "briefs":    enriched_briefs,
    }
    output_path.write_text(
        json.dumps(output_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info(f"→ Enriched briefs written to {output_path.relative_to(_PROJECT_ROOT)}")

    # ── Generate images ───────────────────────────────────────────────────────
    if args.images:
        iso_week   = week
        images_dir = (
            _PROJECT_ROOT / "outputs" / "clients" / client_id / "social" / "images" / iso_week
        )
        image_results = generate_images_from_enriched(enriched_briefs, images_dir)

        if image_results:
            print(f"\n  Generated images:")
            for post_num, paths in sorted(image_results.items()):
                for kind, p in paths.items():
                    try:
                        rel = p.relative_to(_PROJECT_ROOT)
                    except ValueError:
                        rel = p
                    print(f"    Post {post_num} ({kind}): {rel}")

    print(f"\n  ✓ Enriched briefs: {output_path.relative_to(_PROJECT_ROOT)}")
    if args.compare:
        print(f"\n  (Comparison printed above — scroll up to review)")
    print()


if __name__ == "__main__":
    main()
