"""Code-owned conversation episode — branch, phase, awaiting, stuck cycle.

Lifecycle (resolve / mode reset / Still Stuck / handoff) is decided here.
The LLM may *suggest* RESOLVED; it does not commit thread close.
State is persisted on bot message_source as an ``ep:`` tag so no DB migration
is required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

# Branches
BRANCH_NONE = "none"
BRANCH_TECH = "tech"
BRANCH_SCAM = "scam"

# Phases
PHASE_IDLE = "idle"
PHASE_INTAKE = "intake"
PHASE_TROUBLESHOOTING = "troubleshooting"
PHASE_DIAGNOSTIC = "diagnostic"
PHASE_REFINED = "refined"
PHASE_SCAM_FLOW = "scam_flow"
PHASE_CLOSING = "closing"

# What the last bot turn left open for the senior
AWAITING_NONE = "none"
AWAITING_FEEDBACK = "feedback"
AWAITING_MODE = "mode"
AWAITING_PLATFORM_CONFIRM = "platform_confirm"
AWAITING_HANDOFF_CONFIRM = "handoff_confirm"
AWAITING_PLATFORM_PICKER = "platform_picker"
AWAITING_SCAM_OS = "scam_os"

_EP_RE = re.compile(
    r"(?:^|,)ep:(?P<branch>[a-z]+)\|(?P<phase>[a-z_]+)\|(?P<awaiting>[a-z_]+)\|(?P<stuck>\d+)"
)

# Soft mid-flow acks that keep the arc open (farewells / closes are classifier-owned)
_ACK_ONLY = frozenset(
    {
        "ok",
        "okay",
        "k",
        "kk",
        "haan",
        "ha",
        "han",
        "ji",
        "sure",
        "thanks",
        "thank you",
        "thx",
        "cool",
        "alright",
        "all right",
        "got it",
        "hmm",
        "hm",
        "yes",
        "y",
        "yeah",
        "yep",
    }
)

_STRONG_FIXED = (
    "resolved",
    "it's fixed",
    "its fixed",
    "it worked",
    "that worked",
    "working now",
    "works now",
    "all good",
    "all done",
    "sorted",
    "problem solved",
    "issue resolved",
    "fixed now",
    "thank you thats all",
    "thank you that's all",
    "thanks thats all",
    "that's all",
    "thats all",
    "nothing else",
)


@dataclass(frozen=True)
class Episode:
    branch: str = BRANCH_NONE
    phase: str = PHASE_IDLE
    awaiting: str = AWAITING_NONE
    stuck_cycle: int = 0

    def encode(self) -> str:
        stuck = max(0, min(9, int(self.stuck_cycle)))
        return f"ep:{self.branch}|{self.phase}|{self.awaiting}|{stuck}"


def empty_episode() -> Episode:
    return Episode()


def parse_episode(message_source: str | None) -> Episode:
    """Read episode from the last bot message_source; default empty if missing."""
    raw = (message_source or "").strip()
    if not raw:
        return empty_episode()
    m = _EP_RE.search(raw.replace(" ", ""))
    if not m:
        # Infer awaiting from legacy tags so older turns still gate correctly
        return _infer_from_legacy_source(raw)
    try:
        stuck = int(m.group("stuck"))
    except ValueError:
        stuck = 0
    return Episode(
        branch=(m.group("branch") or BRANCH_NONE).strip().lower(),
        phase=(m.group("phase") or PHASE_IDLE).strip().lower(),
        awaiting=(m.group("awaiting") or AWAITING_NONE).strip().lower(),
        stuck_cycle=stuck,
    )


def _infer_from_legacy_source(source: str) -> Episode:
    s = (source or "").lower()
    if "feedback_buttons" in s or "refined_unresolved" in s:
        phase = PHASE_REFINED if "refined_unresolved" in s else PHASE_TROUBLESHOOTING
        return Episode(
            branch=BRANCH_TECH,
            phase=phase,
            awaiting=AWAITING_FEEDBACK,
            stuck_cycle=0,
        )
    if "unresolved_diagnostic" in s:
        return Episode(
            branch=BRANCH_TECH,
            phase=PHASE_DIAGNOSTIC,
            awaiting=AWAITING_NONE,
            stuck_cycle=1,
        )
    if "platform_confirmation" in s:
        return Episode(
            branch=BRANCH_TECH,
            phase=PHASE_INTAKE,
            awaiting=AWAITING_PLATFORM_CONFIRM,
            stuck_cycle=0,
        )
    if "handoff_confirmation" in s:
        return Episode(
            branch=BRANCH_TECH,
            phase=PHASE_TROUBLESHOOTING,
            awaiting=AWAITING_HANDOFF_CONFIRM,
            stuck_cycle=0,
        )
    if "mode_buttons" in s or "post_resolve_welcome" in s:
        return Episode(
            branch=BRANCH_NONE,
            phase=PHASE_IDLE,
            awaiting=AWAITING_MODE,
            stuck_cycle=0,
        )
    if "platform_list" in s:
        return Episode(
            branch=BRANCH_TECH,
            phase=PHASE_INTAKE,
            awaiting=AWAITING_PLATFORM_PICKER,
            stuck_cycle=0,
        )
    if "scam_os" in s:
        return Episode(
            branch=BRANCH_SCAM,
            phase=PHASE_SCAM_FLOW,
            awaiting=AWAITING_SCAM_OS,
            stuck_cycle=0,
        )
    if "scam_flow" in s or "scam_entry" in s or "scam_story_ask" in s:
        return Episode(
            branch=BRANCH_SCAM,
            phase=PHASE_SCAM_FLOW,
            awaiting=AWAITING_NONE,
            stuck_cycle=0,
        )
    return empty_episode()


def strip_episode_tag(message_source: str | None) -> str:
    raw = (message_source or "").strip()
    if not raw:
        return ""
    parts = []
    for part in raw.split(","):
        p = part.strip()
        if not p:
            continue
        if p.startswith("ep:") or _EP_RE.fullmatch(p) or p.startswith("ep:"):
            continue
        if re.match(r"^ep:[a-z]+\|[a-z_]+\|[a-z_]+\|\d+$", p, re.I):
            continue
        parts.append(p)
    return ",".join(parts)


def tag_message_source(message_source: str | None, episode: Episode) -> str:
    base = strip_episode_tag(message_source)
    tag = episode.encode()
    if not base:
        return tag
    if tag in base:
        return base
    return f"{base},{tag}"


def reset_for_mode_chip(button_id: str) -> Episode | None:
    """Explicit Tech/Scam chip starts a new episode. Returns None if not a mode chip."""
    btn = (button_id or "").strip().lower()
    if btn == "tech":
        return Episode(
            branch=BRANCH_TECH,
            phase=PHASE_INTAKE,
            awaiting=AWAITING_NONE,
            stuck_cycle=0,
        )
    if btn == "scam":
        return Episode(
            branch=BRANCH_SCAM,
            phase=PHASE_SCAM_FLOW,
            awaiting=AWAITING_NONE,
            stuck_cycle=0,
        )
    return None


def _normalize_user_text(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip().lower())
    return t.rstrip(".!? ")


def is_ack_only(text: str) -> bool:
    t = _normalize_user_text(text)
    if not t:
        return False
    if t in _ACK_ONLY:
        return True
    # very short thank-you / soft-ok variants (closes/farewells → classifier RESOLVED)
    if len(t) <= 24 and t.startswith(("thank", "ok ", "okay")):
        return True
    return False


def is_strong_issue_fixed(text: str) -> bool:
    t = _normalize_user_text(text)
    if not t:
        return False
    if t in {"resolved", "fixed", "done", "sorted"}:
        return True
    return any(p in t for p in _STRONG_FIXED)


def is_strong_scam_close(text: str) -> bool:
    """User clearly ending scam help (thanks + done), not mid-flow bank/OTP."""
    t = _normalize_user_text(text)
    if not t:
        return False
    if any(p in t for p in ("thats all", "that's all", "nothing else", "all done", "no more")):
        return True
    if ("thank" in t or t in {"thanks", "thank you"}) and any(
        p in t for p in ("all", "bye", "done", "enough")
    ):
        return True
    if t in {"thank you", "thanks", "ok thanks", "okay thanks", "thanks bye"}:
        return True
    return False


def may_resolve(
    *,
    episode: Episode,
    button_id: str = "",
    user_message: str = "",
    suggest_intent: str = "",
    scam_context: bool = False,
) -> bool:
    """
    Hard gate: only True when code allows closing the chatbot thread.

    Classifier / LLM ``RESOLVED`` is advisory — pass as suggest_intent.
    """
    btn = (button_id or "").strip().lower()
    intent = (suggest_intent or "").strip().upper()
    msg = (user_message or "").strip()

    # Explicit Resolved chip always wins on tech
    if btn == "resolved":
        return True

    awaiting = (episode.awaiting or AWAITING_NONE).strip().lower()
    phase = (episode.phase or PHASE_IDLE).strip().lower()
    branch = (episode.branch or BRANCH_NONE).strip().lower()
    on_scam = bool(scam_context) or branch == BRANCH_SCAM

    # Feedback chips open: chip / clear "it worked" / classifier RESOLVED (not soft ok).
    # Farewell vs soft-ack meaning is classifier-owned — code only blocks ok/okay-style holds.
    if awaiting == AWAITING_FEEDBACK:
        if btn == "resolved":
            return True
        if is_strong_issue_fixed(msg):
            return True
        if intent == "RESOLVED" and not is_ack_only(msg):
            return True
        return False
    if awaiting in {
        AWAITING_PLATFORM_CONFIRM,
        AWAITING_HANDOFF_CONFIRM,
        AWAITING_PLATFORM_PICKER,
        AWAITING_SCAM_OS,
        AWAITING_MODE,
    }:
        return False

    # Mid tech ladder without feedback chips (OS ask loop, etc.)
    if not on_scam and phase in {
        PHASE_TROUBLESHOOTING,
        PHASE_DIAGNOSTIC,
        PHASE_REFINED,
        PHASE_INTAKE,
    }:
        if intent == "RESOLVED":
            # Soft ok/okay stay open; farewells / clear closes from classifier may close
            if is_ack_only(msg) and not is_strong_issue_fixed(msg):
                return False
            return True
        if is_ack_only(msg) and not is_strong_issue_fixed(msg):
            return False
        return is_strong_issue_fixed(msg)

    if on_scam:
        if intent == "RESOLVED":
            return is_strong_scam_close(msg) or is_strong_issue_fixed(msg)
        return is_strong_scam_close(msg)

    if intent == "RESOLVED":
        # Idle / ambiguous — allow classifier closes; block only soft mid-flow acks
        if is_ack_only(msg) and not is_strong_issue_fixed(msg) and not is_strong_scam_close(msg):
            return False
        return True

    return False


def bump_stuck(episode: Episode) -> Episode:
    return replace(episode, stuck_cycle=min(9, int(episode.stuck_cycle) + 1))


def still_stuck_transition(episode: Episode) -> tuple[Episode, str]:
    """
    Advance Still Stuck ladder from stored episode (not bot-text regex).

    Returns (new_episode, action) where action is one of:
      diagnostic | refined | handoff | none
    """
    ep = bump_stuck(episode)
    awaiting = (ep.awaiting or AWAITING_NONE).lower()
    phase = (ep.phase or PHASE_IDLE).lower()

    # After refined steps + Still Stuck → handoff
    if phase == PHASE_REFINED or (
        awaiting == AWAITING_FEEDBACK and phase == PHASE_REFINED
    ):
        return replace(ep, awaiting=AWAITING_NONE, phase=PHASE_CLOSING), "handoff"

    # After first troubleshooting steps + Still Stuck → diagnostic
    if awaiting == AWAITING_FEEDBACK or phase == PHASE_TROUBLESHOOTING:
        return (
            replace(ep, phase=PHASE_DIAGNOSTIC, awaiting=AWAITING_NONE),
            "diagnostic",
        )

    # After diagnostic reply (OS/model) → refined retry
    if phase == PHASE_DIAGNOSTIC:
        return (
            replace(ep, phase=PHASE_REFINED, awaiting=AWAITING_FEEDBACK),
            "refined",
        )

    # Never got steps (OS-ask loop / intake) — escalate after 2 stuck signals
    if ep.stuck_cycle >= 2:
        return replace(ep, awaiting=AWAITING_NONE, phase=PHASE_CLOSING), "handoff"

    # First stuck without steps: treat as diagnostic ask once, then next stuck hands off
    return (
        replace(ep, phase=PHASE_DIAGNOSTIC, awaiting=AWAITING_NONE),
        "diagnostic",
    )


def episode_after_outbound(
    *,
    prior: Episode,
    message_source: str = "",
    action: str = "",
    kind: str = "",
    forced_branch: str | None = None,
) -> Episode:
    """Derive the episode to stamp on the outbound we are about to send."""
    src = (message_source or "").lower()
    act = (action or "").strip().lower()
    kd = (kind or "").strip().lower()
    branch = (forced_branch or prior.branch or BRANCH_NONE).strip().lower()
    stuck = prior.stuck_cycle

    if "post_resolve_welcome" in src or act == "mode_buttons" or kd == "resolved":
        return Episode(
            branch=BRANCH_NONE,
            phase=PHASE_IDLE,
            awaiting=AWAITING_MODE,
            stuck_cycle=0,
        )
    if act == "feedback_buttons" or "feedback_buttons" in src:
        phase = PHASE_REFINED if "refined_unresolved" in src else PHASE_TROUBLESHOOTING
        return Episode(
            branch=BRANCH_TECH if branch == BRANCH_NONE else branch,
            phase=phase,
            awaiting=AWAITING_FEEDBACK,
            stuck_cycle=stuck,
        )
    if "unresolved_diagnostic" in src:
        return Episode(
            branch=BRANCH_TECH if branch == BRANCH_NONE else branch,
            phase=PHASE_DIAGNOSTIC,
            awaiting=AWAITING_NONE,
            stuck_cycle=max(stuck, 1),
        )
    if "platform_confirmation" in src:
        return Episode(
            branch=BRANCH_TECH,
            phase=PHASE_INTAKE,
            awaiting=AWAITING_PLATFORM_CONFIRM,
            stuck_cycle=0,
        )
    if "handoff_confirmation" in src:
        return Episode(
            branch=branch if branch != BRANCH_NONE else BRANCH_TECH,
            phase=PHASE_TROUBLESHOOTING,
            awaiting=AWAITING_HANDOFF_CONFIRM,
            stuck_cycle=stuck,
        )
    if "handoff" in src and "confirmation" not in src:
        return Episode(
            branch=branch if branch != BRANCH_NONE else BRANCH_TECH,
            phase=PHASE_CLOSING,
            awaiting=AWAITING_NONE,
            stuck_cycle=stuck,
        )
    if act == "platform_buttons" or "platform_list" in src:
        return Episode(
            branch=BRANCH_TECH,
            phase=PHASE_INTAKE,
            awaiting=AWAITING_PLATFORM_PICKER,
            stuck_cycle=0,
        )
    if act == "scam_os_buttons" or "scam_os" in src:
        return Episode(
            branch=BRANCH_SCAM,
            phase=PHASE_SCAM_FLOW,
            awaiting=AWAITING_SCAM_OS,
            stuck_cycle=0,
        )
    if act == "branch_clarify_buttons" or "branch_clarify" in src:
        # Stay on tech feedback arc — bank clarify must not drop awaiting feedback
        # (otherwise mid-arc "hi" falls into RAG and can bleed prior issues).
        return replace(
            prior,
            branch=BRANCH_TECH if prior.branch == BRANCH_NONE else prior.branch,
            phase=(
                prior.phase
                if prior.phase not in {PHASE_IDLE, "idle", ""}
                else PHASE_TROUBLESHOOTING
            ),
            awaiting=AWAITING_FEEDBACK,
        )
    if "scam_flow" in src or "scam_entry" in src or "scam_story_ask" in src:
        return Episode(
            branch=BRANCH_SCAM,
            phase=PHASE_SCAM_FLOW,
            awaiting=AWAITING_NONE,
            stuck_cycle=0,
        )
    if "platform_issue_prompt" in src:
        return Episode(
            branch=BRANCH_TECH,
            phase=PHASE_INTAKE,
            awaiting=AWAITING_NONE,
            stuck_cycle=0,
        )

    # Preserve prior episode when outbound is plain text mid-arc
    if branch != BRANCH_NONE:
        return replace(prior, branch=branch)
    return prior
