from database._dispatch import load_backend_module

load_backend_module(globals(), "database.firebase.chat", "database.supabase.chat")
