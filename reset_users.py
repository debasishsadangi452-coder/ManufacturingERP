import os
import django

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freshfizz_erp.settings')
django.setup()

from accounts.models import User

def reset_users():
    admin_username = 'admin'
    admin_password = 'admin123'
    
    print(f"--- User Cleanup Mission ---")
    
    # 1. Create or Update Admin
    user, created = User.objects.get_or_create(username=admin_username)
    user.set_password(admin_password)
    user.role = 'admin'
    user.is_staff = True
    user.is_superuser = True
    user.save()
    
    if created:
        print(f"✅ Created new admin: {admin_username}")
    else:
        print(f"🔄 Updated existing admin: {admin_username}")
        
    # 2. Delete all others
    others = User.objects.exclude(username=admin_username)
    count = others.count()
    others.delete()
    
    print(f"🗑️ Deleted {count} other users.")
    print(f"--- Mission Complete ---")
    print(f"CREDENTIALS: {admin_username} / {admin_password}")

if __name__ == "__main__":
    reset_users()
