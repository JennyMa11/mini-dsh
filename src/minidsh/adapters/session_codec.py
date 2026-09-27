"""Shared, validated serialization of provider-neutral session messages."""

from pydantic import BaseModel, ConfigDict, Field

from minidsh.domain import JsonValue, Message


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StoredCall(StrictModel):
    call_id: str
    name: str
    arguments: dict[str, JsonValue]


class StoredMessage(StrictModel):
    role: str
    content: str
    name: str | None
    tool_call_id: str | None
    tool_calls: list[StoredCall] = Field(default_factory=list)
    reasoning_content: str | None = None


def message_to_json(message: Message) -> dict[str, object]:
    """Serialize a message without provider-specific wire fields."""
    return {
        "role": message.role.value,
        "content": message.content,
        "name": message.name,
        "reasoning_content": message.reasoning_content,
        "tool_call_id": message.tool_call_id,
        "tool_calls": [
            {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}
            for call in message.tool_calls
        ],
    }
