import asyncio

from providers.startup import initialize_backend_services
from utils.other.jobs import start_job
import logging

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)

initialize_backend_services()

logger.info('Starting job...')
asyncio.run(start_job())
