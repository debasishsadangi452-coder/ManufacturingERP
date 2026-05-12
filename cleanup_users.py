import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freshfizz_erp.settings')
django.setup()

from django.contrib.auth import get_user_model
from workforce.models import Employee

User = get_user_model()

def cleanup_users():
    print("Deleting all employees and non-superuser accounts...")
    
    # We should probably keep at least one admin to avoid locking ourselves out, 
    # but the user said "delete users". Usually superusers are safe to keep.
    
    employees_deleted = Employee.objects.all().delete()
    print(f"Deleted {employees_deleted[0]} employee records.")
    
    users_to_delete = User.objects.filter(is_superuser=False)
    count = users_to_delete.count()
    users_to_delete.delete()
    print(f"Deleted {count} user accounts.")
    
    print("Cleanup complete. You can now add users manually.")

if __name__ == "__main__":
    cleanup_users()
