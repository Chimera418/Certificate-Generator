import os
import smtplib
import ssl
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

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
            
            # Format subject
            subject = subject_template or "Your Certificate for {event_name}"
            message["Subject"] = subject.format(participant_name=participant_name, event_name=event_name)
            
            message["From"] = formataddr((self.from_name, self.from_address))
            message["To"] = participant_email

            # Add plain text version
            plain_tmpl = plain_body_template or "Hello {participant_name},\n\nAttached is your certificate for {event_name}. Congratulations!\n\nBest regards,\nThe Organizers"
            plain_body = plain_tmpl.format(participant_name=participant_name, event_name=event_name)
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
            html_body = html_tmpl.format(participant_name=participant_name, event_name=event_name)
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
            
            if self.use_ssl:
                self._send_via_ssl(message)
            else:
                self._send_via_standard(message)
                
        except FileNotFoundError:
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
        except Exception as exc:
            raise RuntimeError(f"SMTP error: {exc}") from exc

    def _send_via_ssl(self, message: MIMEMultipart) -> None:
        context = ssl.create_default_context()
        try:
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=context, timeout=30) as smtp:
                smtp.set_debuglevel(0)
                self._login_if_needed(smtp)
                smtp.send_message(message)
        except Exception as exc:
            raise RuntimeError(f"SMTP error: {exc}") from exc

    def _login_if_needed(self, smtp: smtplib.SMTP) -> None:
        if self.username and self.password:
            try:
                smtp.login(self.username, self.password)
            except Exception as exc:
                raise RuntimeError(f"Invalid SMTP credentials: {exc}") from exc
