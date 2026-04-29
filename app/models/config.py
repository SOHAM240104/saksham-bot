from sqlalchemy import Enum


USER_TYPE_ENUM = Enum("user", "senior", "tech_saathi", name="user_type")
GENDER_ENUM = Enum("male", "female", "other", name="gender")
ONBOARDING_TYPE_ENUM = Enum("self", "assisted", name="onboarding_type")

THREAD_ROLE_ENUM = Enum("chatbot", "techsaathi", name="thread_role")

THREAD_STATUS_ENUM = Enum(
    "assigned",
    "resolved",
    name="thread_status"
)