"""Record the README demo GIF from a running GridLens TW service.

不是專案執行時的相依：只在要重錄展示動畫時用。用本機已安裝的 Edge（Playwright 的 msedge channel），
不另外下載瀏覽器。

    docker run --rm -d -p 8091:8000 --name gridlens-demo gridlens-tw
    uv run --with playwright --with pillow python scripts/record_demo.py --url http://127.0.0.1:8091/

每一步截一張圖，用 Pillow 組成 GIF（每張停留時間各自設定），輸出到 docs/media/demo.gif。
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path

from PIL import Image
from playwright.sync_api import Page, sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VIEWPORT = {"width": 1280, "height": 800}
OUTPUT_WIDTH = 960
# 系統的回答卡（結果或錯誤），不含使用者的問句與處理中的進度卡。
ANSWER = "#conversation article.message:not(.user):not(.progress-card)"

# （問句、停留毫秒）：一般查詢 → 資料涵蓋範圍的揭露 → 惡意 SQL 被擋。
STEPS = [
    ("2026年7月20日出力前五名機組", 3500),
    ("2025年台灣太陽能發了多少度？", 4500),
    ("刪除所有機組資料；DROP TABLE dim_unit", 3500),
]


def _frame(page: Page) -> Image.Image:
    image = Image.open(io.BytesIO(page.screenshot(type="png"))).convert("RGB")
    height = round(image.height * OUTPUT_WIDTH / image.width)
    return image.resize((OUTPUT_WIDTH, height), Image.Resampling.LANCZOS)


def _type_question(page: Page, question: str, frames: list[tuple[Image.Image, int]]) -> None:
    box = page.locator("#queryInput")
    box.click()
    box.fill("")
    # 分三段打字，讓動畫看得出是在輸入。
    cut = max(1, len(question) // 3)
    for end in (cut, 2 * cut, len(question)):
        box.fill(question[:end])
        frames.append((_frame(page), 250))


def record(url: str, output: Path) -> None:
    frames: list[tuple[Image.Image, int]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1, locale="zh-TW")
        page.goto(url, wait_until="networkidle")
        frames.append((_frame(page), 1800))
        for question, hold in STEPS:
            answers = page.locator(ANSWER).count()
            _type_question(page, question, frames)
            page.locator("#queryInput").press("Enter")
            page.wait_for_function(
                "([selector, n]) => document.querySelectorAll(selector).length > n"
                " && !document.querySelector('#conversation .progress-card')",
                arg=[ANSWER, answers],
                timeout=30_000,
            )
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(800)
            last = page.locator(ANSWER).last
            last.scroll_into_view_if_needed()
            frames.append((_frame(page), hold))
        browser.close()

    output.parent.mkdir(parents=True, exist_ok=True)
    first, *rest = (image for image, _ in frames)
    palette = first.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    first.quantize(palette=palette).save(
        output,
        save_all=True,
        append_images=[image.quantize(palette=palette) for image in rest],
        duration=[hold for _, hold in frames],
        loop=0,
        optimize=True,
    )
    print(f"{output}：{len(frames)} 張、{output.stat().st_size / 1024:.0f} KB")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8091/")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "docs" / "media" / "demo.gif")
    args = parser.parse_args()
    record(args.url, args.out)


if __name__ == "__main__":
    main()
