import os
import django
from django.urls import resolve, Resolver404

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freshfizz_erp.settings')
django.setup()

paths_to_test = [
    '/api/ai/insights/',
    '/api/ai/chat/',
    '/api/finance/',
]

for path in paths_to_test:
    try:
        match = resolve(path)
        print(f"Path '{path}' resolved to: {match.view_name}")
    except Resolver404:
        print(f"Path '{path}' failed to resolve.")
    except Exception as e:
        print(f"Error testing path '{path}': {e}")
