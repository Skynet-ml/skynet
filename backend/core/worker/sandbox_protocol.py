"""The one contract a worker and its sandbox image share.

This module imports nothing, so the worker can read the image's own copy with a
bare ``python3 -c`` before it sends the image a request it might not understand.
"""

# The request and event shapes the worker and its sandbox exchange. The image
# carries the optimizer itself, so this is the only contract the two share; bump
# it whenever either shape changes, even by an added field: the image silently
# drops a field its models do not know (``test_sandbox_protocol`` pins the shapes).
SANDBOX_PROTOCOL = 17

# Older image protocols that read this worker's guest request the same way, so a
# deploy keeps its pinned image until the image is rebuilt. Protocol 13 added
# ``conversation_id``, which the submit route clears before it stores the run.
# Protocol 14 added ``target.repo.draft_files`` and ``draft_base``: the parent
# writes the drafts into the staged tree and removes both fields before the
# guest request, for a run and for serving, so an older image never sees them.
# Protocol 15 (SKY-691) moves the stop score from ``budget.stop_at_score`` to the
# staged snapshot's ``outcome``. An image before 15 reads only the budget key,
# which a run stored before protocol 15 still carries.
# Protocol 16 (SKY-692) does not change the request shape. The image at 16
# writes the run end verdict of the ``[outcome]`` target and minimums into the
# result details. An image at 15 stops at the target but reports no verdict, so
# the worker refuses every image before 16 for a run whose snapshot ``outcome``
# sets a target or a minimum.
# Protocol 17 added ``target.repo.contract_source`` (SKY-693). Only the worker
# reads it, and an older image's models ignore a field they do not know, so an
# older image runs the request the same way.
# The worker sends an older image the image's own protocol number.
READABLE_PROTOCOLS = frozenset({12, 13, 14, 15, 16, SANDBOX_PROTOCOL})

# The first protocol whose image writes the run end verdict of ``[outcome]``.
# A later bump that keeps the verdict must not refuse the images at this one.
OUTCOME_VERDICT_PROTOCOL = 16
