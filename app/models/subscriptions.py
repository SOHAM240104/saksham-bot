from sqlalchemy import Column, String, Integer, ForeignKey 
from sqlalchemy import DateTime, Boolean
from sqlalchemy.orm import relationship
from app.models.config import SUBSCRIPTION_STATUS_ENUM, PLAN_TYPE_ENUM, PAID_PLAN_ENUM, INTERVAL_ENUM

from app.models.base import BaseModel  

class SubscriptionPlan(BaseModel):
    __tablename__ = "subscription_plan"
    identity = Column(String(255), nullable=False)
    description = Column(String, nullable=True)
    plan_type = Column(PLAN_TYPE_ENUM, nullable=False)
    base_plan = Column(PAID_PLAN_ENUM, nullable=True)
    amount = Column(Integer, nullable=False)  
    currency = Column(String(10), nullable=False, default="INR")
    interval = Column(INTERVAL_ENUM, nullable=True)
    razorpay_plan_id = Column(String(255), nullable=True)
    tag = Column(String(255), nullable=False)
    validity_in_years = Column(Integer, nullable=False, default=10)
    def __str__(self):
        return self.identity


class Subscription(BaseModel):
    __tablename__ = "subscription_subscription"
    user_id = Column(Integer, ForeignKey("senior_senior.id", ondelete="CASCADE"), nullable=False) 
    plan_id= Column(Integer, ForeignKey("subscription_plan.id", ondelete="CASCADE"), nullable=False)
    start_date = Column(DateTime, nullable=True)
    end_date = Column(DateTime, nullable=True)
    status = Column(
        SUBSCRIPTION_STATUS_ENUM,
        default="pending",
        nullable=False
    )

    razorpay_subscription_id = Column(String(255), nullable=True)
    order_id = Column(String(255), nullable=True)

    current_start = Column(DateTime, nullable=True)
    current_end = Column(DateTime, nullable=True)
    charge_at = Column(DateTime, nullable=True)

    total_count = Column(Integer, default=0)
    paid_count = Column(Integer, default=0)

    has_scheduled_changes = Column(Boolean, default=False)
    scheduled_changes_at = Column(DateTime, nullable=True)

    user = relationship("Senior", backref="subscriptions")
    plan = relationship("SubscriptionPlan", backref="subscriptions")
