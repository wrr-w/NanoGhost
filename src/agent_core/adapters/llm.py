import hashlib
import math
import os
from typing import List, Iterator, Optional

from openai import OpenAI

import logging

from agent_core.interfaces import LLMPort, LLMResponse
from agent_core.tool.models import ToolCall


# ── 内置 fallback embedding（纯 Python，无额外依赖）────────
# 基于 char bigram + trigram 的 hash 分桶向量
# 精度不如语义 embedding，但保证永远可用且确定性


def _ngrams(text: str) -> dict:
    """提取 char 2-gram + 3-gram，返回频率 dict。"""
    text = (text or "").lower().strip()
    if not text:
        return {}
    counts: dict = {}
    for n in (2, 3):
        for i in range(len(text) - n + 1):
            gram = text[i:i + n]
            counts[gram] = counts.get(gram, 0) + 1
    return counts


def _stable_bucket(gram: str, dim: int) -> int:
    """基于 MD5 的确定性 hash 分桶（跨进程稳定）。"""
    return int(hashlib.md5(gram.encode("utf-8")).hexdigest(), 16) % dim


def _builtin_embed(text: str) -> List[float]:
    """基于 char 2-gram + 3-gram 的内置 embedding（256 维）。
    
    纯 Python 实现，不依赖任何第三方库。
    使用 MD5 确定性 hash，跨进程/跨机器稳定。
    """
    dim = 256
    ngrams = _ngrams(text)
    if not ngrams:
        return [0.0] * dim
    vec = [0.0] * dim
    for gram, count in ngrams.items():
        bucket = _stable_bucket(gram, dim)
        vec[bucket] += count
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def _probe_fastembed() -> Optional[type]:
    try:
        from fastembed import TextEmbedding
        return TextEmbedding
    except ImportError:
        return None


def _probe_sentence_transformers() -> Optional[type]:
    try:
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer
    except ImportError:
        return None


class OpenAILLM(LLMPort):
    def __init__(self):
        self.client = OpenAI(
            api_key=os.getenv("LLM_API_KEY"),
            base_url=os.getenv("LLM_BASE_URL"),
        )
        self.model = os.getenv("LLM_MODEL") or "gpt-4o"

        # Embedding 配置：EMBED_MODEL 为空 → 使用本地
        self.embed_model = (os.getenv("EMBED_MODEL") or "").strip()
        self.embed_base_url = (os.getenv("EMBED_BASE_URL") or "").strip()
        self.embed_api_key = (os.getenv("EMBED_API_KEY") or "").strip()

        self._local_embed_fn = None
        self._embed_client = None

    def _init_local_embed(self):
        if self._local_embed_fn is not None:
            return

        # 1. fastembed（轻量 ONNX，需 pip install fastembed）
        fb_cls = _probe_fastembed()
        if fb_cls is not None:
            model_name = os.getenv("EMBED_LOCAL_MODEL") or "BAAI/bge-small-zh-v1.5"
            try:
                model = fb_cls(model_name)
                self._local_embed_fn = lambda text: list(model.embed(text))[0]
                logging.getLogger("agent_core").info(
                    f"[Embedding] 使用 fastembed 模型={model_name}"
                )
                return
            except Exception:
                pass

        # 2. sentence-transformers（需 pip install sentence-transformers）
        st_cls = _probe_sentence_transformers()
        if st_cls is not None:
            model_name = os.getenv("EMBED_LOCAL_MODEL") or "all-MiniLM-L6-v2"
            try:
                model = st_cls(model_name)
                self._local_embed_fn = lambda text: model.encode(text).tolist()
                logging.getLogger("agent_core").info(
                    f"[Embedding] 使用 sentence-transformers 模型={model_name}"
                )
                return
            except Exception:
                pass

        # 3. 内置 fallback（纯 Python，零依赖）
        logging.getLogger("agent_core").info(
            "[Embedding] 使用内置 ngram fallback（无外部依赖）"
        )
        self._local_embed_fn = _builtin_embed

    def _build_embed_client(self) -> OpenAI:
        if self._embed_client is not None:
            return self._embed_client
        base = self.embed_base_url or os.getenv("LLM_BASE_URL") or ""
        key = self.embed_api_key or os.getenv("LLM_API_KEY") or ""
        self._embed_client = OpenAI(api_key=key, base_url=base)
        return self._embed_client

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
        reasoning_content = getattr(choice.message, "reasoning_content", None)
        tool_calls = None
        if choice.message.tool_calls:
            tool_calls = [ToolCall.from_openai(tc) for tc in choice.message.tool_calls]

        return LLMResponse(
            content=content,
            reasoning_content=reasoning_content,
            tool_calls=tool_calls,
        )

    def embed(self, text: str) -> List[float]:
        text = (text or "").strip()
        if not text:
            return []

        # 用户配置了远程 embedding 模型 → 调 API
        if self.embed_model:
            try:
                client = self._build_embed_client()
                response = client.embeddings.create(
                    model=self.embed_model,
                    input=text,
                )
                return response.data[0].embedding
            except Exception:
                logging.getLogger("agent_core").warning(
                    f"[Embedding] 远程 API 失败 (model={self.embed_model})，降级到本地"
                )

        # 本地 embedding
        self._init_local_embed()
        try:
            return self._local_embed_fn(text)
        except Exception as e:
            logging.getLogger("agent_core").error(f"[Embedding] 本地 embedding 失败: {e}")
            return []
