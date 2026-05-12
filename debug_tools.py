
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freshfizz_erp.settings')
django.setup()

from ai_assistant.tools import *
from ai_assistant.views import get_erp_state_summary

print("Testing get_erp_state_summary...")
try:
    print(get_erp_state_summary())
except Exception as e:
    import traceback
    traceback.print_exc()

print("Testing TOOL_MAP...")
for name, func in TOOL_MAP.items():
    print(f"Checking {name} imports/setup...")
    # No need to call, just check if it fails on setup
