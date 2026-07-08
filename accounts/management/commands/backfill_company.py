"""Assign existing demo rows to their companies (one-time backfill after the
multi-tenancy migration). Name lists come straight from seed_demo_companies.py.
Rows matching neither company stay company=NULL and become invisible to all
tenants (legacy test data)."""
from django.core.management.base import BaseCommand

from accounts.models import Company


class Command(BaseCommand):
    help = "Backfill company FKs on business data from the demo seed specs."

    def handle(self, *args, **options):
        import seed_demo_companies as seeds  # project root

        from inventory.models import Item, Warehouse
        from procurement.models import Vendor
        from sales.models import Customer
        from production.models import ProductionLine
        from logistics.models import Driver, Vehicle, DeliveryRoute, Shipment
        from workforce.models import Department, Employee, Shift
        from finance.models import DepartmentBudget, ExpenseRequest, OperationalCost

        for spec in (seeds.APEXFORGE, seeds.PURESWEET):
            company = Company.objects.filter(slug=spec["slug"]).first()
            if not company:
                self.stdout.write(f"skip {spec['slug']}: company not found")
                continue
            n = {}

            n["items"] = Item.objects.filter(
                name__in=[i[0] for i in spec["items"]], company__isnull=True
            ).update(company=company)
            n["warehouses"] = Warehouse.objects.filter(
                name__in=[w[0] for w in spec["warehouses"]], company__isnull=True
            ).update(company=company)
            n["vendors"] = Vendor.objects.filter(
                name__in=[v["name"] for v in spec["vendors"]], company__isnull=True
            ).update(company=company)
            n["customers"] = Customer.objects.filter(
                name__in=[cu["name"] for cu in spec["customers"]], company__isnull=True
            ).update(company=company)
            n["lines"] = ProductionLine.objects.filter(
                name__in=[l[0] for l in spec["lines"]], company__isnull=True
            ).update(company=company)
            n["drivers"] = Driver.objects.filter(
                license_number__in=[d["license_number"] for d in spec.get("drivers", [])],
                company__isnull=True,
            ).update(company=company)
            n["vehicles"] = Vehicle.objects.filter(
                name__in=[v["name"] for v in spec.get("vehicles", [])], company__isnull=True
            ).update(company=company)
            n["routes"] = DeliveryRoute.objects.filter(
                name__in=[r[0] for r in spec.get("routes", [])], company__isnull=True
            ).update(company=company)
            n["shipments"] = Shipment.objects.filter(
                customer__in=[s[0] for s in spec.get("shipments", [])], company__isnull=True
            ).update(company=company)
            n["employees"] = Employee.objects.filter(
                email__in=[e["email"] for e in spec.get("employees", [])], company__isnull=True
            ).update(company=company)
            n["expenses"] = ExpenseRequest.objects.filter(
                title__in=[e[0] for e in spec.get("expenses", [])], company__isnull=True
            ).update(company=company)
            n["op_costs"] = OperationalCost.objects.filter(
                title__in=[o[0] for o in spec.get("operational_costs", [])], company__isnull=True
            ).update(company=company)
            # Budgets match on (department, label, amount) to split shared labels
            bn = 0
            for dept, _period, label, total, _lim in spec.get("budgets", []):
                bn += DepartmentBudget.objects.filter(
                    department=dept, period_label=label, total_budget=total,
                    company__isnull=True,
                ).update(company=company)
            n["budgets"] = bn

            self.stdout.write(f"{company.name}: " + ", ".join(f"{k}={v}" for k, v in n.items()))

        # Shared Department/Shift rows go to ApexForge; PureSweet gets its own
        # copies on the next seeder run. Re-point PureSweet employees after.
        apex = Company.objects.filter(slug=seeds.APEXFORGE["slug"]).first()
        pure = Company.objects.filter(slug=seeds.PURESWEET["slug"]).first()
        if apex:
            d = Department.objects.filter(company__isnull=True,
                name__in=[x[0] for x in seeds.APEXFORGE["departments"]]).update(company=apex)
            s = Shift.objects.filter(company__isnull=True,
                name__in=[x[0] for x in seeds.APEXFORGE["shifts"]]).update(company=apex)
            self.stdout.write(f"shared departments->{apex.name}: {d}, shifts: {s}")
        if apex and pure:
            for name, code in seeds.PURESWEET["departments"]:
                dept, _ = Department.objects.get_or_create(
                    company=pure, name=name, defaults={"code": code})
                Employee.objects.filter(company=pure, department__company=apex,
                                        department__name=name).update(department=dept)
            for name, stype, start, end, cap in seeds.PURESWEET["shifts"]:
                Shift.objects.get_or_create(company=pure, name=name, defaults={
                    "shift_type": stype, "start_time": start, "end_time": end,
                    "capacity": cap})
            self.stdout.write(f"PureSweet departments/shifts cloned and employees re-pointed")
        self.stdout.write(self.style.SUCCESS("Backfill complete."))
