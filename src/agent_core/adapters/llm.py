import os
from typing import List, Iterator

from openai import OpenAI

from agent_core.interfaces import LLMPort, LLMResponse
from agent_core.tool.models import ToolCall


class OpenAILLM(LLMPort):
    def __init__(self):
        self.client = OpenAI(
            api_key=os.getenv("LLM_API_KEY"),
            base_url=os.getenv("LLM_BASE_URL"),
        )
        self.model = os.getenv("LLM_MODEL") or "gpt-4o"
        self.embed_model = os.getenv("EMBED_MODEL") or "text-embedding-3-small"

    def stream_chat(self, messages: List[dict], temperature: float = 0.1) -> Iterator[str]:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            stream=True,
        )
        for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def chat(self, messages, temperature=0.1, tools=None):
        kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=temperature,
        )
        if tools:
            kwargs["tools"] = tools

        response = self.client.chat.completions.create(**kwargs)
        choice = response.choices[0]

        content = choice.message.content
        tool_calls = None
        if choice.message.tool_calls:
            tool_calls = [ToolCall.from_openai(tc) for tc in choice.message.tool_calls]

        return LLMResponse(content=content, tool_calls=tool_calls)

    def embed(self, text: str) -> List[float]:
        response = self.client.embeddings.create(
            model=self.embed_model,
            input=text,
        )
        return response.data[0].embedding
