"""TechSaathi Team Inbox REST API."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import AuthenticatedTechSaathi, require_techsaathi_user
from app.config.base import get_db
from app.schema.agent import (
    AgentConversationDetailResponse,
    AgentConversationListResponse,
    AgentMessageDetailResponse,
    AgentMessageListResponse,
    AgentMessageRequest,
    AgentResolveDetailResponse,
)
from app.services.agent.inbox import (
    get_conversation_for_agent,
    get_conversation_messages,
    list_inbox_conversations,
    resolve_conversation_for_agent,
    send_agent_reply,
)

router = APIRouter(prefix="/agent", tags=["agent"])


@router.get("/conversations", response_model=AgentConversationListResponse)
def get_agent_conversations(
    auth: AuthenticatedTechSaathi = Depends(require_techsaathi_user),
    db: Session = Depends(get_db),
) -> AgentConversationListResponse:
    items = list_inbox_conversations(db, auth.tech_saathi.id)
    return AgentConversationListResponse(data=items)


@router.get("/conversations/{conversation_id}", response_model=AgentConversationDetailResponse)
def get_agent_conversation(
    conversation_id: int,
    auth: AuthenticatedTechSaathi = Depends(require_techsaathi_user),
    db: Session = Depends(get_db),
) -> AgentConversationDetailResponse:
    return AgentConversationDetailResponse(
        data=get_conversation_for_agent(db, conversation_id, auth.tech_saathi.id)
    )


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=AgentMessageListResponse,
)
def get_agent_conversation_messages(
    conversation_id: int,
    auth: AuthenticatedTechSaathi = Depends(require_techsaathi_user),
    db: Session = Depends(get_db),
) -> AgentMessageListResponse:
    items = get_conversation_messages(db, conversation_id, auth.tech_saathi.id)
    return AgentMessageListResponse(data=items)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=AgentMessageDetailResponse,
)
def post_agent_conversation_message(
    conversation_id: int,
    body: AgentMessageRequest,
    auth: AuthenticatedTechSaathi = Depends(require_techsaathi_user),
    db: Session = Depends(get_db),
) -> AgentMessageDetailResponse:
    item = send_agent_reply(db, conversation_id, auth.tech_saathi.id, body.message)
    db.commit()
    return AgentMessageDetailResponse(data=item)


@router.post(
    "/conversations/{conversation_id}/resolve",
    response_model=AgentResolveDetailResponse,
)
def post_agent_conversation_resolve(
    conversation_id: int,
    auth: AuthenticatedTechSaathi = Depends(require_techsaathi_user),
    db: Session = Depends(get_db),
) -> AgentResolveDetailResponse:
    result = resolve_conversation_for_agent(db, conversation_id, auth.tech_saathi.id)
    db.commit()
    return AgentResolveDetailResponse(data=result)
