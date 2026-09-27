---
name: botsv3_historical
description: Correctly search BOTSv3 as historical training data and avoid recent relative-time mistakes.
version: 1
---

BOTSv3 HISTORICAL SEARCH
- BOTSv3 is historical training data; the activity used by this lab is centred on August 2018.
- Never use recent windows such as earliest=-24h, -7d, today, or similar.
- The Python wrapper supplies MCP earliest_time="0" and latest_time="now".
- Do not put earliest_time= or latest_time= inside the SPL query string.
- Zero results mean refine the SPL; they do not mean the index is empty.
