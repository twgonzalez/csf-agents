"""Tests: build_prompt() brand injection for CSF and CMA client identities.

build_prompt() is a pure function — no API calls, no file I/O.
"""
import pytest

from agents.social.image_generator import build_prompt, _CSF_BRAND_DEFAULTS


# ---------------------------------------------------------------------------
# CSF brand colors in prompt
# ---------------------------------------------------------------------------

def test_csf_prompt_background_color(image_brief, csf_client):
    prompt = build_prompt(image_brief, "1:1", brand=csf_client)
    # build_prompt applies .capitalize() to bg_name, so check case-insensitively
    assert "deep navy blue" in prompt.lower()


def test_csf_prompt_accent_color(image_brief, csf_client):
    prompt = build_prompt(image_brief, "1:1", brand=csf_client)
    assert "warm gold" in prompt


# ---------------------------------------------------------------------------
# CMA brand colors in prompt
# ---------------------------------------------------------------------------

def test_cma_prompt_background_color(image_brief, cma_client):
    prompt = build_prompt(image_brief, "1:1", brand=cma_client)
    # build_prompt applies .capitalize() to bg_name, so check case-insensitively
    assert "deep forest green" in prompt.lower()


def test_cma_prompt_accent_color(image_brief, cma_client):
    prompt = build_prompt(image_brief, "1:1", brand=cma_client)
    assert "warm amber" in prompt


# ---------------------------------------------------------------------------
# CSF and CMA prompts are distinct
# ---------------------------------------------------------------------------

def test_csf_cma_prompts_differ(image_brief, csf_client, cma_client):
    csf_prompt = build_prompt(image_brief, "1:1", brand=csf_client)
    cma_prompt = build_prompt(image_brief, "1:1", brand=cma_client)
    assert csf_prompt != cma_prompt


# ---------------------------------------------------------------------------
# brand=None falls back to CSF defaults
# ---------------------------------------------------------------------------

def test_no_brand_uses_csf_defaults(image_brief, csf_client):
    no_brand  = build_prompt(image_brief, "1:1", brand=None)
    csf_brand = build_prompt(image_brief, "1:1", brand=csf_client)
    assert no_brand == csf_brand


# ---------------------------------------------------------------------------
# Brief content injected verbatim
# ---------------------------------------------------------------------------

def test_bill_number_in_prompt(image_brief, csf_client):
    prompt = build_prompt(image_brief, "1:1", brand=csf_client)
    assert image_brief["bill_number"] in prompt


def test_headline_in_prompt_verbatim(image_brief, csf_client):
    prompt = build_prompt(image_brief, "1:1", brand=csf_client)
    assert image_brief["headline"] in prompt


# ---------------------------------------------------------------------------
# Structural style rules preserved regardless of brand
# ---------------------------------------------------------------------------

def test_style_rules_preserved_csf(image_brief, csf_client):
    prompt = build_prompt(image_brief, "1:1", brand=csf_client)
    assert "No photos" in prompt


def test_style_rules_preserved_cma(image_brief, cma_client):
    prompt = build_prompt(image_brief, "1:1", brand=cma_client)
    assert "No photos" in prompt


# ---------------------------------------------------------------------------
# Aspect ratio labels
# ---------------------------------------------------------------------------

def test_square_ratio_label(image_brief, csf_client):
    prompt = build_prompt(image_brief, "1:1", brand=csf_client)
    assert "square (1:1)" in prompt


def test_landscape_ratio_label(image_brief, csf_client):
    prompt = build_prompt(image_brief, "16:9", brand=csf_client)
    assert "16:9" in prompt
