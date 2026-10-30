# Draws the Game Guide macOS app icon and writes build/icon.icns. Run: uv run python build/icon.py
import os
import shutil
import subprocess
import tempfile
from PIL import Image, ImageDraw


def generate_icon(out_path: str | None = None) -> str:
    S = 1024
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # macOS icon grid: 824px rounded square centred in the 1024 canvas
    d.rounded_rectangle((100, 100, 924, 924), radius=185, fill=(17, 24, 38), outline=(36, 50, 74), width=6)

    # Subtle inner controller body silhouette
    d.rounded_rectangle((210, 290, 814, 660), radius=140, fill=(22, 31, 48), outline=(54, 73, 104), width=4)

    # Left D-pad in warm amber (245, 158, 11)
    amber = (245, 158, 11)
    dx, dy = 365, 475
    d.rounded_rectangle((dx - 32, dy - 95, dx + 32, dy + 95), radius=14, fill=amber)
    d.rounded_rectangle((dx - 95, dy - 32, dx + 95, dy + 32), radius=14, fill=amber)
    d.ellipse((dx - 18, dy - 18, dx + 18, dy + 18), fill=(17, 24, 38))

    # Right action buttons in emerald (16, 185, 129), sky blue (56, 189, 248), and amber
    emerald = (16, 185, 129)
    sky = (56, 189, 248)
    rose = (244, 63, 94)
    bx, by = 665, 475
    r = 28
    offset = 64
    for ox, oy, col in ((0, -offset, amber), (offset, 0, emerald), (0, offset, sky), (-offset, 0, rose)):
        cx, cy = bx + ox, by + oy
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col)

    # Bottom spoiler-level accent rule (Hint / Strategy / Answer bars)
    bars = [amber, sky, emerald]
    for i, c in enumerate(bars):
        x0 = 292 + i * 152
        d.rounded_rectangle((x0, 760, x0 + 136, 790), radius=15, fill=c)

    build = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(build, exist_ok=True)
    # The portable formats: icon.png is the Linux menu entry's icon, icon.ico the Windows shortcut's.
    img.resize((512, 512), Image.LANCZOS).save(os.path.join(build, "icon.png"))
    img.save(os.path.join(build, "icon.ico"), format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    out = out_path or os.path.join(build, "icon.icns")
    if shutil.which("iconutil"):  # macOS: also refresh the app bundle's .icns; Linux CI needs just the PNG and ICO
        with tempfile.TemporaryDirectory() as tmp:
            iconset = os.path.join(tmp, "icon.iconset")
            os.mkdir(iconset)
            for size in (16, 32, 128, 256, 512):
                for scale in (1, 2):
                    name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                    img.resize((size * scale, size * scale), Image.LANCZOS).save(os.path.join(iconset, name))
            subprocess.run(["/usr/bin/iconutil", "-c", "icns", iconset, "-o", out], check=True)
    return out


if __name__ == "__main__":
    print(generate_icon())
