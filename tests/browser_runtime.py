"""先启动 python -m tests.native_server；完整生产入口 + 真实运行时的浏览器验收。"""
import io
import os
import zipfile
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def main():
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as bundle:
        bundle.writestr("main.py", "def run():\n    return 42\n")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.getenv("WIKI_BROWSER_EXECUTABLE", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"), headless=True)
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
        expect(page.locator('#runtimeActions')).to_have_count(0)
        expect(page.locator('#logs')).to_be_visible()
        expect(page.locator('#logs [data-kind="tool"][data-status="completed"]')).to_contain_text('读取源码')
        expect(page.locator('#logs [data-kind="model"][data-status="running"]')).not_to_have_count(0)
        original = page.url
        page.reload()
        expect(page.locator('#logs [data-kind="tool"][data-status="completed"]')).to_contain_text('main.py')
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert 220 <= page.locator('#logs').bounding_box()['height'] <= 360
        Path('test-results').mkdir(exist_ok=True)
        page.screenshot(path='test-results/runtime-actions-mobile.png', full_page=True)
        page.locator("#stopAnalysis").click()
        expect(page.locator("#jobStatus")).to_have_text("已停止", timeout=15000)
        page.reload()
        page.locator("#continueAnalysis").click()
        expect(page.locator("#resultLink")).to_be_visible(timeout=30000)
        assert page.url == original
        expect(page.locator("#logs")).to_contain_text("恢复 OpenCode 持久会话")
        page.locator("#resultLink").click()
        expect(page.locator("#content")).to_contain_text("返回一个常量")
        # 同一生产入口验证真实超时在原生会话自动恢复，并在等待阶段展示动作。
        page.request.post('http://127.0.0.1:8085/test/timeout-next')
        page.goto('http://127.0.0.1:8085')
        page.get_by_role('button', name='上传 ZIP', exact=True).click()
        page.locator('#zipFile').set_input_files({"name": "timeout.zip", "mimeType": "application/zip", "buffer": content.getvalue()})
        page.locator('#mode').select_option('agent')
        page.locator('#submit').click()
        expect(page.locator('#logs [data-kind="retry"][data-status="waiting"]')).to_contain_text('1/2', timeout=30000)
        page.screenshot(path='test-results/runtime-retry-mobile.png', full_page=True)
        expect(page.locator('#resultLink')).to_be_visible(timeout=30000)
        expect(page.locator('#logs [data-kind="retry"][data-status="completed"]')).to_contain_text('自动重试成功')
        page.reload()
        expect(page.locator('#logs [data-kind="retry"][data-status="completed"]')).to_contain_text('自动重试成功')
        page.evaluate("""async () => {
            const {renderAction} = await import('/assets/logs.js');
            renderAction({action:{id:'untrusted',kind:'tool',status:'completed',title:'<img src=x onerror="window.injected=true">',detail:'<script>bad</script>'}});
        }""")
        assert page.locator('#logs img, #logs script').count() == 0
        # 历史日志浏览不被新动作强行拉回底部，恢复到底部后继续跟随。
        page.evaluate("""async () => {
            const {renderProgress, renderAction} = await import('/assets/logs.js');
            for (let i = 0; i < 80; i++) {
                renderProgress({seq:10000+i,stage:'验收',message:'历史日志 '+i});
            }
            const logs = document.querySelector('#logs');
            logs.scrollTop = 0;
            renderAction({seq:11000,action:{id:'new',kind:'tool',status:'completed',title:'新动作'}});
            if (logs.scrollTop !== 0) {
                throw new Error('新日志打断了历史阅读');
            }
            logs.scrollTop = logs.scrollHeight;
            renderProgress({seq:11001,stage:'验收',message:'跟随新日志'});
            if (logs.scrollHeight-logs.scrollTop-logs.clientHeight > 2) {
                throw new Error('日志没有跟随底部');
            }
            const count = logs.childElementCount;
            renderProgress({seq:11001,stage:'验收',message:'重复事件'});
            if (count !== logs.childElementCount) {
                throw new Error('重复事件未去重');
            }
        }""")
        assert page.evaluate('window.injected') is None
        assert not errors, errors
        browser.close()
    print("Native runtime browser passed: ZIP → actual OpenCode read → stop → reload → same native session → result")


if __name__ == "__main__":
    main()
