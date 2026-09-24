"""Adding a client from the Sales page (POST /api/sales/customers/)."""
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Company, User
from sales.models import Customer


class AddClientTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Fizz Works", slug="fizzworks")
        self.other = Company.objects.create(name="Other Co", slug="otherco")
        self.sales_user = User.objects.create_user(
            username="sam.sales", email="sam@fizz.test", role="sales", company=self.company, password="pass",
        )
        self.client.force_authenticate(user=self.sales_user)

    def test_sales_user_adds_client_to_own_company(self):
        res = self.client.post("/api/sales/customers/", {
            "name": "Corner Store Ltd", "email": "orders@corner.test", "phone": "+91 98765 43210",
            "address": "12 Market Road", "payment_terms": "Net 30",
            "company": self.other.id,  # must be ignored: tenancy comes from the user
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        customer = Customer.objects.get(pk=res.data["id"])
        self.assertEqual(customer.company, self.company)
        self.assertEqual(customer.payment_terms, "Net 30")
        self.assertIn(customer.id, [c["id"] for c in self.client.get("/api/sales/customers/").data])

    def test_name_is_required(self):
        res = self.client.post("/api/sales/customers/", {"name": "", "email": "x@y.test"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("name", res.data)
