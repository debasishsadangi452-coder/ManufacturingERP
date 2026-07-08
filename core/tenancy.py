"""Company scoping for business data.

Root models carry a nullable `company` FK; child models derive their company
through a relation path. Viewsets mix in CompanyScopedMixin with
`company_field` set to the lookup path from the model to its company.

Rows with company=NULL are legacy/global rows: hidden from every company.
"""


class CompanyScopedMixin:
    """Filters list/detail by request.user.company and stamps it on create.

    company_field: lookup path to the company (e.g. "company",
    "vendor__company", "recipe__product__company").
    Stamping only happens for direct FKs (company_field == "company").
    """

    company_field = "company"

    def get_queryset(self):
        qs = super().get_queryset()
        company = getattr(self.request.user, "company", None)
        if company is None:
            return qs.none()
        return qs.filter(**{self.company_field: company})

    def perform_create(self, serializer):
        if self.company_field == "company":
            serializer.save(company=self.request.user.company)
        else:
            super().perform_create(serializer)
