"""Run a network-isolated command while the trusted parent brokers model requests."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import secrets
import shlex
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import guest_model_proxy
from .budgets import BudgetError, BudgetInsufficientError
from .model_dispatch import MODEL_ATTEMPT_HEADER, ModelHTTPResult
from .operation_pricing import UnpricedOperationError
from .runtime import ProviderFailedError, UsagePendingError
from .signals import BudgetReached
from .usage_tags import current_tags, with_tags_header

logger = logging.getLogger(__name__)

STREAM_WRITE_WAIT_SECONDS = 120

# The image's root-owned interpreter. The box user can write a python3 that PATH
# finds first (for example in /app/.venv/bin), so a root proxy never uses PATH.
ROOT_PYTHON = "/usr/local/bin/python3"
# Keep it equal to the Vercel provider's ``ROOT_MODEL_PREFIX``: only files there
# go straight through its file API, in one request and with no box command. All
# other files take a slower box-user copy (SKY-902).
ROOT_MAILBOX_PREFIX = "/tmp/.app-root-model-"


def root_empty_command(folder: str) -> str:
    """Return a shell line that empties a root folder and keeps the folder.

    Root empties only its own real folder. A box-user link or folder that
    holds the name could lead root's rm elsewhere, and the box user can swap
    its own folder for a link in sticky /tmp (SKY-902). A skip exits 0, and
    a failed removal exits nonzero.

    Args:
        folder: The folder path, already safe for the shell.

    Returns:
        The shell line.
    """
    # ``find`` keeps a folder with many answers under the argument limit that
    # one glob would pass; an image with no ``find`` falls back to the glob.
    return (
        f"if [ -d {folder} ] && [ ! -L {folder} ] && [ -O {folder} ]; then "
        f"if [ -x /usr/bin/find ]; then /usr/bin/find {folder} -mindepth 1 -maxdepth 1 -exec /bin/rm -rf -- {{}} +; "
        f"else /bin/rm -rf -- {folder}/* {folder}/.[!.]* {folder}/..?*; fi; fi"
    )


def mailbox_empty_command(folder: str) -> str:
    """Return a shell line that empties a root mailbox folder and keeps its ``responses`` folder.

    A write that its time limit gives up on can still end at Vercel later.
    The kept root ``responses`` folder holds that answer away from the box
    user. Without it, the file API would make a new box-user ``responses``
    for the late answer (SKY-902). A failed removal exits nonzero.

    Args:
        folder: The mailbox folder path, already safe for the shell.

    Returns:
        The shell line.
    """
    responses = f"{folder}/responses"
    # The parent check comes first: through a planted link, ``responses``
    # could be another live mailbox's root folder. The top level holds only
    # a few files, so a loop is enough there.
    return (
        f"if [ -d {folder} ] && [ ! -L {folder} ] && [ -O {folder} ]; then "
        f"failed=0; {root_empty_command(responses)} || failed=1; "
        f"for entry in {folder}/* {folder}/.[!.]* {folder}/..?*; do "
        f'[ "$entry" = {responses} ] || /bin/rm -rf -- "$entry" || failed=1; done; [ "$failed" = 0 ]; fi'
    )


# The box user can rewrite the proxy and config files, so a root proxy runs only
# the bytes whose digests the parent sent, from memory and a root-only copy.
ROOT_PROXY_BOOT = (
    "import hashlib,os,sys,tempfile\n"
    "source,config=(open(path,'rb').read() for path in sys.argv[3:5])\n"
    "if [hashlib.sha256(data).hexdigest() for data in (source,config)]!=sys.argv[1:3]:\n"
    " sys.exit('The model proxy files changed before they ran.')\n"
    "handle,copy=tempfile.mkstemp()\n"
    "os.write(handle,config)\n"
    "os.close(handle)\n"
    "sys.argv=[sys.argv[3],copy,*sys.argv[5:]]\n"
    "exec(compile(source,sys.argv[0],'exec'),{'__name__':'__main__'})\n"
)


class ModelMailbox:
    """Bridge guest model requests without granting the guest network or billing authority."""

    def __init__(self, dispatch: Callable[..., ModelHTTPResult], *, concurrency: int = 8) -> None:
        """Bind the supervisor's authenticated, single-attempt model dispatch.

        Args:
            dispatch: Trusted callback accepting a scoped token, path, body, and protocol headers.
            concurrency: Maximum concurrently serviced guest model requests.
        """
        self._dispatch = dispatch
        self._concurrency = concurrency

    def run(
        self,
        session: Any,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: Callable[[str, str], None] | None = None,
        sudo: bool = False,
    ) -> Any:
        """Wrap one sandbox command and service its model mailbox alongside output.

        Args:
            session: Exact sandbox session whose file and command APIs the parent owns.
            command: Original command executed without changing its algorithm.
            env: Guest environment containing only scoped route credentials.
            timeout_seconds: Command limit already covered by the sandbox reservation.
            on_output: Existing progress or upstream evaluator mailbox callback.
            sudo: Run the proxy and the command as root, as the scorer's supervisor needs (SKY-398).
                The root proxy starts from the image's interpreter and runs only the bytes the parent wrote.

        Returns:
            The sandbox's original command result.

        Raises:
            RuntimeError: Root could not make a root mailbox's folders on a box whose root has capabilities.
        """
        nonce = secrets.token_hex(16)
        # Captured here: requests are answered on pool threads, where the caller's scope is gone.
        host_tags = current_tags()
        directory = f".app-model-{nonce}"
        made = None

        def empty_root_folder(path: str) -> None:
            """Empty a root mailbox folder, whose answers the box user cannot remove, and keep the folder.

            The kept root folder holds the name, so the box user cannot make
            its own folder there for a write that ends late (SKY-902).

            Args:
                path: The mailbox folder. A path outside the root prefix is left alone.
            """
            if not path.startswith(ROOT_MAILBOX_PREFIX):
                return
            try:
                removed = session.run(mailbox_empty_command(shlex.quote(path)), sudo=True, timeout_seconds=30)
            except Exception:
                logger.warning("The sandbox could not empty the model mailbox folder.", exc_info=True)
                return
            if removed.exit_code != 0:
                logger.warning("The sandbox could not empty the model mailbox folder: exit %s.", removed.exit_code)

        # Each bridge session (AsyncBridgeSession, ParentSandboxSession) copies its
        # provider's flags. ``root_has_capabilities`` is True unless the
        # provider says otherwise (Docker says False). Only Vercel sets
        # ``root_file_writes``, so a guest mailbox elsewhere runs no root
        # command and keeps its working folder, as before. A root proxy on a
        # session without the flag still gets the /tmp mailbox, because a
        # box-user working folder would let the box user swap root's answers.
        root_proxy = sudo and getattr(session, "root_has_capabilities", True)
        if root_proxy or getattr(session, "root_file_writes", False):
            folder = f"{ROOT_MAILBOX_PREFIX}{nonce}"
            # A box user can rename a root-owned folder in its own working folder
            # and put its own in place. In sticky /tmp it cannot (SKY-902).
            # No ``-p``: mkdir fails on anything already at the name.
            # The file API writes each answer with all capabilities, so the box
            # user must not make any entry in these folders: it could plant a
            # link or a hard link at the next answer's name. For a root proxy, a
            # 0700 ``responses`` also keeps the box user from changing an answer
            # before the proxy reads it.
            # A guest proxy reads its answers in a 0755 ``responses``, cannot
            # remove them, and keeps its model bindings in a folder of its own.
            mode = "0700" if sudo else "0755"
            try:
                made = session.run(
                    f"/bin/mkdir -m 0755 {shlex.quote(folder)} && /bin/mkdir -m {mode} {shlex.quote(folder)}/responses",
                    sudo=True,
                )
            except Exception:
                # The first mkdir can work before the call raises.
                empty_root_folder(folder)
                if sudo:
                    raise
                # Some providers refuse sudo with an error, not an exit code.
                logger.warning("The sandbox refused the guest model mailbox folders in /tmp.", exc_info=True)
                made = None
            if made is not None and made.exit_code != 0:
                # The first mkdir can work and the second fail. Root empties
                # the folder and keeps it, so the name stays taken.
                empty_root_folder(folder)
            if made is not None and made.exit_code == 0:
                directory = folder
            elif sudo:
                raise RuntimeError("The sandbox could not make the model mailbox folders.")
            elif made is not None:
                # The guest proxy still works in its working folder; on Vercel
                # each answer then takes the slower box-user copy.
                logger.warning("The sandbox could not make the guest model mailbox folders in /tmp.")
        prefix = f"APP_MODEL_{nonce}:"
        source_path = f"{directory}/proxy.py"
        config_path = f"{directory}/config.json"
        response_directory = f"{directory}/responses"
        source = Path(guest_model_proxy.__file__).read_text(encoding="utf-8")
        config = json.dumps(
            {"prefix": prefix, "response_dir": response_directory, "timeout_seconds": timeout_seconds or 600}
        )
        files = {source_path: source, config_path: config}
        if made is None and sudo:
            # Docker writes files as the box user and its root has no capabilities,
            # so a root-owned folder would refuse the parent's writes, and the root
            # proxy could not make ``responses`` itself. The parent makes it first.
            files[f"{response_directory}/.keep"] = ""
        # A stream write checks ``finished`` and counts itself in under one
        # condition, so no write starts after root empties the folder. Writes
        # run outside it, so parallel streams do not wait on each other
        # (SKY-624). A write that its time limit gives up on can still end at
        # Vercel later and leave one answer in the kept root ``responses``
        # folder; nothing reads that path again.
        writes = threading.Condition()
        in_flight = 0

        def begin_write() -> bool:
            """Count one stream write in, unless the command ended.

            Returns:
                True when the caller may write; it must then call ``end_write``.
            """
            nonlocal in_flight
            with writes:
                if finished.is_set():
                    return False
                in_flight += 1
                return True

        def end_write() -> None:
            """Count one stream write out, and wake the cleanup."""
            nonlocal in_flight
            with writes:
                in_flight -= 1
                writes.notify_all()

        try:
            session.write_files(files)
        except BaseException:
            empty_root_folder(directory)
            raise
        pending = {"stdout": "", "stderr": ""}
        visible: dict[str, list[str]] = {"stdout": [], "stderr": []}
        seen: set[str] = set()
        guard = threading.Lock()
        errors: list[BaseException] = []
        # MCP event streams whose box reader left, and a flag for the end of the command.
        abandoned: set[str] = set()
        finished = threading.Event()

        def respond(document: dict[str, Any]) -> None:
            """Write one trusted response while preserving a paid attempt on transport failure.

            Args:
                document: The model frame that the box sent.
            """
            try:
                headers = with_tags_header(
                    {key.lower(): value for key, value in document.get("headers", {}).items()}, host_tags
                )
                headers[MODEL_ATTEMPT_HEADER] = document["id"]
                token = headers.get("authorization", "").removeprefix("Bearer ") or headers.get("x-api-key", "")
                body = base64.b64decode(document["body"])
                # An MCP server or a service takes the client's bytes as they are; a model request is one JSON object.
                if not str(document["path"]).startswith((guest_model_proxy.MCP_PATH, guest_model_proxy.SERVICE_PATH)):
                    body = json.loads(body)
                response = self._dispatch(token, document["path"], body, headers)
            except BudgetReached as error:
                response = failure(402, "budget_reached", str(error))
            except BudgetInsufficientError as error:
                response = failure(402, "budget_insufficient", str(error))
            except ProviderFailedError as error:
                response = failure(502, "provider_failed", str(error))
            except BudgetError:
                response = failure(424, "usage_pending", "Previous model usage is awaiting confirmation.")
            except UsagePendingError as error:
                # The runtime names whose usage is pending: this call's, or earlier work that blocks admission.
                response = failure(424, "usage_pending", str(error))
            except (UnpricedOperationError, ValueError, TypeError) as error:
                response = failure(422, "unpriced_operation", str(error))
            except BaseException as error:
                errors.append(error)
                response = failure(502, "provider_unavailable", "The model transport was interrupted.")
            answer: dict[str, Any] = {
                "status": response.status,
                "content_type": response.content_type,
                "body": base64.b64encode(response.body).decode(),
                "headers": [list(pair) for pair in response.headers],
            }
            if response.stream is None:
                session.write_files({f"{response_directory}/{document['id']}.json": json.dumps(answer)})
                return
            try:
                session.write_files(
                    {f"{response_directory}/{document['id']}.json": json.dumps({**answer, "more": True})}
                )
            except BaseException:
                response.stream.close()
                raise
            # An event stream can stay open for the whole command, so it gets its own thread, not a pool worker.
            threading.Thread(
                target=pump,
                args=(document["id"], response.stream),
                daemon=True,
                name="sandbox-mcp-stream",
            ).start()

        def pump(identity: str, stream: Any) -> None:
            """Write each part of an MCP event stream as its own response file, then an end part.

            Part ``n`` goes to ``<id>.<n>.json``. The last part has ``"more": false``.
            The stream stops with no last part when the box's reader left or the
            command ended, so a server that keeps it alive with pings cannot hold
            its place.

            Args:
                identity: The request's id.
                stream: The relay's event stream.
            """
            base = f"{response_directory}/{identity}"
            part = 0
            try:
                for chunk in stream:
                    if identity in abandoned or not begin_write():
                        return
                    try:
                        part += 1
                        session.write_files(
                            {
                                f"{base}.{part}.json": json.dumps(
                                    {"body": base64.b64encode(chunk).decode(), "more": True}
                                )
                            }
                        )
                    finally:
                        end_write()
            except Exception as error:
                # Only the type: the relay already recorded the call, and a message could hold a body.
                logger.warning("An MCP event stream ended early in the sandbox mailbox: %s", type(error).__name__)
            finally:
                stream.close()
            # A box that went away cannot take the end part; the guest proxy is gone with it.
            if not begin_write():
                return
            try:
                with contextlib.suppress(Exception):
                    session.write_files({f"{base}.{part + 1}.json": json.dumps({"more": False})})
            finally:
                end_write()

        def failure(status: int, code: str, message: str) -> ModelHTTPResult:
            """Return an admission error understood by both supported model protocols.

            Args:
                status: The HTTP status.
                code: The error type and code.
                message: The error text for the caller.

            Returns:
                The error response.
            """
            return ModelHTTPResult(
                status,
                "application/json",
                json.dumps(
                    {
                        "error": {
                            "type": code,
                            "code": code,
                            "message": message,
                        }
                    }
                ).encode(),
            )

        with ThreadPoolExecutor(max_workers=self._concurrency, thread_name_prefix="sandbox-model") as executor:
            futures = []

            def output(stream: str, data: str) -> None:
                """Consume nonce-bound model frames and forward ordinary output in order.

                Args:
                    stream: The output stream name, ``stdout`` or ``stderr``.
                    data: The next text from that stream.
                """
                with guard:
                    pending[stream] = pending.get(stream, "") + data
                    while "\n" in pending[stream]:
                        line, pending[stream] = pending[stream].split("\n", 1)
                        if stream == "stdout" and line.startswith(prefix):
                            document = json.loads(line[len(prefix) :])
                            identity = document.get("id", "")
                            if (
                                not isinstance(identity, str)
                                or len(identity) != 32
                                or any(char not in "0123456789abcdef" for char in identity)
                            ):
                                raise ValueError("Invalid model mailbox identity.")
                            if identity not in seen:
                                seen.add(identity)
                                if "stop" in document:
                                    abandoned.add(str(document["stop"]))
                                elif not finished.is_set():
                                    # A late frame gets no answer: it would land after root empties the folder.
                                    futures.append(executor.submit(respond, document))
                        else:
                            visible.setdefault(stream, []).append(line + "\n")
                            if on_output is not None:
                                on_output(stream, line + "\n")

            # A version can write ~/.bash_profile, ~/.profile or a .pth file in user
            # site-packages under $HOME. A login shell runs the first two, and a
            # python3 without -I runs the .pth (E38). The proxy needs only the stdlib.
            wrapped = (
                f"python3 -I {shlex.quote(source_path)} {shlex.quote(config_path)}"
                f" bash --noprofile --norc -c {shlex.quote(command)}"
            )
            if sudo:
                digests = " ".join(hashlib.sha256(text.encode()).hexdigest() for text in (source, config))
                wrapped = (
                    f"{ROOT_PYTHON} -I -S -c {shlex.quote(ROOT_PROXY_BOOT)} {digests}"
                    f" {shlex.quote(source_path)} {shlex.quote(config_path)}"
                    f" /bin/bash --noprofile --norc -c {shlex.quote(command)}"
                )
            try:
                result = session.run(
                    wrapped,
                    env=env,
                    timeout_seconds=timeout_seconds,
                    on_output=output,
                    **({"sudo": True} if sudo else {}),
                )
            finally:
                finished.set()
                if directory.startswith(ROOT_MAILBOX_PREFIX):
                    # A box snapshot would keep the answers. Remove them once the
                    # answers are written and no stream write is in flight.
                    # ``output`` checks ``finished`` under ``guard``, so after
                    # this no response joins ``futures``.
                    with guard:
                        submitted = list(futures)
                    wait(submitted)
                    with writes:
                        # Another provider's write may have no time limit. A
                        # write that ends after this still lands in the kept
                        # root folder, away from the box user.
                        if not writes.wait_for(lambda: in_flight == 0, timeout=STREAM_WRITE_WAIT_SECONDS):
                            logger.warning("A model mailbox stream write did not end before the cleanup.")
                    empty_root_folder(directory)
            for future in futures:
                future.result()
            for stream, data in pending.items():
                if data and not (stream == "stdout" and data.startswith(prefix)):
                    visible.setdefault(stream, []).append(data)
                    if on_output is not None:
                        on_output(stream, data)
            if errors:
                raise errors[0]
            return replace(result, stdout="".join(visible["stdout"]), stderr="".join(visible["stderr"]))
