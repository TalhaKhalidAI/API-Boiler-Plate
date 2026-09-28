from email.message import EmailMessage
from pathlib import Path
from typing import Optional, Any
import aiosmtplib

from jinja2 import (
    Environment,
    FileSystemLoader,
    select_autoescape,
    TemplateNotFound,
    TemplateSyntaxError,
)

from App.core.settings import settings
from App.core.LoggingInit import get_core_logger

logger = get_core_logger(__name__)


# ============================================================================
# Jinja environment — compiled once at import, reused for all sends
# ============================================================================
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "email"

_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "htm", "xml"]),   # auto-escape HTML
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


class EmailSender:
    """
    Async SMTP sender supporting 4 formats:

      1. send(..., body="plain text")                    → plain only
      2. send(..., body="text", html="<p>hi</p>")        → plain + html (multipart/alternative)
      3. send_template(..., template="x.txt", ...)       → jinja-rendered text only
      4. send_template(..., template="x.html", ...)      → jinja-rendered html + auto plain fallback

    Jinja templates live in App/templates/email/. Extension determines behavior:
      - .html / .htm  → rendered as HTML; a plain-text fallback is auto-generated
      - .txt / .text  → rendered as plain text only
    """

    def __init__(self):
        self.host = settings.EMAIL_SMTP_HOST
        self.port = settings.EMAIL_SMTP_PORT
        self.username = settings.EMAIL_SMTP_USERNAME
        self.password = settings.EMAIL_SMTP_PASSWORD.get_secret_value()
        self.from_addr = settings.EMAIL_FROM
        self.from_name = settings.EMAIL_FROM_NAME
        self.use_tls = settings.EMAIL_USE_TLS
        self.use_ssl = settings.EMAIL_USE_SSL
        self.timeout = settings.EMAIL_TIMEOUT

    # ------------------------------------------------------------------
    # Low-level send — handles both plain and multipart
    # ------------------------------------------------------------------
    async def send(
        self,
        to: str,
        subject: str,
        body: str,
        html: Optional[str] = None,
    ) -> bool:
        """
        Send an email.

        Args:
            body: plain-text body (always required — this becomes the
                  multipart/alternative text part, or the only part if
                  html is None)
            html: optional HTML body. If provided, email is multipart with
                  both text and HTML parts.
        """
        if not settings.EMAIL_ENABLED:
            logger.warning(f"Email disabled — skipping send to {to}")
            return False

        msg = EmailMessage()
        msg["From"] = f"{self.from_name} <{self.from_addr}>"
        msg["To"] = to
        msg["Subject"] = subject

        # Plain text first (bottom of multipart), then HTML alternative.
        # Email clients pick the richest part they support.
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")

        try:
            await aiosmtplib.send(
                msg,
                hostname=self.host,
                port=self.port,
                username=self.username or None,
                password=self.password or None,
                use_tls=self.use_ssl,
                start_tls=self.use_tls,
                timeout=self.timeout,
            )
            logger.info(f"Email sent: {subject} → {to}")
            return True

        except aiosmtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP auth failed (check app password): {e}")
            return False
        except aiosmtplib.SMTPRecipientsRefused as e:
            logger.warning(f"Recipient refused: {to} — {e}")
            return False
        except aiosmtplib.SMTPTimeoutError as e:
            logger.error(f"SMTP timeout sending to {to}: {e}")
            return False
        except Exception as e:
            logger.exception(f"Email send failed to {to}: {e}")
            return False

    # ------------------------------------------------------------------
    # Jinja rendering
    # ------------------------------------------------------------------
    def render_template(self, template_name: str, context: dict[str, Any]) -> Optional[str]:
        """
        Render a Jinja template. Returns None on any failure (already logged).
        """
        try:
            template = _jinja_env.get_template(template_name)
        except TemplateNotFound:
            logger.error(f"Email template not found: {template_name}")
            return None
        except TemplateSyntaxError as e:
            logger.error(f"Template syntax error in {template_name}: line {e.lineno} — {e.message}")
            return None
        except Exception:
            logger.exception(f"Template lookup failed: {template_name}")
            return None

        try:
            return template.render(**context)
        except Exception:
            logger.exception(f"Template render failed: {template_name}")
            return None

    # ------------------------------------------------------------------
    # High-level: send a Jinja template
    # ------------------------------------------------------------------
    async def send_template(
        self,
        to: str,
        subject: str,
        template_name: str,
        context: Optional[dict[str, Any]] = None,
        *,
        plain_template_name: Optional[str] = None,
        fallback_text: Optional[str] = None,
    ) -> bool:
        """
        Render a Jinja template and send it.

        Args:
            template_name: e.g. "otp.html" or "otp.txt"
            context: variables to pass into the template
            plain_template_name: optional separate .txt template to use as
                the plain-text part when template_name is HTML. If omitted,
                fallback_text is used, or a generic fallback.
            fallback_text: plain-text body to use if the template is HTML
                and no plain_template_name is provided.

        Behavior:
            - template_name ends with .html/.htm:
                Rendered as HTML. Plain part = plain_template_name render,
                or fallback_text, or a generic "open in HTML client" string.
            - template_name ends with .txt/.text:
                Rendered as plain text only.
            - Other extensions:
                Treated as plain text (safest default).
        """
        context = context or {}

        # Guard: template_name must not escape the template dir
        if ".." in template_name or template_name.startswith("/"):
            logger.error(f"Refusing suspicious template path: {template_name}")
            return False

        rendered = self.render_template(template_name, context)
        if rendered is None:
            return False

        name_lower = template_name.lower()
        is_html = name_lower.endswith((".html", ".htm"))

        # ---- HTML template: build plain fallback ----
        if is_html:
            plain_body: Optional[str] = None

            # 1. Try an explicit .txt template if provided
            if plain_template_name:
                plain_body = self.render_template(plain_template_name, context)

            # 2. Fall back to caller-supplied text
            if plain_body is None:
                plain_body = fallback_text

            # 3. Last resort: derive a minimal text body from the subject
            if plain_body is None:
                plain_body = f"{subject}\n\n(This email is best viewed in an HTML-capable client.)"

            return await self.send(to=to, subject=subject, body=plain_body, html=rendered)

        # ---- Plain text template (.txt, .text, or anything else) ----
        return await self.send(to=to, subject=subject, body=rendered)


# Singleton — imported by services
email_sender = EmailSender()