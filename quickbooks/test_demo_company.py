"""The aluminium welding-wire demo company must be QuickBooks-ready: seeded with
the same management command used to build it, it has to push to QuickBooks
without a single sync error, and it has to sit in the state the QuickBooks setup
wizard expects for a company whose items and BOMs were created in the ERP."""
import json
from datetime import timedelta
from io import StringIO
from unittest import mock
from urllib.parse import urlsplit

from django.core.management import CommandError, call_command
from rest_framework.test import APIClient
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import Company
from inventory.models import BOM, Item, QuickBooksOnboarding, SalesQuickBooksConfig, ProcurementQuickBooksConfig
from procurement.models import Bill, PurchaseOrder, Vendor
from sales.models import Customer, CustomerPayment, Invoice, SalesOrder

from . import push
from .models import QuickBooksConnection, QuickBooksSyncError
from .services import QuickBooksAPIError
from .test_main_books import ChartFakeQuickBooks
from .tests import TEST_QB_CONFIG


class StrictQuickBooks(ChartFakeQuickBooks):
    """Enforces the QuickBooks Online rules a demo company can trip over.

    * A Customer / Vendor display name is unique across *both* lists.
    * Item names are unique, at most 100 characters, and contain no colon.
    * An order, invoice or bill must have at least one line.
    """

    def __init__(self):
        super().__init__()
        self.party_names = {}   # lower-cased display name -> (resource, id)
        self.item_names = {}

    def __call__(self, request):
        parts = urlsplit(request.full_url)
        if request.data is not None and not parts.path.endswith("/query"):
            resource = parts.path.rstrip("/").split("/")[-1]
            body = json.loads(request.data.decode("utf-8"))
            if resource in ("customer", "vendor"):
                self._claim(self.party_names, body.get("DisplayName", ""), resource, body)
            elif resource == "item":
                name = body.get("Name", "")
                if ":" in name or len(name) > 100:
                    raise QuickBooksAPIError(f"Invalid item name: {name!r}")
                self._claim(self.item_names, name, resource, body)
            elif resource in ("invoice", "bill", "purchaseorder", "estimate") and not body.get("Line"):
                raise QuickBooksAPIError(f"{resource} has no lines")
        return super().__call__(request)

    @staticmethod
    def _claim(registry, name, resource, body):
        key = name.strip().lower()
        owner = registry.get(key)
        if owner and not (body.get("Id") and owner[1] == body["Id"]):
            raise QuickBooksAPIError(f"Duplicate Name Exists Error: {name!r}")
        registry[key] = (resource, body.get("Id"))


# Seeding creates six users; a fast hasher keeps the tests quick (the real one is deliberately slow).
@override_settings(QUICKBOOKS_CONFIG=TEST_QB_CONFIG,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class WeldingDemoQuickBooksTests(TestCase):
    SLUG = "weldco"

    def setUp(self):
        self.fake = StrictQuickBooks()
        patcher = mock.patch("quickbooks.services._open_json", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        push._local.__dict__.clear()
        self.out = StringIO()

    def seed(self, **extra):
        call_command("seed_welding_wire_demo", company="Weld Co", slug=self.SLUG, password="demo12345",
                     stdout=self.out, **extra)
        return Company.objects.get(slug=self.SLUG)

    def connect(self, company):
        connection = QuickBooksConnection.objects.create(
            company=company, realm_id="12345", environment="sandbox",
            access_token_expires_at=timezone.now() + timedelta(hours=1),
        )
        connection.set_access_token("token")
        connection.set_refresh_token("refresh")
        connection.save()
        return connection

    def test_whole_company_pushes_to_quickbooks_without_errors(self):
        company = self.seed()
        connection = self.connect(company)
        run = push.push_all(connection)
        errors = list(QuickBooksSyncError.objects.filter(company=company).values_list("entity_type", "message"))
        self.assertEqual(errors, [])
        self.assertEqual(run.status, "success")
        # Everything that belongs in QuickBooks got a QuickBooks id.
        for model in (Customer, Vendor, Item):
            self.assertFalse(model.objects.filter(company=company, quickbooks_id="").exists(), model.__name__)
        self.assertFalse(Invoice.objects.filter(company=company, quickbooks_id="").exists())
        self.assertFalse(Bill.objects.filter(company=company, quickbooks_id="").exists())
        self.assertFalse(CustomerPayment.objects.filter(company=company, quickbooks_id="").exists())
        self.assertFalse(SalesOrder.objects.filter(customer__company=company, quickbooks_id="")
                         .exclude(status="cancelled").exists())

    def test_send_only_customers_then_everything_else(self):
        """The setup wizard can send just the customers (or just the vendors) first."""
        company = self.seed()
        connection = self.connect(company)
        push.push_all(connection, only=["customer"])
        self.assertFalse(Customer.objects.filter(company=company, quickbooks_id="").exists())
        # Nothing else was sent.
        self.assertTrue(Vendor.objects.filter(company=company, quickbooks_id="").exists())
        self.assertTrue(Item.objects.filter(company=company, quickbooks_id="").exists())
        self.assertFalse(Invoice.objects.filter(company=company).exclude(quickbooks_id="").exists())
        # A second run for everything finishes the job without re-sending the customers.
        sent_before = len([1 for resource, _ in self.fake.requests if resource == "customer"])
        run = push.push_all(connection)
        self.assertEqual(run.status, "success")
        self.assertEqual(len([1 for resource, _ in self.fake.requests if resource == "customer"]), sent_before)
        self.assertFalse(Vendor.objects.filter(company=company, quickbooks_id="").exists())
        self.assertFalse(Invoice.objects.filter(company=company, quickbooks_id="").exists())

    @override_settings(QUICKBOOKS_BACKGROUND_JOBS=False)
    def test_push_all_endpoint_accepts_and_validates_entity_types(self):
        company = self.seed()
        self.connect(company)
        admin = company.users.get(role="admin")
        client = APIClient()
        client.force_authenticate(admin)
        for bad in ([], "customer", ["customer", "bogus"]):
            res = client.post("/api/quickbooks/push-all/", {"entity_types": bad}, format="json")
            self.assertEqual(res.status_code, 400, bad)
        res = client.post("/api/quickbooks/push-all/", {"entity_types": ["vendor"]}, format="json")
        self.assertEqual(res.status_code, 202, res.data)
        self.assertEqual(res.data["status"], "success")
        self.assertFalse(Vendor.objects.filter(company=company, quickbooks_id="").exists())
        self.assertFalse(Customer.objects.filter(company=company).exclude(quickbooks_id="").exists())
        # No payload still means everything.
        res = client.post("/api/quickbooks/push-all/", {}, format="json")
        self.assertEqual(res.status_code, 202, res.data)
        self.assertFalse(Customer.objects.filter(company=company, quickbooks_id="").exists())

    def test_items_are_already_classified_for_the_setup_wizard(self):
        company = self.seed()
        items = Item.objects.filter(company=company)
        self.assertFalse(items.filter(erp_classification__isnull=True).exists())
        self.assertFalse(items.filter(classification_completed_at__isnull=True).exists())
        for item in items.filter(category="finished_good"):
            self.assertTrue(item.bom_completed, item.name)
            self.assertTrue(BOM.objects.filter(finished_good=item, is_active=True).exists())

    def test_quickbooks_setup_is_staged_for_the_wizard(self):
        company = self.seed()
        onboarding = QuickBooksOnboarding.objects.get(company=company)
        # Nothing to classify and every BOM exists, so the wizard starts at customer mapping.
        self.assertEqual(onboarding.status, "customer_mapping")
        sales = SalesQuickBooksConfig.objects.get(company=company)
        self.assertEqual((sales.sale_trigger, sales.default_doc_type), ("shipment", "invoice"))
        self.assertFalse(sales.customers_mapped)  # a person confirms the mapping
        procurement = ProcurementQuickBooksConfig.objects.get(company=company)
        self.assertEqual(procurement.purchase_trigger, "goods_receipt")
        self.assertFalse(procurement.vendors_mapped)

    def test_customers_and_vendors_are_complete_enough_for_quickbooks(self):
        company = self.seed()
        for party in list(Customer.objects.filter(company=company)) + list(Vendor.objects.filter(company=company)):
            self.assertTrue(party.email, party.name)
            self.assertTrue(party.phone, party.name)
            self.assertTrue(party.address, party.name)
        names = [p.name.lower() for p in Customer.objects.filter(company=company)] + \
                [p.name.lower() for p in Vendor.objects.filter(company=company)]
        self.assertEqual(len(names), len(set(names)))

    def test_each_product_has_a_routing_with_resources(self):
        from production.models import ManufacturingSettings, Recipe, Resource, RoutingStep
        company = self.seed()
        self.assertTrue(ManufacturingSettings.objects.filter(company=company).exists())
        self.assertTrue(Resource.objects.filter(company=company, resource_type="machine").exists())
        self.assertTrue(Resource.objects.filter(company=company, resource_type="manpower").exists())
        for recipe in Recipe.objects.filter(product__company=company):
            steps = list(RoutingStep.objects.filter(recipe=recipe))
            self.assertGreaterEqual(len(steps), 3, recipe.product.name)
            self.assertTrue(all(s.run_minutes_per_unit > 0 for s in steps))

    def test_open_orders_show_the_make_to_order_chain(self):
        from procurement.models import PurchaseRequisition
        from production.models import ProductionPlan
        company = self.seed()
        plan = ProductionPlan.objects.get(company=company)
        self.assertEqual(plan.status, "planned")
        self.assertTrue(PurchaseRequisition.objects.filter(company=company, production_plan=plan).exists())

    def test_reseeding_a_quickbooks_connected_company_is_refused(self):
        company = self.seed()
        self.connect(company)
        with self.assertRaises(CommandError) as ctx:
            self.seed(replace=True)
        self.assertIn("QuickBooks", str(ctx.exception))
        self.assertTrue(Company.objects.filter(pk=company.pk).exists())

    def test_reseeding_an_unconnected_company_still_works(self):
        first = self.seed()
        second = self.seed(replace=True)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(Company.objects.filter(slug=self.SLUG).count(), 1)


@override_settings(QUICKBOOKS_CONFIG=TEST_QB_CONFIG,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CookieDemoQuickBooksTests(TestCase):
    """The cookie bakery demo shares the welding command's set-up code; it must stay QuickBooks-ready too."""

    def setUp(self):
        self.fake = StrictQuickBooks()
        patcher = mock.patch("quickbooks.services._open_json", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        push._local.__dict__.clear()

    def test_cookie_company_seeds_and_pushes_clean(self):
        out = StringIO()
        call_command("seed_cookie_demo", company="Crumb Co", slug="crumbco", password="demo12345", stdout=out)
        company = Company.objects.get(slug="crumbco")
        self.assertIn("QuickBooks: not connected", out.getvalue())
        self.assertEqual(QuickBooksOnboarding.objects.get(company=company).status, "customer_mapping")
        self.assertFalse(Item.objects.filter(company=company, erp_classification__isnull=True).exists())
        for bom in BOM.objects.filter(finished_good__company=company, is_active=True):
            self.assertTrue(bom.finished_good.bom_completed, bom.finished_good.name)
        connection = QuickBooksConnection.objects.create(
            company=company, realm_id="12345", environment="sandbox",
            access_token_expires_at=timezone.now() + timedelta(hours=1),
        )
        connection.set_access_token("token")
        connection.set_refresh_token("refresh")
        connection.save()
        run = push.push_all(connection)
        self.assertEqual(list(QuickBooksSyncError.objects.filter(company=company).values_list("entity_type", "message")), [])
        self.assertEqual(run.status, "success")
