"""Small pure helpers for keeping LLM context relevant and bounded."""


def bounded_history(history, char_budget: int):
    """Keep the newest complete messages within a predictable context budget."""
    selected = []
    remaining = max(0, int(char_budget))
    for item in reversed(history or []):
        content = str(item.get("content") or "")
        if selected and len(content) > remaining:
            break
        if not selected and len(content) > remaining:
            content = content[-remaining:] if remaining else ""
        selected.append({"role": item.get("role") or "user", "content": content})
        remaining -= len(content)
        if remaining <= 0:
            break
    return list(reversed(selected))


def needs_directory_context(text: str) -> bool:
    """Whether the LLM needs the cross-chat employee/chat directory."""
    lowered = (text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "сотрудник",
            "участник",
            "ответствен",
            "назнач",
            "кому написать",
            "напиши в чат",
            "отправь в чат",
            "сообщи в чат",
            "какие чаты",
            "в каких чатах",
            "кто занимается",
            "кто делает",
        )
    )
