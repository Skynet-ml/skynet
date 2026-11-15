# Local AWS Emulator

Reference for coding agents. The always-on rules are in `AGENTS.md`.

The AWS Emulator is a local AWS API server from AWS. It runs in Docker on
`https://127.0.0.1:<port>`. It keeps all state in memory, in the account
`000000000000`. Use it to test AWS code with no cost and no risk to staging.

## Start, stop and connect

```bash
just aws-emulator                    # or: scripts/aws-emulator.sh start
( eval "$(scripts/aws-emulator.sh env)" && aws sts get-caller-identity )
just aws-emulator-stop               # or: scripts/aws-emulator.sh stop
```

- `AWS_EMULATOR_PORT` sets the port. The default is 9297. The container
  name is `aws-emulator-<port>`.
- `start` does nothing when an emulator already answers on the port.
- `start` builds the image `skynet-aws-emulator` from
  `deploy/aws-emulator/Dockerfile` when it is missing or older than 7 days.
  The build always gets the latest release. `AWS_EMULATOR_REBUILD=1` rebuilds now.
- The emulator writes `config`, `emulator_credentials` and `ca.pem` to
  `$HOME/.aws/emulator/<port>/`. A stop deletes them. They change on each start.
- `env` prints the export and unset lines. Run it in a subshell, so your
  real AWS settings come back after it.
- A stop deletes all emulator state. Each start begins empty.

## Safety rules

- Every script and test that calls the emulator first checks two things.
  The endpoint must be `https://127.0.0.1:<port>`. STS must return the account
  `000000000000`. If one check fails, the script stops before any write.
- Remove the real credential variables (`AWS_ACCESS_KEY_ID`,
  `AWS_SESSION_TOKEN`, `AWS_ROLE_ARN` and the others in `scripts/aws-emulator.sh`).
  Use the profile `default` with the emulator's credentials and config files.
- Never turn off TLS verification. Trust `ca.pem` only for emulator calls:
  `AWS_CA_BUNDLE` for the CLI and boto3, and an `ssl` context for httpx.
  Never set `SSL_CERT_FILE`: it replaces the whole trust store.
- The container publishes its port on loopback only. Telemetry is off.
- Never pin an emulator version.
- Never commit an emulator file, a key or a home path. The repository is public.

## The AWS MCP server cannot reach it

The AWS MCP server runs its calls in a remote sandbox. That sandbox cannot
reach `127.0.0.1` on your computer. Use the `aws` CLI or boto3 with the
`env` lines for emulator work. Do not test a change with the MCP server: it
calls real AWS.

## Opt-in tests

```bash
just test-aws-emulator
```

- The recipe starts the emulator, then runs `backend/tests/aws_emulator/`
  and `infra/terraform/scripts/tests/test-aws-emulator.sh`.
- `backend/tests/aws_emulator/` runs the S3 connector and the EC2 sandbox
  provider against the emulator. The EC2 tests replace only the SSH wait.
- The shell test runs `deploy_config`, the ECR helpers of `image-key.sh` and
  the commands of `secrets.sh`, with the real `aws` CLI.
- The shell test writes `skynet/staging/{app,frontend,litellm}` and the
  `skynet-staging-*` task definitions in the emulator. Its guard makes sure
  that these writes never go to real AWS.
- With no emulator, the tests skip. `AWS_EMULATOR_REQUIRED=1` makes a skip a
  failure. The recipe sets it.
- The tests are not part of `just test-unit` or CI.

## What passes and what does not

Probe results against release 1.0.385 on 2026-11-14:

- Pass: STS, S3 (buckets, objects, prefix lists, ranged gets, SigV4 checks),
  EC2 (images, run, describe, terminate, tags, client tokens, the
  `instance_running` waiter), ECR (layer upload, put-image, batch-get-image,
  describe-images), Secrets Manager, SSM parameters, and ECS task definitions.
- ECS tasks never start. Do not test `run_task`, `wait_task_ok` or
  `deploy.sh` here.
- EC2 instances are records only. There is no machine and no SSH.
- `run-instances` accepts any well-formed AMI id. Only `describe-images`
  refuses an unknown one.
- A forced secret delete ends a short time after its answer. A create of the
  same name right after it can fail. Try the create again.
- An HTTP 501 answer means that the emulator does not have the operation.
  It is a gap in the emulator, not a bug in Skynet.

## Parallel agents

Each agent uses its own port, for example `AWS_EMULATOR_PORT=9397`. Never
stop an emulator that another agent started. Stop your own when you finish.
