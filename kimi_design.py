# Last modified: 2026-04-15 | Change: Initial build — Kimi API design consultant for CorridorKey plugin UI
"""CorridorKey — Kimi Design Consultant

WHAT IT DOES: Sends the current plugin UI code (HTML/CSS + Qt stylesheet) to
  Kimi's API and asks for visual design improvements. Saves the response to
  a file that Claude Code can read and implement.

DEPENDS-ON: KIMI_API_KEY env var, requests library, internet connection.
AFFECTS: Writes kimi_response.md to this folder. Reads plugin source files.
"""
import os
import sys
import json
import argparse
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
API_KEY = os.environ.get("KIMI_API_KEY", "")
API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "moonshotai/kimi-k2"  # Kimi K2 via OpenRouter

PLUGIN_DIR = Path(__file__).parent / "ae_plugin" / "cep_panel"
INDEX_HTML = PLUGIN_DIR / "index.html"
VIEWER_PY = PLUGIN_DIR / "preview_viewer.py"
OUTPUT_FILE = Path(__file__).parent / "kimi_response.md"

# ── Design prompt ───────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a senior UI/UX designer specializing in professional creative tool plugins
(Adobe After Effects, Premiere Pro, DaVinci Resolve). You have deep expertise in:
- Adobe CEP panel design (HTML/CSS in Chromium Embedded Framework)
- Qt/PySide6 desktop application styling (QSS stylesheets)
- Dark theme design systems for video editing tools
- Color theory, typography, spacing, and visual hierarchy

Your job is to review the current plugin UI code and provide SPECIFIC, IMPLEMENTABLE
design improvements. Not vague suggestions — actual CSS rules, color values, spacing
values, and layout changes.

Design language reference:
- Brand color: #00C853 (accent green)
- Background: #141414 (base dark)
- Surface: #1e1e1e (elevated surfaces)
- Text primary: #e8e8e8
- Text secondary: #888
- Font stack: Inter, SF Pro Display, Segoe UI
- Mono: JetBrains Mono, SF Mono, Consolas

The plugin has TWO visual surfaces:
1. CEP Panel (index.html) — embedded in Adobe's panel system, ~250px wide
2. Preview Viewer (preview_viewer.py) — standalone Qt window with image panes + sliders
Both must look like they belong to the same product."""

USER_PROMPT_TEMPLATE = """Review these two files from the CorridorKey plugin — a professional green-screen
keying tool for Adobe After Effects and Premiere Pro.

I need you to focus on:
1. Overall visual polish — does it look like a $200 professional plugin?
2. Slider design — the sliders need to feel like precision instruments
3. Button hierarchy — primary/secondary/destructive actions should be visually distinct
4. Layout and spacing — tight but breathable, no wasted space
5. The Qt viewer window — should visually match the HTML panel

For EACH suggestion, give me:
- What to change (specific selector or widget)
- The exact CSS/QSS code
- Why it improves the design

=== FILE 1: index.html (Adobe CEP Panel) ===
```html
{html_content}
```

=== FILE 2: preview_viewer.py (Qt Viewer — look for _DARK_STYLE and _build_ui) ===
```python
{viewer_content}
```

Give me your top 10-15 highest-impact design changes, ordered by visual impact.
Do NOT suggest functionality changes — visual design ONLY."""


def load_file(path, max_chars=60000):
    """WHAT IT DOES: Reads a source file, truncating if over max_chars.
    DEPENDS-ON: File exists at path.
    AFFECTS: Nothing — pure read."""
    try:
        text = path.read_text(encoding="utf-8")
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n... [truncated at {max_chars} chars] ..."
        return text
    except Exception as e:
        return f"[ERROR reading {path}: {e}]"


def call_kimi(system_msg, user_msg, model=MODEL):
    """WHAT IT DOES: Sends a chat completion request to Kimi via OpenRouter API.
    DEPENDS-ON: KIMI_API_KEY env var, network access to openrouter.ai.
    AFFECTS: Network call only. Returns response text or error string."""
    try:
        import requests
    except ImportError:
        # Fall back to urllib if requests not installed
        import urllib.request
        import urllib.error

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        }
        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.7,
            "max_tokens": 16384,
        })
        req = urllib.request.Request(API_URL, data=payload.encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode())
                return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            return f"[HTTP {e.code}] {body}"
        except Exception as e:
            return f"[ERROR] {e}"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.7,
        "max_tokens": 16384,
    }
    try:
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=300)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[ERROR] {e}"


def main():
    parser = argparse.ArgumentParser(description="Ask Kimi for CorridorKey UI design feedback")
    parser.add_argument("--prompt", type=str, help="Custom design question (appended to standard prompt)")
    parser.add_argument("--model", type=str, default=MODEL, help=f"Kimi model ID (default: {MODEL})")
    parser.add_argument("--output", type=str, default=str(OUTPUT_FILE), help="Output file path")
    args = parser.parse_args()

    if not API_KEY:
        print("ERROR: Set KIMI_API_KEY environment variable first.")
        print("  Windows:  set KIMI_API_KEY=sk-your-key-here")
        print("  Then run: python kimi_design.py")
        sys.exit(1)

    model = args.model

    print(f"[1/3] Loading plugin source files...")
    html_content = load_file(INDEX_HTML)
    viewer_content = load_file(VIEWER_PY)

    user_msg = USER_PROMPT_TEMPLATE.format(
        html_content=html_content,
        viewer_content=viewer_content,
    )

    if args.prompt:
        user_msg += f"\n\nADDITIONAL FOCUS: {args.prompt}"

    print(f"[2/3] Sending to Kimi ({model})... this takes 30-60 seconds")
    response = call_kimi(SYSTEM_PROMPT, user_msg, model)

    output_path = Path(args.output)
    header = f"# Kimi Design Review — CorridorKey Plugin\n\n"
    header += f"**Model:** {model}\n"
    header += f"**Date:** {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n---\n\n"
    output_path.write_text(header + response, encoding="utf-8")

    print(f"[3/3] Response saved to: {output_path}")
    print(f"\nPreview (first 500 chars):\n{'='*60}")
    print(response[:500])
    if len(response) > 500:
        print(f"\n... [{len(response)} total chars — see full file]")


if __name__ == "__main__":
    main()
