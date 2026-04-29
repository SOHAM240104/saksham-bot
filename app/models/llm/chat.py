from sqlalchemy import Column, Integer, String, ForeignKey, Float, CheckConstraint
from sqlalchemy.orm import relationship, backref


from app.models.base import BaseModel


from app.models.config import (
    THREAD_ROLE_ENUM,
    THREAD_STATUS_ENUM,
)


# =========================================================
# Conversation
# =========================================================

class Conversation(BaseModel):
    __tablename__ = "chatbot_conversation"

    language = Column(String,  ForeignKey("administration_language.identity"), nullable=True) 
    senior_id = Column(Integer, ForeignKey("senior.id"), nullable=True)

    threads = relationship(
        "Thread",
        backref=backref("conversation", lazy="select"),  
        cascade="all, delete-orphan"
    )

    messages = relationship(
        "Message",
        backref=backref("conversation", lazy="select"),  
        cascade="all, delete-orphan"
    )


# =========================================================
# Thread
# =========================================================

class Thread(BaseModel):
    __tablename__ = "chatbot_thread"

    conversation_id = Column(Integer, ForeignKey("chatbot_conversation.id"), nullable=False)

    role = Column(THREAD_ROLE_ENUM, nullable=False)
    techsaathi_id = Column(Integer, ForeignKey("tech_saathi.id"), nullable=True)

    status = Column(THREAD_STATUS_ENUM, nullable=False, default="assigned")#default value what to set?

    messages = relationship(
        "Message",
        backref=backref("thread", lazy="select"),  
        cascade="all, delete-orphan"
    )


# =========================================================
# Message
# =========================================================

class Message(BaseModel):
    __tablename__ = "chatbot_message"

    conversation_id = Column(Integer, ForeignKey("chatbot_conversation.id"), nullable=False)
    thread_id = Column(Integer, ForeignKey("chatbot_thread.id"), nullable=False)

    user_message = Column(String, nullable=False)

    bot_response = Column(String, nullable=True)
    tech_saathi_response = Column(String, nullable=True)

    template = Column(String, nullable=True)
    confidence_score = Column(Float, nullable=True)

    message_source = Column(String, nullable=True)
    wati_message_id = Column(String, nullable=True)

  
    