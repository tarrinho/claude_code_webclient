"""Route modules, one per API prefix.

Each exports a `router` that app.py includes. Nothing here imports app.py:
a router importing the application that includes it is a circular import,
and in Python that fails at import time rather than resolving later.
"""
