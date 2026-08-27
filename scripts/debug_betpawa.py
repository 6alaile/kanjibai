"""
debug_betpawa.py — Debug BetPawa page structure in GitHub Actions.
Run this to dump full HTML and analyze DOM structure.
"""

import asyncio
from playwright.async_api import async_playwright

URLS = [
    "https://www.betpawa.co.tz/events?categoryId=2&marketId=1X2&sorting=competitionPriority_DESC",
    "https://www.betpawa.ke/events?categoryId=2&marketId=1X2&sorting=competitionPriority_DESC",
    "https://www.betpawa.ug/events?categoryId=2&marketId=1X2&sorting=competitionPriority_DESC",
]

async def debug():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")

        for url in URLS:
            print(f"\n{'='*60}")
            print(f"URL: {url}")
            print(f"{'='*60}")

            try:
                await page.goto(url, wait_until="networkidle", timeout=60000)
                await page.wait_for_load_state("networkidle", timeout=30000)
                await page.wait_for_selector("[class*='SportEvents_eventMatch']", timeout=15000)
                await asyncio.sleep(5)

                # Save full HTML
                html = await page.content()
                filename = f"debug_{url.split('.')[1]}.html"
                with open(filename, "w") as f:
                    f.write(html)
                print(f"Saved full HTML to {filename} ({len(html)} chars)")

                # Find event divs
                event_divs = await page.query_selector_all("[class*='SportEvents_eventMatch']")
                print(f"Found {len(event_divs)} event divs")

                if event_divs:
                    # Dump first 3 event divs
                    for i, div in enumerate(event_divs[:3]):
                        inner = await div.inner_html()
                        print(f"\n--- Event div {i} ---")
                        print(inner[:3000])
                        print("...")

                # Also check for any iframes
                frames = page.frames
                print(f"\nFrames: {len(frames)}")
                for frame in frames:
                    print(f"  Frame: {frame.url}")

            except Exception as e:
                print(f"Error: {e}")
                import traceback
                traceback.print_exc()

        await browser.close()

if __name__ == "__main__":
    asyncio.run(debug())