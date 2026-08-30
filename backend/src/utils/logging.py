import logging
import os
import sys

import structlog


def configure_logger():
    # ... (existing config) ...
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.INFO,
    )

    # Read directly from the environment rather than `src.config.settings`:
    # `config.py` imports `logger` from this module at import time, so
    # importing `settings` back here would be circular.
    prod = os.environ.get("ENV", "").strip().lower() == "prod"

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # ConsoleRenderer lives in `structlog.dev`, not `structlog.processors`.
            # JSON is forced in prod regardless of tty -- a centralized log
            # ingestor (Datadog, Loki, CloudWatch) needs structured lines even
            # when the process happens to have a tty attached (e.g. `docker run
            # -it`). Outside prod, a real terminal still gets the readable
            # renderer; every automated run (CI, anything redirected to a file)
            # is not a tty and takes the JSON branch either way.
            structlog.processors.JSONRenderer() if prod or not sys.stdout.isatty() else structlog.dev.ConsoleRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


logger = structlog.get_logger()
