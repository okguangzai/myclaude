"""Managed server entrypoint, including a graceful local shutdown callback."""

import asyncio

import uvicorn
from app.main import app


async def main():
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=8000, access_log=False, timeout_graceful_shutdown=5
        )
    )
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
