import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freshfizz_erp.settings')
django.setup()

from finance.models import DepartmentBudget, ExpenseRequest, OperationalCost, PayrollRecord, FinancialSummary

e = ExpenseRequest.objects.all().delete()
o = OperationalCost.objects.all().delete()
p = PayrollRecord.objects.all().delete()
f = FinancialSummary.objects.all().delete()
b = DepartmentBudget.objects.all().delete()

print("=== Finance DB Cleared ===")
print(f"  Expenses deleted    : {e[0]}")
print(f"  Operational Costs   : {o[0]}")
print(f"  Payroll Records     : {p[0]}")
print(f"  Financial Summaries : {f[0]}")
print(f"  Budgets deleted     : {b[0]}")
print("Finance is now EMPTY. Ready for fresh testing.")
