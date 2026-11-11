"""本地开发启动器：固定绑定回环地址，生产镜像排除此文件。"""
import argparse
import os
from pathlib import Path
from dotenv import load_dotenv
import uvicorn


ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def main():
    parser = argparse.ArgumentParser(description="启动仅本机可访问的开发工作台")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 到 65535 之间")
    # 显式导出的环境变量优先；配置仅从本项目根目录读取。
    load_dotenv(ENV_FILE, override=False)
    os.environ["WIKI_ENV"] = "development"
    os.environ["WIKI_DEV_MODE"] = "1"
    uvicorn.run("app.api:app", host="127.0.0.1", port=args.port, proxy_headers=False)


if __name__ == "__main__":
    main()
