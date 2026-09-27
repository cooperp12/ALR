---
name: email_investigation
description: Find notification/email evidence in BOTSv3 without flooding the model with full MIME bodies.
version: 1
---

EMAIL INVESTIGATION
- Search for distinctive sender/recipient/subject terms before requesting bodies.
- BOTSv3 may include O365 message-trace and SMTP/network email evidence.
- Useful sourcetypes can include ms:o365:reporting:messagetrace and stream:smtp.
- Prefer subject, sender/from, recipient/to, message ID, and a short content
  excerpt over full MIME payloads.
- If the next step depends on a URL in the message, extract only the URL(s)
  with rex/table or inspect a small content excerpt.
