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
# deploy keeps its pinned image until the image is rebuilt. The worker sends such
# an image the image's own protocol number. Protocol 15 (SKY-691) moves the stop
# score from ``budget.stop_at_score`` to the staged snapshot's ``outcome``; an
# older image reads only the budget key and would run a new run without its
# ``[outcome] target``, so no older protocol can read this request.
READABLE_PROTOCOLS = frozenset({SANDBOX_PROTOCOL})
