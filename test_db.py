import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freshfizz_erp.settings')
django.setup()

from inventory.models import Item
from accounts.models import User
try:
    print(f"Items count: {Item.objects.count()}")
    print(f"Users count: {User.objects.count()}")
    print("Database connection OK.")
except Exception as e:
    print(f"Database error: {e}")
