from __future__ import annotations

import logging

import structlog


def configure_logging() -> None:
    """Configure structlog and stdlib logging."""
    timestamper = structlog.processors.TimeStamper(fmt="ISO")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            timestamper,
            structlog.stdlib.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    logging.basicConfig(level=logging.INFO)


logger = structlog.get_logger("finance_manager")


