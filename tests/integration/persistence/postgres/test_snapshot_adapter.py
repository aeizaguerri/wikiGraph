"""Snapshot smoke consolidated into the gated public-store cancellation journey.

That owned PostgREST journey calls ``get_state`` while the run is active and
checks the mapped request/node cap, status, run ID, and persisted owner token.
"""
