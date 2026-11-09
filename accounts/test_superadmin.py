"""Platform super admin: sees every user, can block/unblock/delete them."""
import os
from unittest import mock

from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Company, LoginEvent, User
from accounts.superadmin import ensure_superadmin

USERS = "/api/auth/superadmin/users/"
LOGINS = "/api/auth/superadmin/logins/"
COMPANIES = "/api/auth/superadmin/companies/"
ENV = {"SUPERADMIN_EMAIL": "superadmin@venus.erp", "SUPERADMIN_PASSWORD": "pw-for-tests"}


class SuperAdminTests(APITestCase):
    def setUp(self):
        with mock.patch.dict(os.environ, ENV):
            self.root, _ = ensure_superadmin()
        self.company = Company.objects.create(name="Fizz Works", slug="fizzworks", superadmin_visible=True)
        self.staff = User.objects.create_user(
            username="sam.sales@fizzworks", email="sam@fizz.test", role="sales", company=self.company, password="pass",
        )
        self.client.force_authenticate(user=self.root)

    def test_not_created_without_env(self):
        with mock.patch.dict(os.environ, {"SUPERADMIN_EMAIL": "", "SUPERADMIN_PASSWORD": ""}):
            self.assertEqual(ensure_superadmin(), (None, False))

    def test_ensure_is_idempotent_and_keeps_changed_password(self):
        self.root.set_password("changed")
        self.root.save()
        with mock.patch.dict(os.environ, ENV):
            user, created = ensure_superadmin()
        self.assertFalse(created)
        self.assertTrue(user.check_password("changed"))

    def test_lists_all_users_across_companies(self):
        res = self.client.get(USERS)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        names = {u["username"] for u in res.data["users"]}
        self.assertIn("sam.sales@fizzworks", names)
        self.assertEqual(res.data["summary"]["companies"], 1)

    def test_company_users_cannot_use_it(self):
        self.client.force_authenticate(user=self.staff)
        self.assertEqual(self.client.get(USERS).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.post(f"{USERS}{self.staff.id}/block/").status_code, status.HTTP_403_FORBIDDEN)

    def test_superadmin_has_no_access_to_company_apis(self):
        self.assertEqual(self.client.get("/api/sales/quotations/").status_code, status.HTTP_403_FORBIDDEN)

    def test_block_signs_user_out_and_unblock_restores(self):
        self.assertEqual(self.client.post(f"{USERS}{self.staff.id}/block/").status_code, 200)
        self.staff.refresh_from_db()
        self.assertFalse(self.staff.is_active)
        # Login now fails with an explanatory message (correct password only).
        self.client.force_authenticate(user=None)
        res = self.client.post("/api/token/", {"username": "sam.sales@fizzworks", "password": "pass"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("blocked", str(res.data["detail"]))
        bad = self.client.post("/api/token/", {"username": "sam.sales@fizzworks", "password": "nope"}, format="json")
        self.assertNotIn("blocked", str(bad.data["detail"]))
        self.client.force_authenticate(user=self.root)
        self.assertEqual(self.client.post(f"{USERS}{self.staff.id}/unblock/").status_code, 200)
        self.client.force_authenticate(user=None)
        ok = self.client.post("/api/token/", {"username": "sam.sales@fizzworks", "password": "pass"}, format="json")
        self.assertEqual(ok.status_code, 200)

    def test_delete_user_and_protect_superadmin(self):
        self.assertEqual(self.client.delete(f"{USERS}{self.staff.id}/").status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(User.objects.filter(pk=self.staff.pk).exists())
        self.assertEqual(self.client.delete(f"{USERS}{self.root.id}/").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.post(f"{USERS}{self.root.id}/block/").status_code, status.HTTP_400_BAD_REQUEST)


class NdaAcceptTests(APITestCase):
    def test_accept_is_recorded_for_any_user(self):
        user = User.objects.create_user(username="a@x", role="sales", password="p",
                                        company=Company.objects.create(name="C", slug="c"))
        self.client.force_authenticate(user=user)
        self.assertEqual(self.client.post("/api/auth/nda/accept/").status_code, 200)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.post("/api/auth/nda/accept/").status_code, 401)


class LoginIpTrackingTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Fizz Works", slug="fizzworks", superadmin_visible=True)
        self.user = User.objects.create_user(
            username="sam.sales@fizzworks", email="sam@fizz.test", role="sales", company=self.company, password="pass",
        )

    def login(self, password="pass", **meta):
        return self.client.post("/api/token/", {"username": "sam.sales@fizzworks", "password": password},
                                format="json", **meta)

    def test_successful_login_records_ip_from_proxy_header(self):
        res = self.login(HTTP_X_FORWARDED_FOR="203.0.113.7, 10.0.0.1", HTTP_USER_AGENT="TestBrowser/1.0")
        self.assertEqual(res.status_code, 200)
        self.assertIn("access", res.data)
        event = LoginEvent.objects.get()
        self.assertTrue(event.success)
        self.assertEqual(event.user, self.user)
        self.assertEqual(event.ip_address, "203.0.113.7")
        self.assertEqual(event.forwarded_for, "203.0.113.7, 10.0.0.1")
        self.assertEqual(event.user_agent, "TestBrowser/1.0")

    def test_failed_login_is_recorded_and_still_rejected(self):
        res = self.login(password="wrong", REMOTE_ADDR="198.51.100.9")
        self.assertEqual(res.status_code, 401)
        event = LoginEvent.objects.get()
        self.assertFalse(event.success)
        self.assertEqual(event.user, self.user)  # matched for display even though the password was wrong
        self.assertEqual(event.ip_address, "198.51.100.9")

    def test_unknown_account_attempt_is_recorded_without_a_user(self):
        res = self.client.post("/api/token/", {"username": "nobody@nowhere", "password": "x"}, format="json")
        self.assertEqual(res.status_code, 401)
        event = LoginEvent.objects.get()
        self.assertIsNone(event.user)
        self.assertEqual(event.username, "nobody@nowhere")

    def test_garbage_forwarded_header_falls_back_to_remote_addr(self):
        self.login(HTTP_X_FORWARDED_FOR="not-an-ip", REMOTE_ADDR="192.0.2.5")
        self.assertEqual(LoginEvent.objects.get().ip_address, "192.0.2.5")

    def test_super_admin_sees_ip_in_user_list_and_login_activity(self):
        with mock.patch.dict(os.environ, ENV):
            root, _ = ensure_superadmin()
        self.login(HTTP_X_FORWARDED_FOR="203.0.113.7")
        self.login(password="bad", HTTP_X_FORWARDED_FOR="203.0.113.99")
        self.client.force_authenticate(user=root)
        row = next(u for u in self.client.get(USERS).data["users"] if u["username"] == "sam.sales@fizzworks")
        self.assertEqual(row["last_login_ip"], "203.0.113.7")
        events = self.client.get(LOGINS).data["events"]
        self.assertEqual([e["ip_address"] for e in events], ["203.0.113.99", "203.0.113.7"])
        self.assertEqual(events[0]["company_name"], "Fizz Works")
        failed = self.client.get(LOGINS, {"result": "failed"}).data["events"]
        self.assertEqual(len(failed), 1)

    def test_login_activity_is_super_admin_only(self):
        self.client.force_authenticate(user=self.user)
        self.assertEqual(self.client.get(LOGINS).status_code, status.HTTP_403_FORBIDDEN)


class SuperAdminVisibilityTests(APITestCase):
    def setUp(self):
        with mock.patch.dict(os.environ, ENV):
            self.root, _ = ensure_superadmin()
        self.shown = Company.objects.create(name="Smarties Foods", slug="smarties", superadmin_visible=True)
        self.hidden = Company.objects.create(name="Other Co", slug="otherco")  # default: hidden
        self.shown_user = User.objects.create_user(
            username="a.admin@smarties", role="admin", company=self.shown, password="pass")
        self.hidden_user = User.objects.create_user(
            username="b.admin@otherco", role="admin", company=self.hidden, password="pass")
        self.client.force_authenticate(user=self.root)

    def test_only_visible_company_data_is_listed(self):
        data = self.client.get(USERS).data
        names = {u["username"] for u in data["users"]}
        self.assertIn("a.admin@smarties", names)
        self.assertNotIn("b.admin@otherco", names)
        self.assertEqual([c["name"] for c in data["companies"]], ["Smarties Foods"])
        self.assertEqual(data["summary"]["companies"], 1)
        self.assertEqual(data["summary"]["users"], 2)  # the visible user + the super admin itself

    def test_hidden_users_cannot_be_acted_on(self):
        for call in (self.client.post(f"{USERS}{self.hidden_user.id}/block/"),
                     self.client.delete(f"{USERS}{self.hidden_user.id}/")):
            self.assertEqual(call.status_code, status.HTTP_404_NOT_FOUND)
        self.hidden_user.refresh_from_db()
        self.assertTrue(self.hidden_user.is_active)

    def test_hidden_company_logins_are_not_shown(self):
        LoginEvent.objects.create(user=self.hidden_user, username="b.admin@otherco", success=True, ip_address="198.51.100.1")
        LoginEvent.objects.create(user=self.shown_user, username="a.admin@smarties", success=True, ip_address="198.51.100.2")
        ips = [e["ip_address"] for e in self.client.get(LOGINS).data["events"]]
        self.assertEqual(ips, ["198.51.100.2"])

    def test_migration_flags_only_smarties_foods(self):
        import importlib
        from django.apps import apps
        mod = importlib.import_module("accounts.migrations.0010_login_events_superadmin_visibility")
        smarties = Company.objects.create(name="Smarties Foods Manufacturing Pvt. Ltd.", slug="smartiesfoods")
        mod.show_smarties_in_superadmin(apps, None)
        smarties.refresh_from_db()
        self.hidden.refresh_from_db()
        self.assertTrue(smarties.superadmin_visible)
        self.assertFalse(self.hidden.superadmin_visible)


def run_inline(target, *args):
    target(*args)


class SuperAdminCreateCompanyTests(APITestCase):
    def setUp(self):
        with mock.patch.dict(os.environ, ENV):
            self.root, _ = ensure_superadmin()
        self.client.force_authenticate(user=self.root)

    def create(self, **body):
        with mock.patch("accounts.superadmin._spawn", run_inline):
            return self.client.post(COMPANIES, body, format="json")

    def test_creates_independent_copy_of_smarties_data(self):
        from inventory.models import Item
        from production.models import ProductionPlan
        from sales.models import SalesOrder
        res = self.create(name="Acme Cookies Ltd", password="Demo@12345")
        self.assertEqual(res.status_code, status.HTTP_202_ACCEPTED, res.data)
        self.assertEqual(res.data["slug"], "acmecookiesltd")
        self.assertIn("admin@acmecookiesltd", res.data["credentials"]["logins"])

        company = Company.objects.get(slug="acmecookiesltd")
        self.assertEqual(company.demo_status, "ready")
        self.assertTrue(company.superadmin_visible)
        # Same demo data set, as new rows owned by this company only.
        self.assertEqual(Item.objects.filter(company=company).count(), 14)
        self.assertTrue(Item.objects.filter(company=company, name="Smarties Cookies").exists())
        self.assertEqual(SalesOrder.objects.filter(customer__company=company).count(), 1)
        plan = ProductionPlan.objects.get(company=company)
        self.assertEqual(plan.plan_number, "PP-2026-001-ACMECOOKIESLTD")
        # Four logins, with the password that was chosen; the AI plan is active.
        self.assertEqual(User.objects.filter(company=company).count(), 4)
        self.client.force_authenticate(user=None)
        ok = self.client.post("/api/token/", {"username": "admin@acmecookiesltd", "password": "Demo@12345"}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(company.subscription.plan, "premium_ai")

    def test_two_companies_do_not_collide(self):
        from inventory.models import Item
        self.assertEqual(self.create(name="First Foods").status_code, 202)
        self.assertEqual(self.create(name="Second Foods").status_code, 202)
        self.assertEqual(Company.objects.filter(demo_status="ready").count(), 2)
        for slug in ("firstfoods", "secondfoods"):
            self.assertEqual(Item.objects.filter(company__slug=slug).count(), 14)

    def test_new_company_is_listed_and_its_users_are_visible(self):
        self.create(name="Listed Foods")
        rows = self.client.get(COMPANIES).data["companies"]
        self.assertEqual([r["name"] for r in rows], ["Listed Foods"])
        self.assertEqual(rows[0]["demo_status"], "ready")
        self.assertEqual(rows[0]["users"], 4)
        names = {u["username"] for u in self.client.get(USERS).data["users"]}
        self.assertIn("admin@listedfoods", names)

    def test_validation_and_duplicates(self):
        self.assertEqual(self.create(name="").status_code, 400)
        self.assertEqual(self.create(name="***").status_code, 400)
        self.assertEqual(self.create(name="Fine Foods", password="short").status_code, 400)
        self.assertEqual(self.create(name="Fine Foods").status_code, 202)
        self.assertEqual(self.create(name="fine foods").status_code, 409)  # name, case-insensitive
        Company.objects.create(name="Taken", slug="finefoods2")
        self.assertEqual(self.create(name="Fine-Foods 2").status_code, 409)  # same slug

    def test_failed_build_is_reported_and_can_be_retried(self):
        with mock.patch("seed_smarties_foods.seed", side_effect=RuntimeError("db exploded")):
            res = self.create(name="Doomed Foods")
        self.assertEqual(res.status_code, 202)
        row = self.client.get(COMPANIES).data["companies"][0]
        self.assertEqual(row["demo_status"], "failed")
        self.assertIn("db exploded", row["demo_error"])
        self.assertEqual(self.create(name="Doomed Foods").status_code, 202)  # retry replaces the shell
        self.assertEqual(Company.objects.get(slug="doomedfoods").demo_status, "ready")

    def test_stuck_build_times_out(self):
        from datetime import timedelta
        from django.utils import timezone
        stuck = Company.objects.create(name="Stuck Co", slug="stuckco", superadmin_visible=True, demo_status="creating")
        Company.objects.filter(pk=stuck.pk).update(created_at=timezone.now() - timedelta(minutes=30))
        self.assertEqual(self.client.get(COMPANIES).data["companies"][0]["demo_status"], "failed")

    def test_only_super_admin_can_create(self):
        company = Company.objects.create(name="Fizz Works", slug="fizzworks", superadmin_visible=True)
        staff = User.objects.create_user(username="x.admin@fizzworks", role="admin", company=company, password="p")
        self.client.force_authenticate(user=staff)
        self.assertEqual(self.client.post(COMPANIES, {"name": "Nope Co"}, format="json").status_code, 403)
        self.assertEqual(self.client.get(COMPANIES).status_code, 403)
