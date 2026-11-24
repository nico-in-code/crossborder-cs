import asyncio
import sys

# psycopg async requires a selector loop on Windows; Proactor is the default.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    # Uvicorn 0.36+ passes an explicit Proactor factory on Windows and ignores
    # the event loop policy, so the server must reference this factory directly.
    return asyncio.SelectorEventLoop()