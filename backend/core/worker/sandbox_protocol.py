"""The one contract a worker and its sandbox image share.

This module imports nothing, so the worker can read the image's own copy with a
bare ``python3 -c`` before it sends the image a request it might not understand.
"""

# The request and event shapes the worker and its sandbox exchange. The image
# carries the optimizer itself, so this is the only contract the two share; bump
# it whenever either shape changes, even by an added field: the image silently
# drops a field its models do not know (``test_sandbox_protocol`` pins the shapes).
SANDBOX_PROTOCOL = 15

# Older image protocols that read this worker's guest request the same way, so a
# deploy keeps its pinned image until the image is rebuilt. Protocol 13 added
# ``conversation_id``, which the submit route clears before it stores the run.
# Protocol 14 added ``target.repo.draft_files`` and ``draft_base``: the parent
# writes the drafts into the staged tree and removes both fields before the
# guest request, for a run and for serving, so an older image never sees them.
# Protocol 15 (SKY-691) moves the stop score from ``budget.stop_at_score`` to the
# staged snapshot's ``outcome``. An older image reads only the budget key, which
# a run stored before protocol 15 still carries, so the worker refuses an older
# image only for a run whose snapshot ``outcome`` sets a target or a minimum.
# The worker sends such an image the image's own protocol number.
READABLE_PROTOCOLS = frozenset({12, 13, 14, SANDBOX_PROTOCOL})
