from rest_framework.test import APITestCase

from .models import User, Company, CompanySubscription


class CompanyRegistrationTests(APITestCase):
    """Public company sign-up: creates the company and its single admin with a
    generated username (name.admin@company); the admin logs in with email."""

    def register_company(self, **overrides):
        payload = {
            "company_name": "Acme Corp",
            "first_name": "Debasish",
            "last_name": "Sadangi",
            "email": "boss@acme.com",
            "password": "secret123",
        }
        payload.update(overrides)
        return self.client.post("/api/auth/company/register/", payload)

    def test_register_company_creates_admin_with_generated_username(self):
        res = self.register_company()
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["username"], "debasish.admin@acmecorp")
        self.assertEqual(res.data["company"], "Acme Corp")
        self.assertIn("access", res.data)
        self.assertIn("refresh", res.data)
        admin = User.objects.get(username="debasish.admin@acmecorp")
        self.assertEqual(admin.role, "admin")
        self.assertEqual(admin.company.slug, "acmecorp")

    def test_duplicate_company_name_rejected(self):
        self.register_company()
        res = self.register_company(email="other@x.com")
        self.assertEqual(res.status_code, 400)
        self.assertIn("company_name", res.data)

    def test_duplicate_email_rejected(self):
        self.register_company()
        res = self.register_company(company_name="Other Co")
        self.assertEqual(res.status_code, 400)
        self.assertIn("email", res.data)

    def test_admin_logs_in_with_email(self):
        self.register_company()
        res = self.client.post("/api/token/", {"username": "boss@acme.com", "password": "secret123"})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("access", res.data)

    def test_login_with_generated_username_still_works(self):
        self.register_company()
        res = self.client.post("/api/token/", {"username": "debasish.admin@acmecorp", "password": "secret123"})
        self.assertEqual(res.status_code, 200, res.data)

    def test_each_company_gets_its_own_admin(self):
        self.register_company()
        res = self.register_company(company_name="Beta Ltd", email="ceo@beta.com", first_name="Rita")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["username"], "rita.admin@betaltd")


class MemberRegistrationTests(APITestCase):
    """Admin registers members: usernames are generated as name.role@company,
    capped by the plan's user limit, one admin per company."""

    def setUp(self):
        res = self.client.post("/api/auth/company/register/", {
            "company_name": "Acme Corp",
            "first_name": "Boss",
            "email": "boss@acme.com",
            "password": "secret123",
        })
        self.company = Company.objects.get(slug="acmecorp")
        self.admin = User.objects.get(company=self.company, role="admin")
        self.client.force_authenticate(self.admin)

    def add_member(self, first_name="John", role="production", **overrides):
        payload = {"first_name": first_name, "role": role, "password": "pass1234"}
        payload.update(overrides)
        return self.client.post("/api/auth/register/", payload)

    def test_member_registration_blocked_before_plan_selected(self):
        res = self.add_member()
        self.assertEqual(res.status_code, 400)

    def test_member_gets_generated_username(self):
        self.client.post("/api/auth/subscription/select/", {"plan": "starter"})
        res = self.add_member(first_name="John", role="production")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["username"], "john.production@acmecorp")

    def test_username_collision_gets_suffix(self):
        self.client.post("/api/auth/subscription/select/", {"plan": "starter"})
        self.add_member(first_name="John", role="production")
        res = self.add_member(first_name="John", role="production")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["username"], "john2.production@acmecorp")

    def test_only_one_admin_per_company(self):
        self.client.post("/api/auth/subscription/select/", {"plan": "starter"})
        res = self.add_member(first_name="Impostor", role="admin")
        self.assertEqual(res.status_code, 400)
        self.assertIn("role", res.data)

    def test_user_limit_capped_per_company(self):
        self.client.post("/api/auth/subscription/select/", {"plan": "starter"})  # limit 5
        for i in range(4):  # admin already occupies 1 of 5 seats
            res = self.add_member(first_name=f"Worker{i}")
            self.assertEqual(res.status_code, 201, res.data)
        res = self.add_member(first_name="Extra")
        self.assertEqual(res.status_code, 400)

    def test_other_company_users_dont_count_against_limit(self):
        self.client.post("/api/auth/subscription/select/", {"plan": "starter"})
        other = Company.objects.create(name="Beta Ltd")
        for i in range(10):
            User.objects.create_user(username=f"b{i}@betaltd", password="x", role="sales", company=other)
        res = self.add_member()
        self.assertEqual(res.status_code, 201, res.data)

    def test_admin_can_edit_member_and_password_is_hashed(self):
        self.client.post("/api/auth/subscription/select/", {"plan": "starter"})
        member_id = self.add_member(first_name="John").data["id"]
        res = self.client.patch(f"/api/auth/users/{member_id}/", {
            "first_name": "Johnny", "role": "sales", "password": "newpass99",
        })
        self.assertEqual(res.status_code, 200, res.data)
        member = User.objects.get(pk=member_id)
        self.assertEqual(member.first_name, "Johnny")
        self.assertEqual(member.role, "sales")
        self.assertTrue(member.check_password("newpass99"))  # hashed, not stored raw

    def test_member_cannot_be_promoted_to_admin(self):
        self.client.post("/api/auth/subscription/select/", {"plan": "starter"})
        member_id = self.add_member(first_name="John").data["id"]
        res = self.client.patch(f"/api/auth/users/{member_id}/", {"role": "admin"})
        self.assertEqual(res.status_code, 400)

    def test_admin_role_cannot_be_changed(self):
        res = self.client.patch(f"/api/auth/users/{self.admin.id}/", {"role": "sales"})
        self.assertEqual(res.status_code, 400)

    def test_finance_can_only_edit_auto_approve_limit(self):
        self.client.post("/api/auth/subscription/select/", {"plan": "starter"})
        member_id = self.add_member(first_name="John").data["id"]
        finance = User.objects.create_user(
            username="fin@acmecorp", password="x", role="finance", company=self.company
        )
        self.client.force_authenticate(finance)
        res = self.client.patch(f"/api/auth/users/{member_id}/", {"auto_approve_limit": 5000})
        self.assertEqual(res.status_code, 200, res.data)
        res = self.client.patch(f"/api/auth/users/{member_id}/", {"first_name": "Hacked"})
        self.assertEqual(res.status_code, 400)

    def test_users_list_scoped_to_own_company(self):
        other = Company.objects.create(name="Beta Ltd")
        User.objects.create_user(username="spy@betaltd", password="x", role="sales", company=other)
        res = self.client.get("/api/auth/users/")
        self.assertEqual(res.status_code, 200)
        usernames = [u["username"] for u in res.data]
        self.assertNotIn("spy@betaltd", usernames)


class SubscriptionOnboardingTests(APITestCase):
    """Plan selection + onboarding wizard, scoped to the admin's company."""

    def setUp(self):
        self.client.post("/api/auth/company/register/", {
            "company_name": "Acme Corp",
            "first_name": "Boss",
            "email": "boss@acme.com",
            "password": "secret123",
        })
        self.company = Company.objects.get(slug="acmecorp")
        self.admin = User.objects.get(company=self.company, role="admin")
        self.client.force_authenticate(self.admin)

    def test_plans_catalog_lists_four_plans(self):
        res = self.client.get("/api/auth/subscription/plans/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual([p["id"] for p in res.data["plans"]],
                         ["starter", "standard", "professional", "premium_ai"])

    def test_status_unconfigured_until_plan_selected(self):
        res = self.client.get("/api/auth/subscription/")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["configured"])

        res = self.client.post("/api/auth/subscription/select/", {"plan": "starter"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["subscription"]["plan"], "starter")
        self.assertEqual(res.data["subscription"]["user_limit"], 5)

        res = self.client.get("/api/auth/subscription/")
        self.assertTrue(res.data["configured"])
        self.assertFalse(res.data["subscription"]["onboarding_completed"])

    def test_invalid_plan_rejected(self):
        res = self.client.post("/api/auth/subscription/select/", {"plan": "mega"})
        self.assertEqual(res.status_code, 400)

    def test_cannot_downgrade_below_user_count(self):
        self.client.post("/api/auth/subscription/select/", {"plan": "standard"})
        for i in range(6):
            self.client.post("/api/auth/register/", {
                "first_name": f"Member{i}", "password": "pass1234", "role": "sales",
            })
        res = self.client.post("/api/auth/subscription/select/", {"plan": "starter"})
        self.assertEqual(res.status_code, 400)

    def test_complete_onboarding(self):
        res = self.client.post("/api/auth/subscription/complete-onboarding/")
        self.assertEqual(res.status_code, 400)  # no plan yet

        self.client.post("/api/auth/subscription/select/", {"plan": "premium_ai"})
        res = self.client.post("/api/auth/subscription/complete-onboarding/")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(CompanySubscription.for_company(self.company).onboarding_completed)

    def test_non_admin_cannot_select_plan(self):
        worker = User.objects.create_user(
            username="w1@acmecorp", password="pass1234", role="production", company=self.company
        )
        self.client.force_authenticate(worker)
        res = self.client.post("/api/auth/subscription/select/", {"plan": "starter"})
        self.assertEqual(res.status_code, 403)

    def test_subscriptions_are_per_company(self):
        self.client.post("/api/auth/subscription/select/", {"plan": "premium_ai"})
        res = self.client.post("/api/auth/company/register/", {
            "company_name": "Beta Ltd",
            "first_name": "Rita",
            "email": "ceo@beta.com",
            "password": "secret123",
        })
        beta_admin = User.objects.get(username=res.data["username"])
        self.client.force_authenticate(beta_admin)
        res = self.client.get("/api/auth/subscription/")
        self.assertFalse(res.data["configured"])
