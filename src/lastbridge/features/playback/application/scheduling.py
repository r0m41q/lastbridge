import asyncio


class AudioScheduler:
    """Two acquisitions, at most one background job, foreground first."""

    def __init__(self):
        self.waiters = {}
        self.active = {}
        self.closed = False

    def submit(self, key, priority):
        self.waiters[key] = (priority, asyncio.Event())
        self.dispatch()

    def promote(self, key, priority):
        if key in self.waiters:
            previous, event = self.waiters[key]
            self.waiters[key] = (min(previous, priority), event)
        elif key in self.active:
            self.active[key] = min(self.active[key], priority)
        self.dispatch()

    async def wait(self, key):
        if key in self.waiters:
            await self.waiters[key][1].wait()

    def finish(self, key):
        self.waiters.pop(key, None)
        self.active.pop(key, None)
        self.dispatch()

    def dispatch(self):
        while not self.closed and len(self.active) < 2 and self.waiters:
            key = min(self.waiters, key=lambda k: self.waiters[k][0])
            priority, event = self.waiters[key]
            if priority and any(p > 0 for p in self.active.values()):
                break
            del self.waiters[key]
            self.active[key] = priority
            event.set()
