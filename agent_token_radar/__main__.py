"""Entry point for ``python -m agent_token_radar``.

Also supports ``python path/to/agent_token_radar/__main__.py`` (running the
file directly), which a newcomer naturally tries.  ``python -m`` sets
``__package__``; a direct run does not, so the relative import below would fail
with "attempted relative import with no known parent package".  In that case we
put the *parent* directory on ``sys.path`` and import the package by name.
"""

try:
    from .cli import main
except ImportError:  # executed as a plain file, not as a module
    import os
    import sys

    _pkg_dir = os.path.dirname(os.path.abspath(__file__))
    _parent = os.path.dirname(_pkg_dir)
    if _parent not in sys.path:
        sys.path.insert(0, _parent)
    from agent_token_radar.cli import main

raise SystemExit(main())
