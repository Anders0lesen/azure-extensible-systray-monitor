from __future__ import annotations

from pathlib import Path

from PIL import Image


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    assets = root / "assets"
    source = assets / "AzureHealthBeacon-Brand.png"
    image = Image.open(source).convert("RGB")

    square_size = max(image.size)
    square = Image.new("RGB", (square_size, square_size), "black")
    square.paste(
        image,
        ((square_size - image.width) // 2, (square_size - image.height) // 2),
    )
    square.save(assets / "AzureHealthBeacon-Brand-Square.png", optimize=True)
    square.save(
        assets / "AzureHealthBeacon.ico",
        format="ICO",
        sizes=[
            (16, 16),
            (24, 24),
            (32, 32),
            (48, 48),
            (64, 64),
            (128, 128),
            (256, 256),
        ],
    )

    wizard = Image.new("RGB", (328, 628), "black")
    reduced = square.resize((300, 300), Image.Resampling.LANCZOS)
    wizard.paste(
        reduced,
        (
            (wizard.width - reduced.width) // 2,
            (wizard.height - reduced.height) // 2,
        ),
    )
    wizard.save(assets / "AzureHealthBeacon-Installer-Wizard.png", optimize=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
