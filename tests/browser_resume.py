"""先启动 python -m tests.resume_server，再运行本脚本。"""
import io
import zipfile
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def main():
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as bundle:
        bundle.writestr("main.py", "def run():\n    return 42\n")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://127.0.0.1:8083")
        page.get_by_role("button", name="上传 ZIP", exact=True).click()
        page.locator("#zipFile").set_input_files({"name": "resume.zip", "mimeType": "application/zip", "buffer": content.getvalue()})
        page.locator("#mode").select_option("agent")
        page.locator("#submit").click()
        expect(page.locator("#continueAnalysis")).to_be_visible(timeout=30000)
        expect(page.locator("#jobError")).to_contain_text("temporary upstream response failure")
        original_url = page.url
        page.reload()
        expect(page.locator("#continueAnalysis")).to_be_visible()
        page.locator("#continueAnalysis").click()
        expect(page.locator("#resultLink")).to_be_visible(timeout=30000)
        assert page.url == original_url
        expect(page.locator("#logs")).to_contain_text("恢复检查点")
        expect(page.locator("#jobError")).to_have_text("")
        expect(page.locator("#continueAnalysis")).not_to_be_visible()
        Path("test-results").mkdir(exist_ok=True)
        page.screenshot(path="test-results/continued-task.png", full_page=True)
        page.locator("#resultLink").click()
        expect(page.locator("#content")).to_contain_text("返回一个常量")
        assert not errors, errors
        browser.close()
    print("Browser continuation passed: failure → refresh → manual continue → same job → result")


if __name__ == "__main__":
    main()
