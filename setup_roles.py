import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freshfizz_erp.settings')
django.setup()

from accounts.models import User

roles = ['admin', 'hr', 'store', 'production', 'quality', 'sales', 'finance']

for role in roles:
    username = f"{role}_user"
    if not User.objects.filter(username=username).exists():
        User.objects.create_user(
            username=username,
            password='password123',
            email=f'{role}@example.com',
            role=role,
            is_staff=True if role == 'admin' else False,
            is_superuser=True if role == 'admin' else False
        )
        print(f"Created user: {username}")
    else:
        print(f"User already exists: {username}")
