import asyncio


class JitterBuffer:
    """Bounded, self-correcting buffer that decouples the real-time audio
    capture thread from the asyncio send loop.

    This is NOT a network jitter buffer: there is a single in-process
    producer handing chunks off in order via call_soon_threadsafe, so there
    is nothing to reorder and no packet-loss concealment to do. Its only job
    is to absorb scheduling jitter (GC pauses, event-loop delays, brief
    websocket backpressure) with a small bounded queue, dropping the oldest
    chunk on overflow so total lag never grows unbounded.
    """

    def __init__(self, maxsize: int = 10, loop: asyncio.AbstractEventLoop | None = None):
        self._loop = loop or asyncio.get_running_loop()
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=maxsize)
        self._dropped_count = 0

    def put_from_thread(self, chunk: bytes | None) -> None:
        """The only method safe to call from the audio callback thread."""
        self._loop.call_soon_threadsafe(self._enqueue, chunk)

    def _enqueue(self, chunk: bytes | None) -> None:
        if self._queue.full():
            self._queue.get_nowait()
            self._dropped_count += 1
        self._queue.put_nowait(chunk)

    async def get(self) -> bytes | None:
        return await self._queue.get()

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    @property
    def dropped_count(self) -> int:
        return self._dropped_count
