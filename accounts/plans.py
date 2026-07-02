"""
Plan capability map — single backend source of truth for the 4 commercial plans.
See ERP_DOCS/08_subscription_plan_strategy.md.

Role permissions decide what a user may do inside the company;
the subscription plan decides what the company has purchased.
"""

UNLIMITED = None  # stored as null limits meaning "unlimited / fair usage"

PLAN_CONFIG = {
    "starter": {
        "name": "Starter",
        "tagline": "Core operational visibility for small plants",
        "user_limit": 5,
        "warehouse_limit": 1,
        "production_line_limit": 2,
        "ai_monthly_message_limit": 0,
        "modules": [
            "dashboard",
            "inventory",
            "production",
            "quality",
            "maintenance_limited",
            "workforce_self_service",
            "notifications",
        ],
        "features": [
            "Dashboard, Inventory, Production & Quality",
            "Workforce self-service",
            "Basic notifications",
            "1 warehouse, 2 production lines",
        ],
        "excluded": ["Procurement & Sales", "Finance", "Audit Logs", "AI features"],
    },
    "standard": {
        "name": "Standard",
        "tagline": "Daily manufacturing and commercial workflows",
        "user_limit": 20,
        "warehouse_limit": 3,
        "production_line_limit": 5,
        "ai_monthly_message_limit": 0,
        "modules": [
            "dashboard",
            "inventory",
            "production",
            "quality",
            "maintenance",
            "procurement",
            "sales",
            "logistics",
            "projection_demo",
            "workforce_self_service",
            "workforce_limited",
            "notifications",
        ],
        "features": [
            "Everything in Starter",
            "Procurement, Sales & Logistics",
            "Maintenance management",
            "Demand projection (demo)",
            "3 warehouses, 5 production lines",
        ],
        "excluded": ["Finance", "Audit Logs", "AI features"],
    },
    "professional": {
        "name": "Professional",
        "tagline": "Full plant operations, finance and compliance",
        "user_limit": 50,
        "warehouse_limit": 10,
        "production_line_limit": UNLIMITED,
        "ai_monthly_message_limit": 0,
        "modules": [
            "dashboard",
            "inventory",
            "production",
            "quality",
            "maintenance",
            "procurement",
            "sales",
            "logistics",
            "projection_demo",
            "workforce_self_service",
            "workforce_full",
            "finance",
            "audit_logs",
            "settings_advanced",
            "notifications",
        ],
        "features": [
            "Everything in Standard",
            "Finance & payroll workflows",
            "Full workforce management",
            "Audit logs & advanced settings",
            "10 warehouses, unlimited lines",
        ],
        "excluded": ["AI chatbot & AI automation"],
    },
    "premium_ai": {
        "name": "Premium AI",
        "tagline": "Everything plus AI chatbot and automation",
        "user_limit": UNLIMITED,
        "warehouse_limit": UNLIMITED,
        "production_line_limit": UNLIMITED,
        "ai_monthly_message_limit": 2000,
        "modules": [
            "dashboard",
            "inventory",
            "production",
            "quality",
            "maintenance",
            "procurement",
            "sales",
            "logistics",
            "projection_ai",
            "workforce_self_service",
            "workforce_full",
            "finance",
            "audit_logs",
            "settings_advanced",
            "notifications",
            "ai_automation",
            "ai_chatbot",
            "ai_erp_actions",
            "ai_image_po",
            "ai_insights",
        ],
        "features": [
            "Everything in Professional",
            "Embedded AI chatbot on every page",
            "AI ERP actions & automation",
            "AI image purchase-order processing",
            "AI insights, unlimited users & warehouses",
        ],
        "excluded": [],
    },
}

PLAN_ORDER = ["starter", "standard", "professional", "premium_ai"]


def get_plan_config(plan: str) -> dict:
    return PLAN_CONFIG[plan]
