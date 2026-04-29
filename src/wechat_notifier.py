"""PushPlus-based WeChat push (ported from paper_pulse, adapted to SelectedPaper).

Use:
    1. 扫码关注「pushplus 推送加」公众号
    2. 在 https://www.pushplus.plus 获取 token
    3. 环境变量 ``WECHAT_PUSHPLUS_TOKEN`` 填入

Optional env vars:
    WECHAT_PUSHPLUS_TOPIC    群组编码（一对多推送时填）
    WECHAT_PUSHPLUS_CHANNEL  可选：wechat / mail / webhook，默认 wechat
"""

from __future__ import annotations

import os
from datetime import date

import httpx
from loguru import logger

from src.models import SelectedPaper
from src.utils import sanitise_abstract

PUSHPLUS_API = "https://www.pushplus.plus/send"


_BUCKET_EMOJI = {
    "top_ranked": "🔥",
    "latest_hot": "🔥",
    "top_cited": "📈",
    "classic": "📚",
}


def _build_html_content(papers: list[SelectedPaper], today: date) -> str:
    """Build the HTML body PushPlus will render inside the WeChat message.

    We hand-build HTML rather than reusing the email template because WeChat's
    formatted-message renderer strips many CSS features — a compact, inline-only
    style is much more legible on phones.
    """

    lines = [
        f"<h2>📚 VLA 论文日报 · {today.isoformat()}</h2>",
        f"<p><strong>共 {len(papers)} 篇</strong>："
        + " / ".join(f"{_BUCKET_EMOJI.get(p.bucket, '')}{p.bucket_label_cn}" for p in papers)
        + "</p>",
        "<hr>",
    ]
    for i, sp in enumerate(papers, 1):
        emoji = _BUCKET_EMOJI.get(sp.bucket, "📄")
        lines.append(
            f'<h3>{i}. {emoji} [{sp.bucket_label_cn}] '
            f'<a href="{sp.paper.best_url}">{sp.paper.title}</a></h3>'
        )
        # Venue / affiliation badges — make them visually prominent at the top.
        badge_parts: list[str] = []
        venue_text = sp.paper.venue or ""
        if venue_text:
            badge_parts.append(
                f"<span style='background:#eef6ee;color:#2e7d32;padding:2px 8px;"
                f"border-radius:4px;font-size:12px;font-weight:600'>🏛 {venue_text}"
                f"{' ' + str(sp.paper.year) if sp.paper.year else ''}</span>"
            )
        elif sp.paper.year:
            badge_parts.append(
                f"<span style='background:#eef0f6;color:#4a5578;padding:2px 8px;"
                f"border-radius:4px;font-size:12px;font-weight:600'>📄 {sp.paper.year}</span>"
            )
        if sp.lab_label:
            badge_parts.append(
                f"<span style='background:#fde4e4;color:#b71c1c;padding:2px 8px;"
                f"border-radius:4px;font-size:12px;font-weight:600'>"
                f"🏢 {sp.lab_label}</span>"
            )
        elif sp.paper.primary_affiliation:
            badge_parts.append(
                f"<span style='background:#fff4e5;color:#bf6a00;padding:2px 8px;"
                f"border-radius:4px;font-size:12px;font-weight:600'>"
                f"🔬 {sp.paper.primary_affiliation}</span>"
            )
        if sp.notable_author_matches:
            badge_parts.append(
                f"<span style='background:#e8eefc;color:#1a237e;padding:2px 8px;"
                f"border-radius:4px;font-size:12px;font-weight:600'>"
                f"⭐ {', '.join(sp.notable_author_matches)}</span>"
            )
        if badge_parts:
            lines.append("<p>" + " ".join(badge_parts) + "</p>")

        meta_parts: list[str] = []
        if sp.paper.authors:
            top3 = ", ".join(sp.paper.authors[:3])
            if len(sp.paper.authors) > 3:
                top3 += " 等"
            meta_parts.append(top3)
        meta_parts.append(f"引用 {sp.paper.citation_count}")
        lines.append(f"<p><em>{' · '.join(meta_parts)}</em></p>")
        boost_text = ""
        if sp.scores.lab_boost > 0:
            boost_text += f" · lab +{sp.scores.lab_boost:.1f}"
        if sp.scores.notable_author_boost > 0:
            boost_text += f" · ⭐ +{sp.scores.notable_author_boost:.1f}"
        lines.append(
            "<p style='font-size:12px;color:#555'>"
            f"综合 {sp.scores.composite:.2f} · 相关 {sp.scores.relevance:.2f} · "
            f"venue {sp.scores.venue:.1f} · 新鲜 {sp.scores.freshness:.1f} · "
            f"速度 {sp.scores.velocity:.2f}{boost_text}"
            "</p>"
        )

        # Only render the 6 fields if the LLM actually produced non-empty text;
        # otherwise fall back to the sanitised abstract so users never see
        # bare section headers with empty content.
        has_llm_content = bool(
            sp.summary and not sp.summary.failed and (sp.summary.direction or "").strip()
        )
        if has_llm_content:
            lines.append(f"<p>🔹 <strong>方向:</strong> {sanitise_abstract(sp.summary.direction)}</p>")
            lines.append(f"<p>🔹 <strong>问题:</strong> {sanitise_abstract(sp.summary.core_problem)}</p>")
            lines.append(f"<p>🔹 <strong>方法:</strong> {sanitise_abstract(sp.summary.key_method)}</p>")
            lines.append(f"<p>🔹 <strong>结论:</strong> {sanitise_abstract(sp.summary.conclusion)}</p>")
            lines.append(f"<p>🔹 <strong>局限:</strong> {sanitise_abstract(sp.summary.limitation)}</p>")
            lines.append(
                "<p style='background:#fff8e1;padding:6px;border-left:3px solid #ffb300'>"
                f"↪ <strong>Smoothness 关联:</strong> "
                f"{sanitise_abstract(sp.summary.relevance_to_smoothness)}"
                "</p>"
            )
        elif sp.paper.abstract:
            # Fallback: show a sanitised, length-bounded abstract. WeChat / PushPlus
            # will truncate very long bodies; cap at ~500 chars to stay safe.
            clean = sanitise_abstract(sp.paper.abstract)
            snippet = clean[:500]
            if len(clean) > 500:
                snippet += " ..."
            lines.append(f"<p><em>{snippet}</em></p>")

        if i < len(papers):
            lines.append("<hr>")

    lines.append(
        "<p style='color:#999;font-size:12px;margin-top:16px'>"
        "由 vla-paper-digest 自动生成 · Semantic Scholar / OpenReview / arXiv"
        "</p>"
    )
    return "\n".join(lines)


def send_wechat(papers: list[SelectedPaper], today: date) -> bool:
    """Send via PushPlus. Returns True on success, False otherwise (never raises)."""

    token = os.getenv("WECHAT_PUSHPLUS_TOKEN", "").strip()
    if not token:
        logger.info("WECHAT_PUSHPLUS_TOKEN not set — skip WeChat push")
        return False

    title = f"[VLA 论文] {today.isoformat()} · 今日 {len(papers)} 篇"
    content = _build_html_content(papers, today)

    payload: dict = {
        "token": token,
        "title": title,
        "content": content,
        "template": "html",
    }
    topic = os.getenv("WECHAT_PUSHPLUS_TOPIC", "").strip()
    if topic:
        payload["topic"] = topic
    channel = os.getenv("WECHAT_PUSHPLUS_CHANNEL", "").strip()
    if channel:
        payload["channel"] = channel

    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.post(PUSHPLUS_API, json=payload)
            data = resp.json()
    except Exception as exc:  # network / json errors degrade silently
        logger.error(f"WeChat push exception: {exc}")
        return False

    if data.get("code") == 200:
        logger.info("WeChat push sent successfully (PushPlus)")
        return True

    logger.error(
        f"WeChat push failed: code={data.get('code')} msg={data.get('msg')}"
    )
    return False
