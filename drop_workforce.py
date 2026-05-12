import django
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freshfizz_erp.settings')
django.setup()

from django.db import connection

tables = [
    'workforce_trainingprogram_enrolled_employees',
    'workforce_trainingprogram',
    'workforce_shift',
    'workforce_employee',
]

with connection.cursor() as cursor:
    for t in tables:
        try:
            cursor.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE')
            print(f'Dropped: {t}')
        except Exception as e:
            print(f'Error dropping {t}: {e}')

print('Done!')
