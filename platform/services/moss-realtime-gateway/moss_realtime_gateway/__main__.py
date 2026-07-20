from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run("moss_realtime_gateway.app:app", host="127.0.0.1", port=8890, log_level="info")


if __name__ == "__main__":
    main()
