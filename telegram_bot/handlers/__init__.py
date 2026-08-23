"""Command/callback handlers for the Telegram bot, split by domain.

``bot.py`` is a thin assembler that imports the handler callables from these
modules and registers them on the Application. Shared state and helpers live in
``common`` (imported by every other module); to avoid import cycles no handler
module imports another except ``subscriptions``, which reuses the action
helpers in ``premium_analytics`` and ``tracker_cmds`` for its inline menu.
"""
