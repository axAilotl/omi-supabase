import logging

logger = logging.getLogger(__name__)


def initialize_backend_services() -> None:
    logger.info("Supabase backend mode active; no legacy backend initialization required")
