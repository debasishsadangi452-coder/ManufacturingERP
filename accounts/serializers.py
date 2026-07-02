from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import User, Company, CompanySubscription, generate_username


class UserSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)
    company_slug = serializers.CharField(source='company.slug', read_only=True)

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "password",
            "email",
            "first_name",
            "last_name",
            "role",
            "company_name",
            "company_slug",
            "auto_approve_limit",
        ]
        extra_kwargs = {
            "password": {"write_only": True, "required": False},
            "username": {"read_only": True},
        }

    def validate(self, attrs):
        request = self.context.get("request")
        if self.instance is not None and request is not None:
            if request.user.role != "admin":
                # Finance may only adjust the auto-approve limit; everything else is admin-only.
                disallowed = set(attrs) - {"auto_approve_limit"}
                if disallowed:
                    raise serializers.ValidationError(
                        "Only the company admin can edit user details."
                    )
            new_role = attrs.get("role")
            if new_role and new_role != self.instance.role:
                if self.instance.role == "admin":
                    raise serializers.ValidationError(
                        {"role": "The company admin's role cannot be changed."}
                    )
                if new_role == "admin":
                    raise serializers.ValidationError(
                        {"role": "Only one admin account is allowed per company."}
                    )
        return attrs

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


class RegisterUserSerializer(serializers.ModelSerializer):
    """Admin registers a team member for their own company.
    The username is auto-generated as firstname.role@companyslug; enforces
    one admin per company and the subscription plan's user limit."""

    company_name = serializers.CharField(source='company.name', read_only=True)

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "password",
            "email",
            "first_name",
            "last_name",
            "role",
            "company_name",
            "auto_approve_limit",
        ]
        extra_kwargs = {
            "password": {"write_only": True},
            "username": {"read_only": True},
            "first_name": {"required": True, "allow_blank": False},
        }

    def validate(self, attrs):
        company = self.context["request"].user.company
        if company is None:
            raise serializers.ValidationError(
                "Your account is not linked to a company. Register a company first."
            )

        if attrs.get("role") == "admin" and User.objects.filter(company=company, role="admin").exists():
            raise serializers.ValidationError(
                {"role": "Only one admin account is allowed per company."}
            )

        subscription = CompanySubscription.for_company(company)
        if subscription is None:
            raise serializers.ValidationError(
                "Select a subscription plan before registering team members."
            )
        if subscription.user_limit is not None:
            if User.objects.filter(company=company).count() >= subscription.user_limit:
                raise serializers.ValidationError(
                    f"User limit reached: the {subscription.get_plan_display()} plan "
                    f"allows up to {subscription.user_limit} users. Upgrade your plan to add more."
                )
        return attrs

    def create(self, validated_data):
        company = self.context["request"].user.company
        username = generate_username(validated_data["first_name"], validated_data["role"], company)
        return User.objects.create_user(username=username, company=company, **validated_data)


class CompanyRegisterSerializer(serializers.Serializer):
    """Public sign-up: a new user registers their company and becomes its admin.
    Their username is generated as firstname.admin@companyslug; they log in with
    their email address."""

    company_name = serializers.CharField(max_length=100)
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=6)

    def validate_company_name(self, value):
        from django.utils.text import slugify
        slug = slugify(value).replace('-', '')
        if not slug:
            raise serializers.ValidationError("Company name must contain letters or numbers.")
        if Company.objects.filter(name__iexact=value).exists() or Company.objects.filter(slug=slug).exists():
            raise serializers.ValidationError("A company with this name is already registered.")
        return value

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError(
                "This email is already in use. Admins log in with their email, so it must be unique."
            )
        return value

    def create(self, validated_data):
        company = Company.objects.create(name=validated_data["company_name"])
        return User.objects.create_user(
            username=generate_username(validated_data["first_name"], "admin", company),
            email=validated_data["email"],
            password=validated_data["password"],
            first_name=validated_data["first_name"],
            last_name=validated_data.get("last_name", ""),
            role="admin",
            company=company,
        )


class EmailOrUsernameTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Accepts either the generated username (name.role@company) or an email
    address in the username field — admins always log in with their email."""

    def validate(self, attrs):
        login = attrs.get(self.username_field, "")
        if login and not User.objects.filter(username=login).exists():
            match = User.objects.filter(email__iexact=login).order_by("id").first()
            if match:
                attrs[self.username_field] = match.username
        return super().validate(attrs)


class CompanySubscriptionSerializer(serializers.ModelSerializer):
    plan_name = serializers.CharField(source='get_plan_display', read_only=True)
    company_name = serializers.CharField(source='company.name', read_only=True)

    class Meta:
        model = CompanySubscription
        fields = [
            "plan",
            "plan_name",
            "company_name",
            "status",
            "user_limit",
            "warehouse_limit",
            "production_line_limit",
            "ai_monthly_message_limit",
            "ai_messages_used",
            "current_period_start",
            "current_period_end",
            "onboarding_completed",
        ]
