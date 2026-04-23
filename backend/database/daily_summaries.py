from database._dispatch import load_backend_module

load_backend_module(globals(), "database.firebase.daily_summaries")
