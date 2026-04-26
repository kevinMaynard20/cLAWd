"""Generate the cLAWd app icon set programmatically.

Design: deep-navy background, large cream §-symbol (the legal section sign),
flanked by `{` `}` brackets to nod at the AI/code half of the app. Slight
inner glow + bottom shadow to give the icon some depth without leaning into
photographic textures (those don't survive the 32×32 downscale).

Output:
  apps/web/src-tauri/icons/{32x32,128x128,128x128@2x}.png
  apps/web/src-tauri/icons/icon.icns       (built via `iconutil`)
  apps/web/src-tauri/icons/icon.ico        (PIL)
  apps/web/src-tauri/icons/icon-1024.png   (master, kept for reference)

Run:  .venv/bin/python scripts/generate_icons.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# --- Palette ------------------------------------------------------------
# Deep navy: classic legal-firm trim, reads as serious. Cream on top: warm
# enough to avoid the cold "tech demo" look. Accent gold is for the
# brackets so they don't clash with the §-glyph and double as a frame.
NAVY = (15, 25, 47, 255)        # #0F192F
CREAM = (245, 232, 199, 255)    # #F5E8C7
GOLD = (191, 156, 64, 255)      # #BF9C40
SHADOW = (0, 0, 0, 110)         # soft drop-shadow under the §

# --- Layout knobs -------------------------------------------------------
SIZE = 1024  # master size; downscale lossily for the smaller PNGs


def _font(size: int) -> ImageFont.FreeTypeFont:
    """Pick a serif Mac system font that ships with macOS so the build is
    reproducible without bundling fonts. Fall back through a few options
    in case the build host has a stripped /System/Library/Fonts."""
    candidates = [
        "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
        "/System/Library/Fonts/Times.ttc",
        "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
        "/System/Library/Fonts/SFNSRounded.ttf",
        "/Library/Fonts/Times New Roman Bold.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    return ImageFont.load_default()


def _bracket_font(size: int) -> ImageFont.FreeTypeFont:
    """Mono / sans for the `{` `}` so they read as code, not prose."""
    candidates = [
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/SFNSMono.ttf",
        "/System/Library/Fonts/Monaco.ttf",
        "/System/Library/Fonts/Courier.ttc",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    return _font(size)


def _draw_master() -> Image.Image:
    """Build the 1024×1024 master we downscale from."""
    img = Image.new("RGBA", (SIZE, SIZE), NAVY)
    draw = ImageDraw.Draw(img)

    # Inner gradient — subtle radial glow centred slightly above the §
    # so the icon catches light at typical menu-bar viewing angles.
    glow_layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    glow_centre = (SIZE // 2, SIZE * 7 // 16)
    for r in range(SIZE // 2, 0, -16):
        alpha = max(0, int(40 - r * 40 / (SIZE // 2)))
        glow_draw.ellipse(
            (
                glow_centre[0] - r,
                glow_centre[1] - r,
                glow_centre[0] + r,
                glow_centre[1] + r,
            ),
            fill=(245, 232, 199, alpha),
        )
    img = Image.alpha_composite(img, glow_layer.filter(ImageFilter.GaussianBlur(48)))

    draw = ImageDraw.Draw(img)

    # Section sign §. We could use the Unicode glyph directly; some serif
    # fonts render it with thin strokes that disappear at 32×32, so we
    # oversize and apply a light bold offset for visual weight.
    section_font = _font(int(SIZE * 0.78))
    section_glyph = "§"
    bbox = draw.textbbox((0, 0), section_glyph, font=section_font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    cx = (SIZE - text_w) // 2 - bbox[0]
    cy = (SIZE - text_h) // 2 - bbox[1] - int(SIZE * 0.04)

    # Drop shadow first.
    shadow_layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    shadow_draw.text((cx + 6, cy + 14), section_glyph, font=section_font, fill=SHADOW)
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(10))
    img = Image.alpha_composite(img, shadow_layer)
    draw = ImageDraw.Draw(img)

    # Faux-bold by drawing the glyph twice, offset by 1px in opposite
    # directions. Stays sharper than ImageDraw's stroke= which thickens
    # asymmetrically on serifs.
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            draw.text((cx + dx, cy + dy), section_glyph, font=section_font, fill=CREAM)

    # Code-style brackets either side. Smaller, gold, closer to the
    # vertical centre. Margin tuned visually — the brackets shouldn't
    # touch the safe-area circle macOS clips icons against.
    bracket_font = _bracket_font(int(SIZE * 0.36))
    left_bracket = "{"
    right_bracket = "}"
    lb_bbox = draw.textbbox((0, 0), left_bracket, font=bracket_font)
    lb_w = lb_bbox[2] - lb_bbox[0]
    lb_h = lb_bbox[3] - lb_bbox[1]
    bracket_y = (SIZE - lb_h) // 2 - lb_bbox[1] - int(SIZE * 0.02)
    draw.text(
        (int(SIZE * 0.10) - lb_bbox[0], bracket_y),
        left_bracket,
        font=bracket_font,
        fill=GOLD,
    )
    rb_bbox = draw.textbbox((0, 0), right_bracket, font=bracket_font)
    rb_w = rb_bbox[2] - rb_bbox[0]
    draw.text(
        (SIZE - int(SIZE * 0.10) - rb_w - rb_bbox[0], bracket_y),
        right_bracket,
        font=bracket_font,
        fill=GOLD,
    )

    # Bottom caption: a subtle "cLAWd" wordmark in the lower safe area.
    # Tiny — only really legible at 256+. Builds brand without dominating
    # the §.
    caption_font = _bracket_font(int(SIZE * 0.072))
    caption = "cLAWd"
    cap_bbox = draw.textbbox((0, 0), caption, font=caption_font)
    cap_w = cap_bbox[2] - cap_bbox[0]
    draw.text(
        ((SIZE - cap_w) // 2 - cap_bbox[0], int(SIZE * 0.86)),
        caption,
        font=caption_font,
        fill=GOLD,
    )

    return img


def _round_corners(im: Image.Image, radius_pct: float = 0.225) -> Image.Image:
    """Apply the macOS squircle-ish corner mask. Tauri's bundle pipeline
    doesn't auto-round; rounding here means the icon looks consistent
    whether it's the .app, the Dock, or LaunchPad."""
    radius = int(im.size[0] * radius_pct)
    mask = Image.new("L", im.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, im.size[0], im.size[1]), radius=radius, fill=255)
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def _build_icns(master: Image.Image, icns_target: Path) -> bool:
    """Build an .icns via macOS `iconutil`. Falls back to a plain PNG
    rename when iconutil isn't on $PATH (CI on Linux, etc.). Returns
    True when a real .icns was produced."""
    iconutil = shutil.which("iconutil")
    if iconutil is None:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        # Apple's required size matrix.
        for label, px in [
            ("icon_16x16.png", 16),
            ("icon_16x16@2x.png", 32),
            ("icon_32x32.png", 32),
            ("icon_32x32@2x.png", 64),
            ("icon_128x128.png", 128),
            ("icon_128x128@2x.png", 256),
            ("icon_256x256.png", 256),
            ("icon_256x256@2x.png", 512),
            ("icon_512x512.png", 512),
            ("icon_512x512@2x.png", 1024),
        ]:
            master.resize((px, px), Image.LANCZOS).save(iconset / label, "PNG")
        subprocess.check_call(
            [iconutil, "-c", "icns", str(iconset), "-o", str(icns_target)]
        )
    return True


def main() -> None:
    repo = Path(__file__).resolve().parent.parent
    out = repo / "apps" / "web" / "src-tauri" / "icons"
    out.mkdir(parents=True, exist_ok=True)

    print("→ rendering 1024×1024 master")
    master_raw = _draw_master()
    master = _round_corners(master_raw)
    master.save(out / "icon-1024.png", "PNG")

    print("→ writing PNG sizes")
    sizes = {
        "32x32.png": 32,
        "128x128.png": 128,
        "128x128@2x.png": 256,
    }
    for name, px in sizes.items():
        master.resize((px, px), Image.LANCZOS).save(out / name, "PNG")
        print(f"   {name} ({px}×{px})")

    print("→ writing ICO (Windows; placeholder OK on Mac)")
    ico_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master.save(out / "icon.ico", sizes=ico_sizes)

    print("→ writing ICNS via iconutil")
    if _build_icns(master_raw, out / "icon.icns"):
        print("   icon.icns")
    else:
        # Iconutil missing — Tauri's bundle step still wants the file to
        # exist + be a real icns. Fall back to a 1024 PNG renamed; the
        # bundler will warn but won't reject.
        master.save(out / "icon.icns", "PNG")
        print("   (iconutil not found; wrote PNG-as-.icns fallback)")

    print(f"\n✓ icons in {out}")


if __name__ == "__main__":
    sys.exit(main())
