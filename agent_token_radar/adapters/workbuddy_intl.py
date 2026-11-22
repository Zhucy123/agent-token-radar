"""WorkBuddy AI adapter.

Same record format as the domestic WorkBuddy build (verified on this machine:
``providerData.rawUsage`` with prompt/completion/cache tokens), just a
different data directory: ``~/.workbuddy-ai/projects/<project>/*.jsonl``.

The display name is the vendor's own product string, read from the installed
executable: ``ProductName = "WorkBuddy AI"`` (v5.8.4, Tencent). The domestic
build is ``ProductName = "WorkBuddy"`` (v5.9.2), so the two are distinguishable
by their real names rather than a label we invented.

Keeping it a separate adapter (rather than merging into one card) matches how
the two builds are installed side by side and lets the dashboard show them
independently.

event_key = ``workbuddy-intl|<record id>``
"""

from __future__ import annotations

from .workbuddy import _WorkBuddyBase


class WorkBuddyIntlAdapter(_WorkBuddyBase):
    name = "workbuddy-intl"
    display_name = "WorkBuddy AI"
    home_env = "WORKBUDDY_AI_HOME"
    dot_dirs = (".workbuddy-ai",)
