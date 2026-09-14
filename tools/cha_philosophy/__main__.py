import os

from .cli import main

try:
    raise SystemExit(main())
except KeyboardInterrupt:
    # main() closes its connection and refresh() propagates the interrupt.
    # Running provider threads cannot be cancelled by Future.cancel(). Avoid
    # Python's executor atexit join at this process boundary. SQLite recovers
    # any interrupted transaction; previously committed checkpoints remain.
    # Library callers still receive KeyboardInterrupt and own their lifecycle.
    os._exit(130)
