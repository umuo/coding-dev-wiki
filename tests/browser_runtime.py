"""先启动 python -m tests.native_server；完整生产入口 + 真实运行时的浏览器验收。"""
import io
import zipfile
from playwright.sync_api import sync_playwright, expect


def main():
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as bundle:
        bundle.writestr("main.py", "def run():\n    return 42\n")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://127.0.0.1:8085")
        page.get_by_role("button", name="上传 ZIP", exact=True).click()
        page.locator("#zipFile").set_input_files({"name": "native.zip", "mimeType": "application/zip", "buffer": content.getvalue()})
        page.locator("#mode").select_option("agent")
        page.locator("#submit").click()
        expect(page.locator("#logs")).to_contain_text("阅读 main.py:1-2", timeout=30000)
        expect(page.locator("#jobStatus")).to_have_text("正在运行")
        original = page.url
        page.locator("#stopAnalysis").click()
        expect(page.locator("#jobStatus")).to_have_text("已停止", timeout=15000)
        page.reload()
        page.locator("#continueAnalysis").click()
        expect(page.locator("#resultLink")).to_be_visible(timeout=30000)
        assert page.url == original
        expect(page.locator("#logs")).to_contain_text("恢复 OpenCode 持久会话")
        page.locator("#resultLink").click()
        expect(page.locator("#content")).to_contain_text("返回一个常量")
        assert not errors, errors
        browser.close()
    print("Native runtime browser passed: ZIP → actual OpenCode read → stop → reload → same native session → result")


if __name__ == "__main__":
    main()
