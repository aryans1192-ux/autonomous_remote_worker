"""Shared fake LLM for tests and the offline eval harness.

FakeMessagesListChatModel does not implement bind_tools; BindableFakeChatModel
no-ops it so the graph's agent_node can bind tools without a real provider.
"""
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel


class BindableFakeChatModel(FakeMessagesListChatModel):
    """A scripted fake chat model that accepts bind_tools without raising."""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self
