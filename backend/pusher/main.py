import logging
import os

logging.basicConfig(level=logging.INFO)

from fastapi import FastAPI

from providers.startup import initialize_backend_services
from routers import pusher, metrics
from utils.http_client import close_all_clients

initialize_backend_services()

app = FastAPI()
app.include_router(pusher.router)
app.include_router(metrics.router)

paths = ['_temp', '_samples', '_segments', '_speech_profiles']
for path in paths:
    if not os.path.exists(path):
        os.makedirs(path)


@app.on_event("shutdown")
async def shutdown_event():
    await close_all_clients()


@app.get('/health')
async def health_check():
    return {"status": "healthy"}
