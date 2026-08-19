"""
Email template placeholder substitution.

The subject and bodies are authored by an admin in the Send Emails form and were
previously passed to str.format(), which both leaks process internals through
attribute traversal and dies on any stray brace. Nothing here touches SMTP.

Run with: python tests/test_email_templates.py
"""
import sys

from _fixture import Results  # noqa: F401  (also fixes sys.path for the import below)

from utils.emailer import PLACEHOLDERS, fill_placeholders

VALUES = {"participant_name": "Ada Lovelace", "event_name": "Hackathon 2026"}

r = Results()

# ── the documented behaviour ─────────────────────────────────────────────────
r.check("substitutes both placeholders",
        fill_placeholders("Hi {participant_name}, welcome to {event_name}!", VALUES)
        == "Hi Ada Lovelace, welcome to Hackathon 2026!")
r.check("substitutes repeated placeholders",
        fill_placeholders("{event_name} {event_name}", VALUES) == "Hackathon 2026 Hackathon 2026")
r.check("leaves a template with no placeholders alone",
        fill_placeholders("Plain text", VALUES) == "Plain text")
r.check("handles an empty template", fill_placeholders("", VALUES) == "")
r.check("handles None", fill_placeholders(None, VALUES) == "")
r.check("the documented placeholders are the supported ones",
        set(PLACEHOLDERS) == set(VALUES), PLACEHOLDERS)

# ── the injection that str.format() allowed ──────────────────────────────────
traversal = "{event_name.__class__.__init__.__globals__}"
result = fill_placeholders(traversal, VALUES)
r.check("attribute traversal is not evaluated", result == traversal, result)
r.check("attribute traversal leaks nothing", "globals" not in result.lower().replace("__globals__", ""),
        result)

indexing = "{participant_name[0]}"
r.check("index access is not evaluated", fill_placeholders(indexing, VALUES) == indexing)

conversion = "{event_name!r}"
r.check("conversion specs are not evaluated", fill_placeholders(conversion, VALUES) == conversion)

spec = "{event_name:>500}"
r.check("format specs are not evaluated (no padding bomb)",
        fill_placeholders(spec, VALUES) == spec)

# ── the crashes that str.format() caused ─────────────────────────────────────
css = "<style>body { margin: 0; padding: 0 }</style><p>Hi {participant_name}</p>"
rendered = fill_placeholders(css, VALUES)
r.check("CSS braces survive instead of raising", "{ margin: 0; padding: 0 }" in rendered, rendered)
r.check("placeholders still fill around CSS braces", "Hi Ada Lovelace" in rendered, rendered)

r.check("an unknown placeholder is left verbatim",
        fill_placeholders("Hello {sponsor_name}", VALUES) == "Hello {sponsor_name}")
r.check("a lone opening brace is harmless", fill_placeholders("100% { of it", VALUES) == "100% { of it")
r.check("a lone closing brace is harmless", fill_placeholders("done }", VALUES) == "done }")

# ── HTML escaping ────────────────────────────────────────────────────────────
spicy = {"participant_name": "Tom & Jerry <script>", "event_name": "Q&A"}
plain = fill_placeholders("Hi {participant_name}", spicy)
escaped = fill_placeholders("Hi {participant_name}", spicy, escape_html=True)
r.check("plain text part is not escaped", plain == "Hi Tom & Jerry <script>", plain)
r.check("html part escapes ampersands and tags",
        escaped == "Hi Tom &amp; Jerry &lt;script&gt;", escaped)
r.check("escaping does not touch the surrounding markup",
        fill_placeholders("<p>{event_name}</p>", spicy, escape_html=True) == "<p>Q&amp;A</p>")

# ── the shipped defaults still render ────────────────────────────────────────
default_subject = "Your Certificate for {event_name}"
default_plain = ("Hello {participant_name},\n\nAttached is your certificate for "
                 "{event_name}. Congratulations!\n\nBest regards,\nThe Organizers")
default_html = ("<html><body><p>Hello <b>{participant_name}</b>,</p>"
                "<p>Attached is your certificate for <b>{event_name}</b>.</p></body></html>")
r.check("default subject renders",
        fill_placeholders(default_subject, VALUES) == "Your Certificate for Hackathon 2026")
r.check("default plain body renders", "Hello Ada Lovelace," in fill_placeholders(default_plain, VALUES))
r.check("default html body renders",
        "<b>Ada Lovelace</b>" in fill_placeholders(default_html, VALUES, escape_html=True))

sys.exit(r.finish())
