import html
import logging
import os
import re
import smtplib
import ssl
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

LOGGER = logging.getLogger(__name__)

# Only a bare {name} is a placeholder. Deliberately no support for dots, indexes,
# conversions or format specs - see fill_placeholders().
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

PLACEHOLDERS = ("participant_name", "event_name")


def fill_placeholders(template: str, values: dict, escape_html: bool = False) -> str:
    """
    Substitute {participant_name} / {event_name} into an admin-authored template.

    This deliberately does not use str.format(). Two reasons:

    1. str.format() on an operator-supplied string allows attribute traversal -
       "{event_name.__class__.__init__.__globals__}" walks straight out of the
       template and into process internals.
    2. str.format() raises on any brace it does not understand, so a single CSS
       rule in an HTML body ("body { margin: 0 }") aborts that participant's send
       with a KeyError.

    Unknown placeholders and stray braces are left exactly as written.
    """
    def substitute(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)
        value = str(values[key])
        return html.escape(value) if escape_html else value

    return _PLACEHOLDER_RE.sub(substitute, template or "")


class EmailSender:
    def __init__(self):
        self.smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        self.username = os.environ.get("SMTP_USER", "")
        self.password = os.environ.get("SMTP_PASS", "")
        self.from_address = os.environ.get("SMTP_FROM", self.username)
        self.from_name = os.environ.get("SMTP_FROM_NAME", "Certificate Generator")
        self.use_starttls = str(os.environ.get("SMTP_STARTTLS", "true")).lower() == "true"
        self.use_ssl = str(os.environ.get("SMTP_SSL", "false")).lower() == "true"

    def send_certificate(self, participant_email: str, participant_name: str, event_name: str, certificate_path: str,
                         subject_template: str = None, plain_body_template: str = None, html_body_template: str = None) -> None:
        try:
            message = MIMEMultipart("alternative")
            values = {"participant_name": participant_name, "event_name": event_name}

            # Format subject
            subject = subject_template or "Your Certificate for {event_name}"
            message["Subject"] = fill_placeholders(subject, values)

            message["From"] = formataddr((self.from_name, self.from_address))
            message["To"] = participant_email

            # Add plain text version
            plain_tmpl = plain_body_template or "Hello {participant_name},\n\nAttached is your certificate for {event_name}. Congratulations!\n\nBest regards,\nThe Organizers"
            plain_body = fill_placeholders(plain_tmpl, values)
            text_part = MIMEText(plain_body, "plain", "utf-8")
            message.attach(text_part)

            # Add HTML version
            html_tmpl = html_body_template or """
            <html>
                <body>
                    <p>Hello <b>{participant_name}</b>,</p>
                    <p>Attached is your certificate for <b>{event_name}</b>. Congratulations!</p>
                    <p>Best regards,<br>The Organizers</p>
                </body>
            </html>
            """
            # Escaped here: a name like "Tom & Jerry" would otherwise emit broken markup.
            html_body = fill_placeholders(html_tmpl, values, escape_html=True)
            html_part = MIMEText(html_body, "html", "utf-8")
            message.attach(html_part)

            # Attach certificate
            cert_file = Path(certificate_path)
            if not cert_file.exists():
                raise FileNotFoundError(f"Certificate file not found: {certificate_path}")

            certificate_bytes = cert_file.read_bytes()
            image_part = MIMEImage(certificate_bytes, name=cert_file.name)
            image_part.add_header("Content-Disposition", "attachment", filename=cert_file.name)
            message.attach(image_part)

            LOGGER.debug("Email prepared for %s (subject: %s)", participant_email, message["Subject"])

            if self.use_ssl:
                self._send_via_ssl(message)
            else:
                self._send_via_standard(message)

        except (FileNotFoundError, RuntimeError):
            raise
        except Exception as exc:
            raise RuntimeError(f"Failed to prepare email for {participant_email}: {exc}") from exc

    def _send_via_standard(self, message: MIMEMultipart) -> None:
        context = ssl.create_default_context()
        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as smtp:
                smtp.set_debuglevel(0)
                smtp.ehlo()
                if self.use_starttls:
                    smtp.starttls(context=context)
                    smtp.ehlo()
                self._login_if_needed(smtp)
                smtp.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            raise RuntimeError(f"SMTP authentication failed: {exc}") from exc
        except smtplib.SMTPException as exc:
            raise RuntimeError(f"SMTP error: {exc}") from exc
        except OSError as exc:
            raise RuntimeError(f"Network error: {exc}") from exc

    def _send_via_ssl(self, message: MIMEMultipart) -> None:
        context = ssl.create_default_context()
        try:
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=context, timeout=30) as smtp:
                smtp.set_debuglevel(0)
                self._login_if_needed(smtp)
                smtp.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            raise RuntimeError(f"SMTP authentication failed: {exc}") from exc
        except smtplib.SMTPException as exc:
            raise RuntimeError(f"SMTP error: {exc}") from exc
        except OSError as exc:
            raise RuntimeError(f"Network error: {exc}") from exc

    def _login_if_needed(self, smtp: smtplib.SMTP) -> None:
        if self.username:
            try:
                smtp.login(self.username, self.password)
                LOGGER.debug("SMTP authentication successful for %s", self.username)
            except smtplib.SMTPAuthenticationError as exc:
                raise RuntimeError(f"Invalid SMTP credentials: {exc}") from exc
