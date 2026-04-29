"""GPT-4o-mini structured summariser (6 fields, Chinese output).

The prompt is tuned for a PhD audience: we demand conciseness, ban filler
phrases, and require an explicit "relevance to VLA smoothness" line so the
reader can immediately judge whether to read further.
"""

from __future__ import annotations

import json

from loguru import logger
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from src.models import LLMSummary, Paper

SYSTEM_PROMPT = """你是一位 VLA（Vision-Language-Action）方向的资深论文分析师。请阅读论文的标题和摘要，输出 **严格的 JSON 对象**，字段如下：
{
  "direction": "论文方向，1 句，<=20 字",
  "core_problem": "核心问题，1 句，<=30 字",
  "key_method": "核心方法，1-2 句，<=50 字",
  "conclusion": "主要结论，1-2 句，<=50 字",
  "limitation": "未解决问题或局限，1-2 句，<=50 字",
  "relevance_to_smoothness": "与 VLA 路径生成 smoothness（动力学 jerk/acceleration + 语义 action chunk 一致性）的关联，1 句，<=30 字。若无关写 '间接相关' 或 '作为背景知识'"
}

硬性要求：
- 纯中文；客观、精准、**不使用套话**（如"具有广阔前景"、"值得研究"）。
- **不要**复述摘要；只输出判断与关键信息。
- **严格输出 JSON**，不加任何解释、前后缀、markdown。
- 如果摘要太短或缺失，保留字段但简要写 "信息不足" 类内容。"""


def _build_user_prompt(paper: Paper) -> str:
    abstract = paper.abstract or "(abstract missing)"
    venue = f"{paper.venue} {paper.year}" if paper.venue else f"year={paper.year}"
    return (
        f"Title: {paper.title}\n"
        f"Venue: {venue}\n"
        f"Abstract:\n{abstract}"
    )


def _is_reasoning_model(model: str) -> bool:
    """GPT-5 and o-series are reasoning models that (a) require
    ``max_completion_tokens`` instead of ``max_tokens``, (b) don't accept
    a custom ``temperature``, and (c) spend most tokens on internal
    reasoning before producing any output. They need a **much** larger
    token budget — empirically GPT-5 burns 1000-2000 reasoning tokens
    for this task."""

    m = (model or "").lower()
    return m.startswith(("gpt-5", "o1", "o3", "o4"))


class _EmptyCompletion(Exception):
    """Raised when the API returns an empty completion (usually because
    reasoning tokens exhausted ``max_completion_tokens``). Triggers a
    tenacity retry so the next attempt can decide whether to grow the
    budget or give up."""


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20), reraise=True)
def _call_llm(client: OpenAI, model: str, paper: Paper) -> dict:
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(paper)},
        ],
        "response_format": {"type": "json_object"},
    }
    if _is_reasoning_model(model):
        # Big enough to fit GPT-5 reasoning (~1500) + JSON output (~500).
        kwargs["max_completion_tokens"] = 6000
    else:
        kwargs["max_tokens"] = 800
        kwargs["temperature"] = 0.2

    response = client.chat.completions.create(**kwargs)
    choice = response.choices[0]
    content = (choice.message.content or "").strip()
    if not content:
        # Almost certainly ``finish_reason == 'length'`` on a reasoning model:
        # raise so tenacity retries; on final attempt the caller degrades.
        reason = getattr(choice, "finish_reason", "unknown")
        logger.warning(
            f"LLM returned empty content (finish_reason={reason}) for "
            f"'{paper.title[:50]}'; will retry."
        )
        raise _EmptyCompletion(f"empty content, finish_reason={reason}")
    return json.loads(content)


def summarize_paper(client: OpenAI, model: str, paper: Paper) -> LLMSummary:
    """Produce a structured summary; on any failure, return a failed fallback."""

    try:
        data = _call_llm(client, model, paper)
    except Exception as exc:  # broad on purpose: LLM/network errors → degrade
        logger.error(f"LLM summary failed for '{paper.title[:60]}': {exc}")
        return LLMSummary(failed=True, error=str(exc))

    return LLMSummary(
        direction=str(data.get("direction", ""))[:80],
        core_problem=str(data.get("core_problem", ""))[:120],
        key_method=str(data.get("key_method", ""))[:200],
        conclusion=str(data.get("conclusion", ""))[:200],
        limitation=str(data.get("limitation", ""))[:200],
        relevance_to_smoothness=str(data.get("relevance_to_smoothness", ""))[:120],
    )


def summarize_batch(api_key: str, model: str, papers: list[Paper]) -> list[LLMSummary]:
    client = OpenAI(api_key=api_key)
    out: list[LLMSummary] = []
    for i, paper in enumerate(papers, 1):
        logger.info(f"[LLM] ({i}/{len(papers)}) summarising: {paper.title[:70]}")
        out.append(summarize_paper(client, model, paper))
    return out
