import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freshfizz_erp.settings')
django.setup()

from workforce.models import LeaveType, Shift
from datetime import time

def fix_constraints():
    print("Resetting Leave Types...")
    LeaveType.objects.all().delete()
    LeaveType.objects.create(name="Sick Leave", code="SICK", annual_quota=10)
    LeaveType.objects.create(name="Non-Sick Leave", code="NSK", annual_quota=10)
    
    print("Configuring Shift Timings...")
    # Clean up existing shifts to ensure unique timings
    Shift.objects.all().delete()
    
    # Morning: 06:00 - 14:00
    Shift.objects.create(
        name='Morning Shift', 
        shift_type='morning', 
        start_time=time(6, 0), 
        end_time=time(14, 0),
        capacity=20
    )
    # Afternoon: 14:00 - 22:00
    Shift.objects.create(
        name='Afternoon Shift', 
        shift_type='afternoon', 
        start_time=time(14, 0), 
        end_time=time(22, 0),
        capacity=20
    )
    # Night: 22:00 - 06:00
    Shift.objects.create(
        name='Night Shift', 
        shift_type='night', 
        start_time=time(22, 0), 
        end_time=time(6, 0),
        capacity=20
    )
    print("Workforce constraints applied successfully.")

if __name__ == "__main__":
    fix_constraints()
