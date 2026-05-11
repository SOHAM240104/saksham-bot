from app.models.chat.chat import Conversation, Thread
from app.models.subscriptions import Subscription, SubscriptionPlan


FREE_THREAD_LIMIT = 10


class FreeTierThreadLimitReached(Exception):
    """Raised when free-tier user has reached max allowed threads."""


def free_thread_limit(db, conversation_id: int) -> None:
    """
    If senior is on active free plan, allow only FREE_THREAD_LIMIT total threads
    across chatbot + techsaathi.
    """
    conversation = (
        db.query(Conversation)
        .filter(
            Conversation.id == conversation_id,
            Conversation.is_deleted.is_(False),
        )
        .first()
    )
    if not conversation or not conversation.senior_id:
        return

    senior_id = conversation.senior_id

    # check active free subscription 
    is_active_free = (
        db.query(Subscription.id)
        .join(SubscriptionPlan, Subscription.plan_id == SubscriptionPlan.id)
        .filter(
            Subscription.user_id == senior_id,
            Subscription.status == "active",
            SubscriptionPlan.plan_type == "free",
        )
        .first()
        is not None
    )
    if not is_active_free:
        return

    thread_count = (
        db.query(Thread.id)
        .join(Conversation, Thread.conversation_id == Conversation.id)
        .filter(
            Conversation.senior_id == senior_id,
            Conversation.is_deleted.is_(False),
            Thread.is_deleted.is_(False),
        )
        .count()
    )

    if thread_count >= FREE_THREAD_LIMIT:
        raise FreeTierThreadLimitReached(
            f"Free-tier limit reached ({FREE_THREAD_LIMIT} threads)."
        )