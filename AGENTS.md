# H2D Custom Dashboard

Read README.md before making changes. The local source contains the dashboard,
logger, fan controller, and Dock launcher; the running service is deployed separately.

- Never open, print, or copy config.json, fan-tokens.json, or private fan configuration.
- Preserve unrelated changes and keep secrets out of Git and public artifacts.
- Do not send a print or change printer firmware, macros, or fan settings unless requested.
- Test the actual dashboard flow and run `python3 air/test_h2d_logger.py` after code changes.
  Its fake printer and temporary configuration do not use the real printer credentials.
- Run browser checks headlessly; close browsers and stop temporary servers afterward.
- Do not send messages or email without approval of the exact complete draft and recipients.
- Do not add AI attribution or contributor information; use the owner's Git identity.
- The Dock launcher resolves assets relative to its own folder. Keep that layout intact.
