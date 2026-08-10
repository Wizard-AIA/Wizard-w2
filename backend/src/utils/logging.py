import inspect
import logging
import os
import sys
import time
from functools import wraps

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


def trace_agent(agent_name: str):
    """Decorator to trace agent execution time and outcomes. Supports both sync and async functions."""

    def decorator(func):
        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                start_time = time.time()
                logger.info(f"Agent Started: {agent_name}", status="started")
                try:
                    result = await func(*args, **kwargs)
                    duration = time.time() - start_time
                    logger.info(f"Agent Finished: {agent_name}", status="success", duration_sec=round(duration, 3))
                    return result
                except Exception as e:
                    duration = time.time() - start_time
                    logger.error(
                        f"Agent Failed: {agent_name}", status="error", error=str(e), duration_sec=round(duration, 3)
                    )
                    raise

            return async_wrapper
        else:

            @wraps(func)
            def wrapper(*args, **kwargs):
                start_time = time.time()
                logger.info(f"Agent Started: {agent_name}", status="started")
                try:
                    result = func(*args, **kwargs)
                    duration = time.time() - start_time
                    logger.info(f"Agent Finished: {agent_name}", status="success", duration_sec=round(duration, 3))
                    return result
                except Exception as e:
                    duration = time.time() - start_time
                    logger.error(
                        f"Agent Failed: {agent_name}", status="error", error=str(e), duration_sec=round(duration, 3)
                    )
                    raise

            return wrapper

    return decorator
