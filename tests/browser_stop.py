"""运行 WIKI_TEST_STOP=1 python -m tests.resume_server 后执行。"""
import io
import zipfile
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def main():
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as bundle:
        bundle.writestr("main.py", "def run():\n    return 42\n")
    output = Path("test-results")
    output.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless=True)
        monitor = browser.new_page(viewport={"width": 1280, "height": 900})
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        for tab in (monitor, page):
            tab.on("pageerror", lambda error: errors.append(str(error)))
        identifier = None
        try:
            monitor.goto("http://127.0.0.1:8083")
            page.goto("http://127.0.0.1:8083")
            page.get_by_role("button", name="上传 ZIP", exact=True).click()
            page.locator("#zipFile").set_input_files({"name": "stop.zip", "mimeType": "application/zip", "buffer": content.getvalue()})
            page.locator("#mode").select_option("agent")
            page.locator("#submit").click()
            expect(page.locator("#jobStatus")).to_have_text("正在运行", timeout=15000)
            expect(page.locator("#logs")).to_contain_text("已保存功能", timeout=15000)
            original = page.url
            identifier = original.rsplit("/", 1)[1]
            monitor.bring_to_front()
            expect(monitor.locator(".job-row .status.running")).to_be_visible(timeout=8000)
            monitor.locator(".job-row").first.click()
            expect(monitor.locator("#jobStatus")).to_have_text("正在运行")
            expect(monitor.locator("#stopAnalysis")).to_be_enabled()
            monitor.screenshot(path=str(output / "running-status.png"), full_page=True)
            monitor.locator("#stopAnalysis").click()
            expect(monitor.locator("#jobStatus")).to_have_text("停止中")
            expect(monitor.locator("#stopAnalysis")).to_be_disabled()
            expect(monitor.locator("#jobStatus")).to_have_text("已停止", timeout=15000)
            expect(monitor.locator("#continueAnalysis")).to_be_visible()
            expect(monitor.locator("#stopAnalysis")).not_to_be_visible()
            expect(monitor.locator("#resultLink")).not_to_be_visible()
            monitor.reload()
            expect(monitor.locator("#jobStatus")).to_have_text("已停止")
            monitor.set_viewport_size({"width": 390, "height": 844})
            assert monitor.evaluate("document.documentElement.scrollWidth <= innerWidth")
            monitor.screenshot(path=str(output / "stopped-mobile.png"), full_page=True)
            monitor.set_viewport_size({"width": 1280, "height": 900})
            monitor.locator("#continueAnalysis").click()
            expect(monitor.locator("#resultLink")).to_be_visible(timeout=15000)
            expect(monitor.locator("#jobStatus")).to_have_text("已完成")
            assert monitor.url == original
            monitor.locator("#resultLink").click()
            expect(monitor.locator("#content")).to_contain_text("返回一个常量")
            assert not errors, errors
        finally:
            if identifier:
                monitor.request.post(f"http://127.0.0.1:8083/api/jobs/{identifier}/stop")
            browser.close()
    print("Browser stop passed: auto-refresh running → stop → stopping → stopped → refresh/mobile → continue same task → completed")


if __name__ == "__main__":
    main()
