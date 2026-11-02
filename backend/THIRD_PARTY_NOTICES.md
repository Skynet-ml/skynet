# Third-party notices for the Skynet backend

Skynet is licensed under the GNU Affero General Public License v3 (`LICENSE`).
This file lists third-party material that is copied into the source tree or
bundled into the backend and worker image. Python dependencies ship their own
license files in their `*.dist-info/` directories inside the image.

## Copied or adapted source

| Material | Location | Upstream | License |
| --- | --- | --- | --- |
| Meta-Harness proposer skill | `core/service_gateway/optimization/blackbox/upstream_prompts/meta_harness/` | stanford-iris-lab/meta-harness@0cbc31e9, Copyright (c) 2026 Yoonho Lee | MIT, `LICENSE` beside it |
| AutoSaddler plugin and prompt-pack wiring | `core/service_gateway/optimization/blackbox/autosaddler_plugin/`, `autosaddler_runner.py` | microsoft/AutoSaddler@9df6d2e3 | MIT, `LICENSE` beside it |
| ShinkaEvolve runner wiring (EVOLVE-BLOCK program layout, evaluation contract, SEARCH/REPLACE edit format) | `core/service_gateway/optimization/blackbox/shinka_runner.py`, `shinka_bundle.py` | SakanaAI/ShinkaEvolve, `shinka-evolve==0.0.7` on PyPI | Apache-2.0 |
| Meta-Harness loop structure | `core/service_gateway/optimization/blackbox/native_engines.py` | as above | MIT |
| GEPA sources sent to sandboxes | archived at run time with `gepa/LICENSE` | gepa-ai/gepa@0632cdb5, Copyright (c) 2025 Lakshya A Agrawal | MIT |
| Scalar API reference bundle | `core/api/static/scalar/` | scalar/scalar, Copyright (c) 2023-present Scalar | MIT, `LICENSE` beside it |
| Inter and JetBrains Mono web fonts | `core/api/static/scalar/fonts/` | rsms/inter, JetBrains/JetBrainsMono | OFL-1.1, `OFL.txt` beside them |
| Common password list | `core/api/data/common_passwords.txt` | danielmiessler/SecLists, Copyright (c) 2018 Daniel Miessler | MIT, `common_passwords.LICENSE` |

## Bundled runtimes and tools

| Component | Version | License | Notice |
| --- | --- | --- | --- |
| Node.js | 22.22.0 | MIT and bundled third-party licenses | `licenses/node.LICENSE` |
| Deno | 2.6.6 | MIT | `licenses/deno.LICENSE` |
| Pyodide (Deno npm cache) | 0.29.4 | MPL-2.0 | source: https://github.com/pyodide/pyodide/tree/0.29.4 |
| OpenAI Codex CLI | see `sandbox-runtime/package-lock.json` | Apache-2.0 | `licenses/openai-codex.LICENSE`, `licenses/openai-codex.NOTICE` |
| Pi coding agent | see `sandbox-runtime/package-lock.json` | MIT | `licenses/pi-coding-agent.LICENSE` |
| Prime Agent | see `sandbox-runtime/package-lock.json` | MIT | `licenses/prime-agent.LICENSE` |
| Prime Agent kernel Python packages | see `sandbox-runtime/prime-agent-kernel.txt` | MIT, BSD, Apache-2.0; certifi is MPL-2.0 | license texts in their `dist-info` folders |
| OpenCode | see `sandbox-runtime/package-lock.json` | MIT | `LICENSE` inside the package |

ShinkaEvolve (`shinka-evolve==0.0.7`, Apache-2.0, Copyright Sakana AI) is
installed unmodified from PyPI, with the pinned dependencies in
`core/service_gateway/optimization/blackbox/shinka_requirements.txt`, into its
own virtual environment at `/opt/shinka/venv` only in sandbox images built with
`--build-arg INCLUDE_SHINKA_EVOLVE=1` (off by default, so backend/worker
images do not contain it); outside such an image, a run installs it into
a private virtual environment inside the run's sandbox.
When bundled, each package's license text is in its `dist-info` folder. Most are MIT, BSD or
Apache-2.0; certifi and tqdm are MPL-2.0, and `Levenshtein` and
`python-Levenshtein` 0.27.5 are GPL-2.0-or-later
(https://github.com/rapidfuzz/Levenshtein/tree/v0.27.5).

## LGPL Python dependencies

These are installed unmodified as separate packages and may be replaced with
any compatible version. Their license texts are in their `dist-info` folders.

| Package | License | Source |
| --- | --- | --- |
| ldap3 2.9.1 | LGPL-3.0 | https://github.com/cannatag/ldap3/tree/v2.9.1 |
| psycopg2-binary 2.9.11 | LGPL-3.0 with OpenSSL exception | https://github.com/psycopg/psycopg2/tree/2.9.11 |
