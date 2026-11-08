"""Create and delete one test account for one check on Skynet staging or a PR preview.

Usage (AWS profile ``skynet`` must be signed in):

    python3 account.py create --target staging --label SKY-123   # or --target pr-1004
    eval "$(python3 account.py env <id>)"                        # then source login.sh
    python3 account.py delete <id>
    python3 account.py list                                      # leftover accounts
    python3 account.py forget <id>                               # secret only

``create`` writes the Secrets Manager secret
``skynet/staging/check-account/<id>`` with status ``pending`` first. Then it
signs up through the normal register -> email code -> confirm path, with the
code from the Resend receiving inbox, and sets the status to ``ready``. So a
failed or killed ``create`` still leaves a secret that ``list`` shows.
``delete`` signs in, calls ``POST /account/delete`` and then removes the
secret. If sign-in fails for a ``pending`` account, it first finishes the
email confirmation and marks the secret ``ready``. ``forget`` removes
only the secret, for an account that does not exist.
Only ``env`` prints the password; run it only inside ``eval``. No command
prints a token or an email code.
"""

import argparse
import json
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

AWS = ["aws", "--profile", "skynet", "--region", "us-east-2"]
DOH = ["--doh-url", "https://cloudflare-dns.com/dns-query"]
INBOX = "eegeupelae.resend.app"
PREFIX = "skynet/staging/check-account/"


def run(args, stdin=None):
    """Run a command and return its stdout.

    Args:
        args: Command and arguments. Never put a secret here: argv is visible
            in the process list.
        stdin: Text for the command's stdin.

    Returns:
        The command's stdout.
    """
    return subprocess.run(args, input=stdin, capture_output=True, text=True, check=True).stdout


def curl(url, *, body=None, form=False, header_file=None, jar=None, stdin_header=None):
    """Call a URL with curl and return the status code and the body.

    Secrets travel only on stdin or in a private temporary file.

    Args:
        url: Target URL.
        body: Request body (dict). Sent as JSON, or as a form when ``form``.
        form: Send ``body`` as ``application/x-www-form-urlencoded``.
        header_file: Path of a private file with one extra header.
        jar: Cookie jar path.
        stdin_header: One header line sent on stdin (GET only).

    Returns:
        ``(status, text)``.
    """
    args = ["curl", "-s", *DOH, url, "-w", "\n%{http_code}"]
    if jar:
        args += ["-c", jar, "-b", jar]
    if header_file:
        args += ["-H", f"@{header_file}"]
    stdin = None
    if body is not None:
        if form:
            stdin = urllib.parse.urlencode(body)
            args += ["-H", "Content-Type: application/x-www-form-urlencoded", "-H", "X-Auth-Return-Redirect: 1"]
        else:
            stdin = json.dumps(body)
            args += ["-H", "Content-Type: application/json"]
        args += ["--data-binary", "@-"]
    elif stdin_header:
        stdin = stdin_header + "\n"
        args += ["-H", "@-"]
    text, status = run(args, stdin).rsplit("\n", 1)
    return int(status), text


def targets(target):
    """Return the app and API URLs of a target.

    Args:
        target: ``staging`` or ``pr-<n>``.

    Returns:
        ``(app, api)``.
    """
    if target == "staging":
        return "https://staging.skynetml.com", "https://api-staging.skynetml.com"
    if re.fullmatch(r"pr-\d+", target):
        return f"https://{target}.staging.skynetml.com", f"https://api-{target}.staging.skynetml.com"
    sys.exit("account: --target must be staging or pr-<n>")


def read_secret(name):
    """Return the parsed JSON value of a Secrets Manager secret.

    Args:
        name: Secret name.

    Returns:
        The secret value as a dict.
    """
    return json.loads(
        run(
            [
                *AWS,
                "secretsmanager",
                "get-secret-value",
                "--secret-id",
                name,
                "--query",
                "SecretString",
                "--output",
                "text",
            ]
        )
    )


def secret_gone(err):
    """Return whether a failed AWS call means that the secret no longer exists.

    Args:
        err: The failed ``aws`` call.

    Returns:
        True for a missing secret, or one that a delete removed a moment ago.
    """
    # For a short time after a force delete, AWS answers "marked for deletion" instead of not found.
    return "ResourceNotFoundException" in err.stderr or "marked for deletion" in err.stderr


def inbox_auth():
    """Return the Resend authorization header line."""
    return "Authorization: Bearer " + read_secret("skynet/staging/app")["SMTP_PASSWORD"]


def mail_ids(email, auth):
    """Return the ids of the received emails sent to ``email``.

    Args:
        email: Recipient address.
        auth: Resend authorization header line.

    Returns:
        A list of email ids, newest first.
    """
    _, text = curl("https://api.resend.com/emails/receiving?limit=100", stdin_header=auth)
    return [m["id"] for m in json.loads(text).get("data", []) if email in json.dumps(m.get("to", ""))]


def email_code(email, seen=()):
    """Wait for the confirmation code sent to ``email`` in the Resend inbox.

    Args:
        email: Address that the sign-up sent the code to.
        seen: Email ids to skip, so that an old code is not used again.

    Returns:
        The six-digit code, or ``None`` after about two minutes.
    """
    auth = inbox_auth()
    for _ in range(30):
        time.sleep(4)
        for mail_id in mail_ids(email, auth):
            if mail_id not in seen:
                _, text = curl(f"https://api.resend.com/emails/receiving/{mail_id}", stdin_header=auth)
                msg = json.loads(text)
                found = re.search(
                    r"\b(\d{6})\b",
                    " ".join(
                        [msg.get("subject") or "", msg.get("text") or "", re.sub("<[^>]+>", " ", msg.get("html") or "")]
                    ),
                )
                if found:
                    return found.group(1)
    return None


def write_secret(ident, value, *, new=False):
    """Store ``value`` in the account's secret and tag it with its target and status.

    The value goes through a private temporary file, never through argv.

    Args:
        ident: Account id.
        value: Secret value (dict with ``target`` and ``status``).
        new: Create the secret instead of putting a new version.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".json") as tmp:
        Path(tmp.name).chmod(0o600)
        json.dump(value, tmp)
        tmp.flush()
        tags = [f"Key=target,Value={value['target']}", f"Key=status,Value={value['status']}"]
        if new:
            desc = f"Check account for {value['label']} on {value['target']}; delete after the check"
            run(
                [
                    *AWS,
                    "secretsmanager",
                    "create-secret",
                    "--name",
                    PREFIX + ident,
                    "--description",
                    desc,
                    "--secret-string",
                    f"file://{tmp.name}",
                    "--tags",
                    *tags,
                ]
            )
            return
        run(
            [
                *AWS,
                "secretsmanager",
                "put-secret-value",
                "--secret-id",
                PREFIX + ident,
                "--secret-string",
                f"file://{tmp.name}",
            ]
        )
        run([*AWS, "secretsmanager", "tag-resource", "--secret-id", PREFIX + ident, "--tags", *tags])


def create(target, label):
    """Store a pending account secret, sign up and verify, then mark it ready and print its id.

    Args:
        target: ``staging`` or ``pr-<n>``.
        label: Issue or PR label, for example ``SKY-123``.
    """
    app, api = targets(target)
    ident = f"{target}-{re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')}-{secrets.token_hex(3)}"
    email = f"{ident}@{INBOX}"
    password = secrets.token_urlsafe(18) + "A1!"
    value = {"email": email, "password": password, "target": target, "app": app, "api": api, "label": label}
    # The secret exists before the account does, so no failure or kill can leave an account that list cannot see.
    write_secret(ident, {**value, "status": "pending"}, new=True)
    print(f"id={ident} status=pending")
    stop = f"account: create stopped; the secret stays pending. Run: python3 account.py delete {ident}"
    status, _ = curl(f"{app}/api/register", body={"email": email, "password": password})
    print("register: HTTP", status)
    if status >= 300:
        sys.exit(stop)
    code = email_code(email)
    if not code:
        sys.exit(stop)
    status, _ = curl(f"{app}/api/email-verify/confirm", body={"email": email, "code": code})
    print("confirm: HTTP", status)
    if status >= 300:
        sys.exit(stop)
    write_secret(ident, {**value, "status": "ready"})
    print(f"id={ident} status=ready")


def env(ident):
    """Print shell exports for ``login.sh``. Use only inside ``eval``.

    Args:
        ident: Account id that ``create`` printed.
    """
    value = read_secret(PREFIX + ident)
    exports = {
        "SKYNET_TEST_EMAIL": value["email"],
        "SKYNET_TEST_PASSWORD": value["password"],
        "PREFIX": "SKYNET_TEST",
        "APP": value["app"],
        "API": value["api"],
    }
    print("export " + " ".join(f"{k}={json.dumps(v)}" for k, v in exports.items()))


def verify_pending(ident, value):
    """Finish the email confirmation of an unverified account, then mark its secret ready.

    An unverified account cannot sign in, and so cannot delete itself.

    Args:
        ident: Account id.
        value: Secret value of the account.
    """
    email, app = value["email"], value["app"]
    seen = set(mail_ids(email, inbox_auth()))
    status = None
    if seen:
        status, _ = curl(f"{app}/api/email-verify/confirm", body={"email": email, "code": email_code(email)})
        print("confirm with the sign-up code: HTTP", status)
    if status is None or status >= 300:
        # The resend route answers 200 but sends nothing within 60 s of the last code.
        time.sleep(61)
        # Read the inbox again: a late sign-up email can arrive during the wait, and its code is not the new one.
        seen = set(mail_ids(email, inbox_auth()))
        status, _ = curl(f"{app}/api/email-verify/request", body={"email": email})
        print("code request: HTTP", status)
        code = email_code(email, seen)
        if not code:
            # A verified account and an unknown address also get no code, so this alone never proves the account is gone.
            sys.exit(
                "account: sign-in failed and no new email code arrived; the secret stays."
                " A verified account gets no new code, so the account may still exist."
                " Use forget only if create printed no 'register: HTTP 2xx' line, or the preview"
                f" database is gone. Otherwise report the id {ident}."
            )
        status, _ = curl(f"{app}/api/email-verify/confirm", body={"email": email, "code": code})
        print("confirm: HTTP", status)
        if status >= 300:
            sys.exit("account: confirm failed; the secret stays, report the id " + ident)
    write_secret(ident, {**value, "status": "ready"})


def sign_in(value, tmp):
    """Sign in through the app and return the backend access token.

    Args:
        value: Secret value of the account.
        tmp: Private temporary directory for the cookie jar.

    Returns:
        The token, or an empty string when the sign-in fails.
    """
    app = value["app"]
    jar = str(Path(tmp) / "jar")
    Path(jar).unlink(missing_ok=True)
    _, text = curl(f"{app}/api/auth/csrf", jar=jar)
    csrf = json.loads(text)["csrfToken"]
    curl(
        f"{app}/api/auth/callback/credentials",
        jar=jar,
        form=True,
        body={"email": value["email"], "password": value["password"], "csrfToken": csrf, "callbackUrl": app},
    )
    _, text = curl(f"{app}/api/auth/session", jar=jar)
    return (json.loads(text) or {}).get("backendAccessToken", "")


def delete(ident):
    """Delete the account through the app, then remove its secret.

    Args:
        ident: Account id that ``create`` printed.
    """
    try:
        value = read_secret(PREFIX + ident)
    except subprocess.CalledProcessError as err:
        if not secret_gone(err):
            raise
        print("account: no secret for this id; the account is already deleted or forgotten. Nothing to do.")
        return
    api = value["api"]
    with tempfile.TemporaryDirectory() as tmp:
        # Sign in first: the secret can say pending for a verified account (create or delete died after confirm).
        token = sign_in(value, tmp)
        if not token and value.get("status") != "ready":
            verify_pending(ident, value)
            token = sign_in(value, tmp)
        if not token:
            sys.exit(
                "account: sign-in failed; the secret stays. If an earlier delete printed"
                " 'account delete: HTTP 200', or the preview database is gone, the account is gone:"
                f" run python3 account.py forget {ident}. Otherwise report the id."
            )
        header = Path(tmp) / "auth"
        header.touch(mode=0o600)
        header.write_text(f"authorization: Bearer {token}\n")
        status, _ = curl(f"{api}/account/delete", header_file=header, body={"password": value["password"]})
    print("account delete: HTTP", status)
    if status != 200:
        sys.exit("account: delete failed; the secret stays, report the id " + ident)
    run([*AWS, "secretsmanager", "delete-secret", "--secret-id", PREFIX + ident, "--force-delete-without-recovery"])
    print("secret removed")


def forget(ident):
    """Remove only the secret of an account that no longer exists in the app.

    Args:
        ident: Account id.
    """
    try:
        run([*AWS, "secretsmanager", "delete-secret", "--secret-id", PREFIX + ident, "--force-delete-without-recovery"])
    except subprocess.CalledProcessError as err:
        if not secret_gone(err):
            raise
        print("account: no secret for this id. Nothing to do.")
        return
    print("secret removed; no account delete ran")


def list_left():
    """Print the id, target, status and creation date of every check account secret."""
    out = json.loads(
        run(
            [
                *AWS,
                "secretsmanager",
                "list-secrets",
                "--filters",
                f"Key=name,Values={PREFIX}",
                "--query",
                "SecretList[].[Name,CreatedDate,Tags]",
                "--output",
                "json",
            ]
        )
    )
    for name, created, tags in out:
        tag = {t["Key"]: t["Value"] for t in tags or []}
        print(name.removeprefix(PREFIX), tag.get("target", "?"), tag.get("status", "?"), created)
    print(f"{len(out)} check accounts")


def main():
    """Parse the command line and run one command."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    make = sub.add_parser("create")
    make.add_argument("--target", required=True)
    make.add_argument("--label", required=True)
    for name in ("env", "delete", "forget"):
        sub.add_parser(name).add_argument("id")
    sub.add_parser("list")
    args = parser.parse_args()
    if args.cmd == "create":
        create(args.target, args.label)
    elif args.cmd == "env":
        env(args.id)
    elif args.cmd == "delete":
        delete(args.id)
    elif args.cmd == "forget":
        forget(args.id)
    else:
        list_left()


if __name__ == "__main__":
    main()
