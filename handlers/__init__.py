"""Per-surface Telegram handler package for the multi-tenant paid path.

One module per top-level surface (anti-sprawl). `middleware.py` owns identity
resolution, the invite gate, and the @requires_tier decorator. The owner's
single-tenant path in bot.py is untouched: the owner falls through to the legacy
handlers, while non-owner invited users are routed through this package.
"""
