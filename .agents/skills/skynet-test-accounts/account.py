"""Create and delete one test account for one check on Skynet staging or a PR preview.

Usage (AWS profile ``skynet`` must be signed in):

    python3 account.py create --target staging --label SKY-123   # or --target pr-1004
    eval "$(python3 account.py env <id>)"                        # then source login.sh
    python3 account.py delete <id>
    python3 account.py list                                      # leftover accounts

``create`` signs up through the normal register -> email code -> confirm path.
The code comes from the Resend receiving inbox. The account lives in the
Secrets Manager secret ``skynet/staging/check-account/<id>`` only until
``delete``, which calls ``POST /account/delete`` and then removes the secret.
No command prints a password, token or email code.
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


def email_code(email):
    """Wait for the confirmation code sent to ``email`` in the Resend inbox.

    Args:
        email: Address that the sign-up sent the code to.

    Returns:
        The six-digit code, or ``None`` after about two minutes.
    """
    auth = "Authorization: Bearer " + read_secret("skynet/staging/app")["SMTP_PASSWORD"]
    for _ in range(30):
        time.sleep(4)
        _, text = curl("https://api.resend.com/emails/receiving?limit=100", stdin_header=auth)
        for mail in json.loads(text).get("data", []):
            if email in json.dumps(mail.get("to", "")):
                _, text = curl(f"https://api.resend.com/emails/receiving/{mail['id']}", stdin_header=auth)
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


def create(target, label):
    """Sign up, verify and store a fresh account, then print its id.

    Args:
        target: ``staging`` or ``pr-<n>``.
        label: Issue or PR label, for example ``SKY-123``.
    """
    app, api = targets(target)
    ident = f"{target}-{re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')}-{secrets.token_hex(3)}"
    email = f"{ident}@{INBOX}"
    password = secrets.token_urlsafe(18) + "A1!"
    status, _ = curl(f"{app}/api/register", body={"email": email, "password": password})
    print("register: HTTP", status)
    if status >= 300:
        sys.exit(1)
    code = email_code(email)
    if not code:
        sys.exit("account: no email code arrived; the unverified account stays, report the id " + ident)
    status, _ = curl(f"{app}/api/email-verify/confirm", body={"email": email, "code": code})
    print("confirm: HTTP", status)
    if status >= 300:
        sys.exit(1)
    value = {"email": email, "password": password, "target": target, "app": app, "api": api, "label": label}
    with tempfile.NamedTemporaryFile("w", suffix=".json") as tmp:
        Path(tmp.name).chmod(0o600)
        json.dump(value, tmp)
        tmp.flush()
        run(
            [
                *AWS,
                "secretsmanager",
                "create-secret",
                "--name",
                PREFIX + ident,
                "--description",
                f"Check account for {label} on {target}; delete after the check",
                "--secret-string",
                f"file://{tmp.name}",
            ]
        )
    print(f"id={ident}")


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


def delete(ident):
    """Delete the account through the app, then remove its secret.

    Args:
        ident: Account id that ``create`` printed.
    """
    value = read_secret(PREFIX + ident)
    app, api = value["app"], value["api"]
    with tempfile.TemporaryDirectory() as tmp:
        jar = str(Path(tmp) / "jar")
        _, text = curl(f"{app}/api/auth/csrf", jar=jar)
        csrf = json.loads(text)["csrfToken"]
        curl(
            f"{app}/api/auth/callback/credentials",
            jar=jar,
            form=True,
            body={"email": value["email"], "password": value["password"], "csrfToken": csrf, "callbackUrl": app},
        )
        _, text = curl(f"{app}/api/auth/session", jar=jar)
        token = (json.loads(text) or {}).get("backendAccessToken", "")
        if not token:
            sys.exit("account: sign-in failed; the account and its secret stay, report the id " + ident)
        header = Path(tmp) / "auth"
        header.touch(mode=0o600)
        header.write_text(f"authorization: Bearer {token}\n")
        status, _ = curl(f"{api}/account/delete", header_file=header, body={"password": value["password"]})
    print("account delete: HTTP", status)
    if status != 200:
        sys.exit("account: delete failed; the secret stays, report the id " + ident)
    run([*AWS, "secretsmanager", "delete-secret", "--secret-id", PREFIX + ident, "--force-delete-without-recovery"])
    print("secret removed")


def list_left():
    """Print the id and creation date of every check account that still exists."""
    out = json.loads(
        run(
            [
                *AWS,
                "secretsmanager",
                "list-secrets",
                "--filters",
                f"Key=name,Values={PREFIX}",
                "--query",
                "SecretList[].[Name,CreatedDate]",
                "--output",
                "json",
            ]
        )
    )
    for name, created in out:
        print(name.removeprefix(PREFIX), created)
    print(f"{len(out)} check accounts")


def main():
    """Parse the command line and run one command."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    make = sub.add_parser("create")
    make.add_argument("--target", required=True)
    make.add_argument("--label", required=True)
    for name in ("env", "delete"):
        sub.add_parser(name).add_argument("id")
    sub.add_parser("list")
    args = parser.parse_args()
    if args.cmd == "create":
        create(args.target, args.label)
    elif args.cmd == "env":
        env(args.id)
    elif args.cmd == "delete":
        delete(args.id)
    else:
        list_left()


if __name__ == "__main__":
    main()
