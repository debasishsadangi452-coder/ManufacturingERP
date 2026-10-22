"""Platform super admin: sees every user, can block/unblock/delete them."""
import os
from unittest import mock

from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Company, User
from accounts.superadmin import ensure_superadmin

USERS = "/api/auth/superadmin/users/"
ENV = {"SUPERADMIN_EMAIL": "superadmin@venus.erp", "SUPERADMIN_PASSWORD": "pw-for-tests"}


class SuperAdminTests(APITestCase):
    def setUp(self):
        with mock.patch.dict(os.environ, ENV):
            self.root, _ = ensure_superadmin()
        self.company = Company.objects.create(name="Fizz Works", slug="fizzworks")
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
