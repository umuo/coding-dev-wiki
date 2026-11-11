"""对真实服务进行 ZIP 导入和桌面 / 窄屏交互验证，不 mock 浏览器 API。"""
import io
import os
import tempfile
import zipfile
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def main():
    output = Path("test-results")
    output.mkdir(exist_ok=True)
    base = os.getenv("WIKI_TEST_URL", "http://127.0.0.1:8080")
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as bundle:
        bundle.writestr("demo/main.py", '"""Sample entry."""\nfrom tasks import create_task\n\ndef submit(title):\n    return create_task(title)\n')
        bundle.writestr("demo/tasks.py", 'tasks = []\n\ndef create_task(title):\n    tasks.append(title)\n    return tasks\n')
        bundle.writestr("demo/README.md", "# Demo task service\nCreate tasks from the entry function.\n<script>window.injected=true</script>")
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.getenv("WIKI_BROWSER_EXECUTABLE", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base)
        page.screenshot(path=str(output / "home.png"), full_page=True)
        page.get_by_role("button", name="上传 ZIP", exact=True).click()
        page.locator('#zipFile').set_input_files({"name": "demo.zip", "mimeType": "application/zip", "buffer": content.getvalue()})
        page.locator('#mode').select_option("preview")
        page.locator('#submit').click()
        expect(page.locator('#resultLink')).to_be_visible(timeout=30000)
        assert "分析完成" in page.locator('#logs').inner_text()
        job_url = page.url
        page.reload()
        expect(page.locator('#resultLink')).to_be_visible()
        page.screenshot(path=str(output / "progress.png"), full_page=True)
        page.locator('#resultLink').click()
        expect(page.locator('#content h1')).to_have_text("demo")
        expect(page.locator('.arch-map')).to_be_visible()
        page.screenshot(path=str(output / "overview.png"), full_page=True)
        page.locator('#navigation .nav-item').filter(has_text='main.py').first.click()
        expect(page.locator('.step-card')).to_have_count(1)
        expect(page.locator('.code-row')).not_to_have_count(0)
        page.get_by_role('button', name='聚焦此函数').click()
        expect(page.locator('[data-mode="focus"]')).to_have_class('active')
        page.locator('[data-mode="full"]').click()
        page.locator('#jumpInput').fill('4')
        page.locator('#jumpBtn').click()
        assert 'start=4' in page.url
        page.locator('#findInput').fill('return')
        expect(page.locator('#findCount')).to_have_text('1 行匹配')
        page.locator('#searchOpen').click()
        page.locator('#searchMode').select_option('grep')
        page.locator('#searchInput').fill('create_task')
        expect(page.locator('.search-row').first).to_be_visible()
        page.locator('.search-row').first.click()
        page.screenshot(path=str(output / "source.png"), full_page=True)
        linked = page.url
        page.reload()
        expect(page.locator('.code-row')).not_to_have_count(0)
        assert page.url == linked
        page.locator('#chatOpen').click()
        page.locator('#question').fill('这个项目是什么？')
        page.locator('#ask').click()
        expect(page.locator('.message').last).to_contain_text('尚未配置 LLM', timeout=10000)
        page.locator('#chatClose').click()
        assert page.evaluate('window.injected') is None
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator('#menuToggle').click()
        expect(page.locator('.leftbar')).to_be_visible()
        page.locator('[data-tab="files"]').click()
        page.locator('#navigation .nav-item').first.click()
        expect(page.locator('.leftbar')).not_to_be_visible()
        page.screenshot(path=str(output / "mobile.png"), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors, errors
        if os.getenv("WIKI_TEST_DEVELOPER") == "1":
            page.goto(base)
            expect(page.locator('#developerImport')).to_be_visible()
            page.locator('#developerImport summary').click()
            with tempfile.TemporaryDirectory(prefix="wiki-local-project-") as folder:
                source = Path(folder) / 'main.py'
                original = 'def local_entry():\n    return "local repository"\n'
                source.write_text(original)
                page.locator('#mode').select_option('preview')
                page.locator('#localPath').fill(folder)
                page.locator('#localSubmit').click()
                expect(page.locator('#resultLink')).to_be_visible(timeout=30000)
                assert '已连接本地项目目录' in page.locator('#logs').inner_text()
                page.locator('#resultLink').click()
                expect(page.locator('#navigation')).to_contain_text('main.py')
                assert source.read_text() == original
                page.screenshot(path=str(output / "local-import.png"), full_page=True)
        assert not errors, errors
        browser.close()
    print("Browser smoke passed: ZIP → SSE → result → chain/focus/full → search → deep link → chat error → mobile")
    print("Job:", job_url)


if __name__ == '__main__':
    main()
