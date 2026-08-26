from __future__ import annotations

import math

from PIL import Image, ImageDraw

from .model import BeaconState

SIZE = 64
GREEN = (45, 190, 93, 255)
RED = (225, 55, 65, 255)
AMBER = (246, 170, 33, 255)
GREY = (135, 142, 150, 255)
WHITE = (242, 244, 247, 255)


def _ball(color: tuple[int, int, int, int], opacity: float = 1.0) -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    alpha = max(25, min(255, int(255 * opacity)))
    base = (*color[:3], alpha)
    draw.ellipse((8, 8, 56, 56), fill=base, outline=(20, 24, 28, alpha), width=2)
    highlight_alpha = max(15, int(150 * opacity))
    draw.ellipse((17, 15, 31, 27), fill=(255, 255, 255, highlight_alpha))
    return image


def _crosshatched() -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).ellipse((8, 8, 56, 56), fill=255)
    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.ellipse((8, 8, 56, 56), fill=GREY)
    for offset in range(-48, 80, 8):
        draw.line((offset, 60, offset + 60, 0), fill=(65, 70, 76, 230), width=3)
    image.alpha_composite(Image.composite(layer, Image.new("RGBA", image.size), mask))
    ImageDraw.Draw(image).ellipse((8, 8, 56, 56), outline=(65, 70, 76, 255), width=2)
    return image


def _spinner(frame: int) -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    start = (frame * 45) % 360
    end = start + 275
    draw.arc((10, 10, 54, 54), start=start, end=end, fill=WHITE, width=7)
    radians = math.radians(end)
    x = 32 + 22 * math.cos(radians)
    y = 32 + 22 * math.sin(radians)
    direction = radians + math.pi / 2
    points = [
        (x, y),
        (x + 10 * math.cos(direction + 0.7), y + 10 * math.sin(direction + 0.7)),
        (x + 10 * math.cos(direction - 0.7), y + 10 * math.sin(direction - 0.7)),
    ]
    draw.polygon(points, fill=WHITE)
    return image


def icon_for(state: BeaconState, frame: int = 0) -> Image.Image:
    if state is BeaconState.HEALTHY:
        return _ball(GREEN)
    if state is BeaconState.FAILED:
        return _ball(RED)
    if state is BeaconState.UNCONNECTABLE:
        return _crosshatched()
    if state is BeaconState.CONNECTING:
        return _spinner(frame)
    pulse = 0.35 + 0.65 * ((math.sin(frame * math.pi / 8) + 1) / 2)
    return _ball(AMBER, pulse)
