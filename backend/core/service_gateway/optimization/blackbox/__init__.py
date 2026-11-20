"""Black-box repository optimization: every run optimizes a repository against its scorer.

Modules: :mod:`protocol` (task / eval server / engine contract),
:mod:`scorer` (score normalization and the remote scorer adapter), :mod:`gepa_engine`,
:mod:`best_of_n` and :mod:`meta_harness` (in-process engines),
:mod:`registry` (engine catalog and capability-based availability),
:mod:`autosaddler` and :mod:`shinka_evolve` (pinned upstream packages run in
the sandbox), :mod:`auto` (the Auto explore → continue strategy) and :mod:`service` (job
entry points used by the worker subprocess and the submissions router).

The repository side uses :mod:`harness` (how each coding agent is installed,
routed and run), :mod:`sandbox` (private workspaces inside the run's outer
Vercel Sandbox) and :mod:`repo_scorer` (scores repository versions in the
trusted parent's boxes).

Deliberately slim: :mod:`core.service_gateway.safe_exec` imports
:mod:`scorer` from here, and :mod:`service` imports ``safe_exec`` back, so
nothing heavier than the docstring may live in this package init.
"""
