from sqlalchemy import Column, Integer, String, ForeignKey, Float, CheckConstraint,Text
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

    language_id = Column(Integer, ForeignKey("administration_language.id"), nullable=True)
    senior_id = Column(Integer, ForeignKey("senior_senior.id"), nullable=True)

    wati_conversation_id = Column(String, nullable=True)


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
    tech_saathi_id = Column(Integer, ForeignKey("tech_saathi_techsaathi.id"), nullable=True)

    status = Column(THREAD_STATUS_ENUM, nullable=False, default="assigned")#default value what to set?

    messages = relationship(
        "Message",
        backref=backref("thread", lazy="select"),  
        cascade="all, delete-orphan"
    )
    techsaathi = relationship(
    "TechSaathi",
    backref=backref("threads", lazy="select")
)


# =========================================================
# Message
# =========================================================

class Message(BaseModel):
    __tablename__ = "chatbot_message"

    conversation_id = Column(Integer, ForeignKey("chatbot_conversation.id"), nullable=False)
    thread_id = Column(Integer, ForeignKey("chatbot_thread.id"), nullable=False)

    user_message = Column(Text, nullable=False)
    tech_saathi_id = Column(Integer, ForeignKey("tech_saathi_techsaathi.id"), nullable=True)
    

    bot_response = Column(Text, nullable=True)
    tech_saathi_response = Column(Text, nullable=True)

    template = Column(Text, nullable=True)
    confidence_score = Column(Float, nullable=True)

    message_source = Column(Text , nullable=True)
    wati_message_id = Column(Text, nullable=True)
  
    techsaathi = relationship(
    "TechSaathi",
    backref=backref("messages", lazy="select")
)