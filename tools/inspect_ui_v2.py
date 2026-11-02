from playwright.sync_api import sync_playwright


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1500, "height": 950})
    page.goto("http://127.0.0.1:8765/", wait_until="networkidle")
    print({
        "inbox_items": page.locator("#list li.item").count(),
        "pool_items": page.locator("#pool li.pool-item").count(),
        "document_visible": page.locator("#docwrap").is_visible(),
        "stage_cards": page.locator("#stages section.stage").count(),
        "left_text": page.locator(".side").inner_text(),
    })
    page.screenshot(path="runtime/ui-before.png", full_page=True)
    browser.close()
