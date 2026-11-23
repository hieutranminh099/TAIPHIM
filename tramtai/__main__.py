import argparse
import threading
import webbrowser

import uvicorn

from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Trạm Tải Tập Phim")
    parser.add_argument("--no-browser", action="store_true", help="Chạy ứng dụng và mở trình duyệt thủ công.")
    parser.add_argument("--port", type=int, default=8765, help="Cổng HTTP cục bộ (mặc định là 8765).")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Cổng phải nằm trong khoảng 1–65535.")
    host, port = "127.0.0.1", args.port
    url = f"http://{host}:{port}/"

    class LocalServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started and not args.no_browser:
                threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

    config = uvicorn.Config(create_app(), host=host, port=port, proxy_headers=False, access_log=False)
    LocalServer(config).run()


if __name__ == "__main__":
    main()
