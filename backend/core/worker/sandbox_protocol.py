"""The one contract a worker and its sandbox image share.

This module imports nothing, so the worker can read the image's own copy with a
bare ``python3 -c`` before it sends the image a request it might not understand.
"""

# The request and event shapes the worker and its sandbox exchange. The image
# carries the optimizer itself, so this is the only contract the two share; bump
# it whenever either shape changes, even by an added field: the image silently
# drops a field its models do not know (``test_sandbox_protocol`` pins the shapes).
SANDBOX_PROTOCOL = 6
