"""SMTP email delivery. Reuses the generic SSL/STARTTLS pattern from paper_pulse.

Environment variables (sourced from :mod:`src.config`):
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_TO

Why not just Gmail? The same script needs to work with 163/QQ/Gmail and the
user already confirmed the paper_pulse SMTP implementation works well for
their account — we mirror it here.
"""

from __future__ import annotations

import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from loguru import logger

from src.config import DATA_DIR, TEMPLATE_DIR, RuntimeSettings
from src.models import SelectedPaper
from src.utils import sanitise_abstract


def _jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    # Expose LaTeX / markdown scrubber as a Jinja filter so the template
    # can opt into it on exactly the fields we want cleaned.
    env.filters["sanitise"] = sanitise_abstract
    return env


def render_digest_html(papers: list[SelectedPaper], *, today: date) -> str:
    env = _jinja_env()
    template = env.get_template("email_template.html")
    return template.render(
        papers=papers,
        date_str=today.strftime("%Y-%m-%d"),
        date_cn=today.strftime("%Y年%m月%d日"),
    )


def render_plaintext(papers: list[SelectedPaper], *, today: date) -> str:
    lines = [f"VLA Paper Digest — {today.isoformat()}\n"]
    for i, sp in enumerate(papers, 1):
        lines.append(f"\n[{sp.bucket_label_cn}] {i}. {sp.paper.title}")
        lines.append(f"  {sp.paper.best_url}")
        lines.append(
            f"  venue={sp.paper.venue} year={sp.paper.year} "
            f"cites={sp.paper.citation_count} composite={sp.scores.composite:.2f}"
        )
        if sp.summary and not sp.summary.failed:
            lines.append(f"  方向: {sp.summary.direction}")
            lines.append(f"  问题: {sp.summary.core_problem}")
            lines.append(f"  方法: {sp.summary.key_method}")
            lines.append(f"  结论: {sp.summary.conclusion}")
    return "\n".join(lines)


def send_digest_email(
    papers: list[SelectedPaper],
    settings: RuntimeSettings,
    *,
    today: date,
    subject_prefix: str = "[每日论文推送]",
) -> bool:
    """Build the MIME message and send via SMTP. On failure, persist the HTML."""

    html = render_digest_html(papers, today=today)
    plain = render_plaintext(papers, today=today)
    subject = f"{subject_prefix} {today.isoformat()} · 今日 {len(papers)} 篇"

    if not settings.has_smtp:
        logger.warning("SMTP not configured — writing HTML to disk instead")
        _persist_failed(html, today)
        return False

    msg = MIMEMultipart("alternative")
    msg["From"] = settings.smtp_user
    msg["To"] = settings.email_to
    msg["Subject"] = subject
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    recipients = [addr.strip() for addr in settings.email_to.split(",") if addr.strip()]
    try:
        if settings.smtp_port == 465:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
            server.starttls()
        server.login(settings.smtp_user, settings.smtp_password)
        server.sendmail(settings.smtp_user, recipients, msg.as_string())
        server.quit()
        logger.info(f"email sent to {settings.email_to}")
        return True
    except Exception as exc:
        logger.error(f"email send failed: {exc}")
        _persist_failed(html, today)
        return False


def _persist_failed(html: str, today: date) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"failed_digest_{today.isoformat()}.html"
    path.write_text(html, encoding="utf-8")
    logger.info(f"HTML fallback written: {path}")
    return path
