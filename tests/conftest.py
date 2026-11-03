import os

# database.py raises at import time if these are missing, and `import main` imports database.
# Dummy values are enough: tests never hit the network (everything external is faked).
os.environ.setdefault("SUPABASE_URL", "https://dummy-project.supabase.co")
os.environ.setdefault("SUPABASE_PUBLISHABLE_KEY", "dummy-publishable-key")
os.environ.setdefault("SUPABASE_SECRET_KEY", "dummy-secret-key")
