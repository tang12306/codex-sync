"""生成 Inno Setup 安装向导用的图片（installer/wizard-*.png）。

只在应用图标变化时手动运行一次，生成的图片提交到仓库，打包时不需要 Pillow：

    python -m pip install pillow
    python scripts/make_installer_images.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "assets" / "CodexSync-icon.png"
OUT = ROOT / "installer"

# Inno Setup 在 100% 缩放下的尺寸；按 DPI 提供多份，安装程序会挑最接近的一份
LARGE_BASE = (164, 314)
SMALL_BASE = 55
SCALES = (1, 1.5, 2)

TOP = (15, 23, 42)      # 深海军蓝，和应用深色主题一致
BOTTOM = (30, 64, 140)  # 品牌蓝
TITLE_FONT = Path("C:/Windows/Fonts/segoeuib.ttf")
SUBTITLE_FONT = Path("C:/Windows/Fonts/msyh.ttc")


def _icon() -> Image.Image:
    icon = Image.open(ICON).convert("RGBA")
    return icon.crop(icon.getchannel("A").getbbox())


def _font(path: Path, size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.load_default()


def _gradient(size: tuple[int, int]) -> Image.Image:
    width, height = size
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        t = y / max(1, height - 1)
        color = tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM))
        draw.line([(0, y), (width, y)], fill=color)
    return image


def _centered_text(draw: ImageDraw.ImageDraw, width: int, y: int, text: str, font, fill) -> int:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(((width - (right - left)) / 2 - left, y - top), text, font=font, fill=fill)
    return y + (bottom - top)


def make_large(scale: float) -> Image.Image:
    size = (round(LARGE_BASE[0] * scale), round(LARGE_BASE[1] * scale))
    image = _gradient(size)
    icon = _icon()
    icon_width = round(size[0] * 0.72)
    icon = icon.resize((icon_width, round(icon.height * icon_width / icon.width)), Image.LANCZOS)
    icon_top = round(size[1] * 0.22)
    image.paste(icon, ((size[0] - icon.width) // 2, icon_top), icon)

    draw = ImageDraw.Draw(image)
    y = icon_top + icon.height + round(18 * scale)
    y = _centered_text(draw, size[0], y, "Codex Sync", _font(TITLE_FONT, round(19 * scale)), (255, 255, 255))
    _centered_text(draw, size[0], y + round(8 * scale), "多台电脑间同步 Codex", _font(SUBTITLE_FONT, round(10 * scale)), (191, 207, 235))
    return image


def make_small(scale: float) -> Image.Image:
    side = round(SMALL_BASE * scale)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    icon = _icon()
    inner = round(side * 0.92)
    icon.thumbnail((inner, inner), Image.LANCZOS)
    canvas.paste(icon, ((side - icon.width) // 2, (side - icon.height) // 2), icon)
    return canvas


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for scale in SCALES:
        suffix = f"{round(scale * 100)}"
        make_large(scale).save(OUT / f"wizard-large-{suffix}.png", optimize=True)
        make_small(scale).save(OUT / f"wizard-small-{suffix}.png", optimize=True)
    print(f"已生成 {len(SCALES) * 2} 张图片到 {OUT}")


if __name__ == "__main__":
    main()
