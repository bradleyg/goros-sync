"""Generate the PWA icon set from one SVG design.

Writes static/icons/*.svg and renders PNGs with headless Chrome (no Python
imaging deps needed). Re-run after tweaking the design:

    python scripts/make_icons.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "static" / "icons"

CHROME_CANDIDATES = [
    os.environ.get("CHROME", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "chromium",
    "chromium-browser",
]


def design(rounded: bool, scale: float = 1.0) -> str:
    """512x512 icon. `rounded` = transparent corners; else full-bleed (maskable / Apple)."""
    bg = (
        '<rect width="512" height="512" rx="116" fill="url(#bg)"/>'
        '<rect x="1.5" y="1.5" width="509" height="509" rx="114.5" fill="none" stroke="url(#rim)" stroke-width="3"/>'
        if rounded
        else '<rect width="512" height="512" fill="url(#bg)"/>'
    )
    # Two arcs chasing each other round a circle (the "sync" loop): white for Garmin,
    # orange for COROS, each ending in an arrowhead. The second is the first rotated 180°.
    arc = (
        '<path d="M124.4 208.1 A140 140 0 0 1 368.6 173.4" fill="none" stroke-width="40" stroke-linecap="round"/>'
        '<path d="M398 220 L402.5 141.5 L330.1 198.1 Z" stroke-width="10" stroke-linejoin="round"/>'
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <defs>
    <radialGradient id="bg" cx="50%" cy="30%" r="80%">
      <stop offset="0" stop-color="#262c3a"/>
      <stop offset="1" stop-color="#11141b"/>
    </radialGradient>
    <linearGradient id="rim" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ffffff" stop-opacity=".14"/>
      <stop offset="1" stop-color="#ffffff" stop-opacity="0"/>
    </linearGradient>
    <linearGradient id="white" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#cfd6e4"/>
      <stop offset="1" stop-color="#ffffff"/>
    </linearGradient>
    <linearGradient id="orange" x1="1" y1="0" x2="0" y2="0">
      <stop offset="0" stop-color="#ff8a4c"/>
      <stop offset="1" stop-color="#ff4f2e"/>
    </linearGradient>
    <filter id="glow" x="-30%" y="-30%" width="160%" height="160%">
      <feGaussianBlur stdDeviation="14"/>
    </filter>
  </defs>
  {bg}
  <g transform="translate(256 256) scale({scale}) translate(-256 -256)">
    <circle cx="256" cy="256" r="150" fill="none" stroke="#ff5a36" stroke-opacity=".16" stroke-width="44" filter="url(#glow)"/>
    <g stroke="url(#white)" fill="url(#white)">{arc}</g>
    <g stroke="url(#orange)" fill="url(#orange)" transform="rotate(180 256 256)">{arc}</g>
    <path d="M178 256 h30 l18 -46 l30 92 l20 -60 l12 14 h46" fill="none" stroke="#ffffff" stroke-opacity=".92"
          stroke-width="18" stroke-linecap="round" stroke-linejoin="round"/>
  </g>
</svg>
"""


def find_chrome() -> str:
    for c in CHROME_CANDIDATES:
        if c and (Path(c).exists() or shutil.which(c)):
            return c
    sys.exit("Chrome/Chromium not found; set $CHROME")


def render(chrome: str, svg: str, size: int, dest: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "icon.html"
        body = svg.replace('width="512" height="512"', f'width="{size}" height="{size}"', 1)
        page.write_text(
            "<!doctype html><html><head><style>html,body{margin:0;background:transparent}"
            f"svg{{display:block}}</style></head><body>{body}</body></html>"
        )
        subprocess.run(
            [
                chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                "--default-background-color=00000000", "--force-device-scale-factor=1",
                f"--window-size={size},{size}", f"--screenshot={dest}", page.as_uri(),
            ],
            check=True, capture_output=True,
        )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rounded, bleed = design(rounded=True), design(rounded=False, scale=0.86)
    (OUT / "icon.svg").write_text(rounded)
    (OUT / "maskable.svg").write_text(bleed)

    chrome = find_chrome()
    jobs = [
        (rounded, 192, "icon-192.png"),
        (rounded, 512, "icon-512.png"),
        (bleed, 192, "maskable-192.png"),
        (bleed, 512, "maskable-512.png"),
        (bleed, 180, "apple-touch-icon.png"),
        (rounded, 32, "favicon-32.png"),
    ]
    for svg, size, name in jobs:
        render(chrome, svg, size, OUT / name)
        print("wrote", OUT / name)


if __name__ == "__main__":
    main()
