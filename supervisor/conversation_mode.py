"""Read persistent conversation routing; never call a model on message ingress."""

from ouroboros.conversation import PERSONAL, WORK


def select_conversation_mode(state: dict, chat_id: int, text: str = "", *, event_queue=None) -> str:
    """Legacy call signature retained for ingress; only the main dialogue changes mode."""
    if str(chat_id) != str(state.get("owner_chat_id")):
        return WORK
    return PERSONAL if state.get("personal_chat_active") else WORK
