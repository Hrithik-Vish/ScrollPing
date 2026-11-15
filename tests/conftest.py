import os

# database.py raises at import time if these are missing, and `import main` imports database.
# Dummy values are enough: tests never hit the network (everything external is faked).
os.environ.setdefault("SUPABASE_URL", "https://dummy-project.supabase.co")
os.environ.setdefault("SUPABASE_PUBLISHABLE_KEY", "dummy-publishable-key")
os.environ.setdefault("SUPABASE_SECRET_KEY", "dummy-secret-key")
os.environ.setdefault("PTB_TIMEDELTA", "1")
# A real ADMIN_TELEGRAM_USER_IDS in a developer's .env must never make a test send an admin message.
# (load_dotenv does not override a variable that is already set, even to an empty string.)
os.environ.setdefault("ADMIN_TELEGRAM_USER_IDS", "")
