"""The new-story email to the owner, sent through the owner's own Gmail over SMTP.

Ported from the Nuxt app's shared/utils/notify.ts when the social agent took over
sharing new stories: the agent writes the subject and a short note, and this module
builds the rest of the email (cover, story text, link) and sends it. It uses a Gmail
App Password, not the account password, since Google blocks plain-password SMTP.
Errors raise SocialError, like the platform posts, so the agent can read them.
"""

import asyncio
import smtplib
import socket
from email.message import EmailMessage
from html import escape

from .social import SocialError

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
TIMEOUT_SECONDS = 20.0


class _SMTP4(smtplib.SMTP_SSL):
    """SMTP over TLS on IPv4 only: smtp.gmail.com resolves to IPv6 first on the
    owner's machine, where IPv6 isn't routable (same fix as app/social.py)."""

    def _get_socket(self, host, port, timeout):
        address = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
        sock = socket.create_connection(address, timeout, self.source_address)
        return self.context.wrap_socket(sock, server_hostname=self._host)


def story_email_html(story: dict, story_url: str, image_url: str, note: str) -> str:
    paragraphs = "\n".join(f"<p>{escape(p)}</p>" for p in story.get("body") or [])
    note_html = "".join(f"<p>{escape(line)}</p>" for line in note.splitlines() if line.strip())
    return f"""
<div style="font-family:Georgia,serif;max-width:600px;color:#1B3B5F;">
  {note_html}
  <img src="{escape(image_url)}" alt="{escape(story['title'])}" width="600"
       style="display:block;width:100%;max-width:600px;height:auto;border-radius:16px;margin:16px 0;" />
  <h2 style="margin-bottom:4px;">{escape(story['title'])} {escape(story.get('emoji') or '')}</h2>
  <p style="color:#5B5347;"><em>{escape(story['excerpt'])}</em></p>
  <p style="font-size:13px;color:#5B5347;">
    Category: {escape(story['category'])} &middot; Age range: {escape(story['age_range'])}
    &middot; {story.get('read_time_minutes') or 2} min read
  </p>
  <hr style="border:none;border-top:1px solid #E9DAB8;margin:16px 0;" />
  {paragraphs}
  <p style="margin-top:24px;"><a href="{escape(story_url)}">Read it live on Kidstory &rarr;</a></p>
</div>"""


async def send_email(user: str, app_password: str, *, to: list[str], subject: str, html: str, text: str) -> str:
    """Sends one email from the owner's Gmail. Returns the Message-ID."""
    msg = EmailMessage()
    msg["From"] = f"Kidstory <{user}>"
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    def send() -> None:
        with _SMTP4(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT_SECONDS) as smtp:
            smtp.login(user, app_password)
            smtp.send_message(msg)

    try:
        await asyncio.to_thread(send)
    except (smtplib.SMTPException, OSError) as exc:
        raise SocialError(f"Gmail did not send the email: {exc}") from exc
    return str(msg.get("Message-ID") or "sent")
