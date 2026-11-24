from __future__ import annotations

import uvicorn

from app.main import app


def main() -> None:
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        log_level="info",
        loop="app:selector_loop_factory",
    )


if __name__ == "__main__":
    main()