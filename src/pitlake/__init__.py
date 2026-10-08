"""pitlake: a point-in-time fundamentals lakehouse.

Every value is stored with the date it became public (``known_from``) and the
date it was superseded (``known_to``), so any question can be answered
"as the market knew it" on a given day, and every change is explained.
"""

__version__ = "0.1.0"
