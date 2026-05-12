
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freshfizz_erp.settings')
django.setup()

from ai_assistant.tools import get_inventory_summary
from accounts.models import User

user = User.objects.get(username="admin_user")
print("Calling with positional...")
try:
    print(get_inventory_summary(user))
except Exception as e:
    print(f"Positional failed: {e}")

print("Calling with keyword...")
try:
    print(get_inventory_summary(user=user))
except Exception as e:
    print(f"Keyword failed: {e}")

print("Calling with keyword + empty dict...")
args = {}
try:
    print(get_inventory_summary(user=user, **args))
except Exception as e:
    print(f"Keyword+dict failed: {e}")
