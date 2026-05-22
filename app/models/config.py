from sqlalchemy import Enum


USER_TYPE_ENUM = Enum("user", "senior", "tech_saathi","admin", name="user_type")
GENDER_ENUM = Enum("male", "female", "other", name="gender")
ONBOARDING_TYPE_ENUM = Enum("self", "assisted", name="onboarding_type")

THREAD_ROLE_ENUM = Enum("chatbot", "techsaathi", name="thread_role")

THREAD_STATUS_ENUM = Enum(
    "assigned",
    "resolved",
    name="thread_status"
)

SUBSCRIPTION_STATUS_ENUM = Enum(
    "pending",
    "active",
    "cancelled",
    "expired",
    name="subscription_status",
)


PLAN_TYPE_ENUM = Enum(
    "free",
    "paid",
    name="plan_type",
)
PAID_PLAN_ENUM = Enum(
    "monthly",
    "yearly",
    "lifetime",
    name="paid_plan",
)
INTERVAL_ENUM = Enum(
    "daily",
    "weekly",
    "monthly",
    "yearly",
    name="plan_interval",
)