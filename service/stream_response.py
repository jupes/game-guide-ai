"""Keep an admitted SSE lease owned until its response body takes over."""

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager

import anyio
import psycopg

log = logging.getLogger(__name__)


@asynccontextmanager
async def leased_body(
    frames: AsyncGenerator[bytes], release: Callable[[], Awaitable[None]], *, release_warning: str,
) -> AsyncGenerator[AsyncGenerator[bytes]]:
    started = False

    async def body() -> AsyncGenerator[bytes]:
        nonlocal started
        started = True
        async for frame in frames:
            yield frame

    async with aclosing(body()) as events:
        try:
            yield events
        finally:
            # A disconnect can precede response headers. Until iteration starts
            # the stream generator cannot run its own lease-release finalizer.
            with anyio.CancelScope(shield=True):
                await frames.aclose()
                if not started:
                    with anyio.move_on_after(2):
                        try:
                            await release()
                        except psycopg.Error:
                            log.warning(release_warning)
