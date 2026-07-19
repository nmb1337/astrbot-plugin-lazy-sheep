"""无需 matplotlib 的轻量趋势图生成器。"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _font(size: int):
    candidates = (
        "C:/Windows/Fonts/msyh.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def render_trend_chart(title: str, series: Sequence[tuple[date, int]]) -> str:
    """绘制 30 日趋势图，返回可由 event.image_result 发送的临时 PNG 路径。"""
    width, height = 1080, 620
    image = Image.new("RGB", (width, height), "#fff8df")
    draw = ImageDraw.Draw(image)
    title_font, body_font, small_font = _font(38), _font(24), _font(18)
    draw.rounded_rectangle((28, 24, width - 28, height - 28), radius=30, fill="#fffdf5", outline="#f0cd77", width=3)
    draw.text((66, 56), title, font=title_font, fill="#64222a")

    left, top, right, bottom = 110, 160, width - 70, height - 120
    draw.line((left, bottom, right, bottom), fill="#b08049", width=3)
    draw.line((left, top, left, bottom), fill="#b08049", width=3)
    values = [value for _, value in series] or [0]
    maximum = max(max(values), 1)
    for line in range(5):
        y = bottom - (bottom - top) * line / 4
        value = round(maximum * line / 4)
        draw.line((left, y, right, y), fill="#f3e5c2", width=1)
        draw.text((32, y - 10), str(value), font=small_font, fill="#8a6a3e")

    if len(series) == 1:
        points = [(left, bottom - (bottom - top) * series[0][1] / maximum)]
    else:
        points = [
            (
                left + (right - left) * index / (len(series) - 1),
                bottom - (bottom - top) * value / maximum,
            )
            for index, (_, value) in enumerate(series)
        ]
    if len(points) > 1:
        draw.line(points, fill="#dd7c46", width=5, joint="curve")
    for x, y in points:
        draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill="#8f3c35")
    if series:
        draw.text((left, bottom + 28), series[0][0].strftime("%m-%d"), font=body_font, fill="#8a6a3e")
        end_text = series[-1][0].strftime("%m-%d")
        text_width = draw.textbbox((0, 0), end_text, font=body_font)[2]
        draw.text((right - text_width, bottom + 28), end_text, font=body_font, fill="#8a6a3e")
    draw.text((left, height - 68), "近 30 天，每个点代表一天", font=small_font, fill="#8a6a3e")

    file_descriptor, raw_target = tempfile.mkstemp(prefix="lazy_sheep_trend_", suffix=".png")
    os.close(file_descriptor)
    target = Path(raw_target)
    image.save(target, format="PNG", optimize=True)
    return str(target)
