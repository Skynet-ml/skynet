import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

const srcUrl = new URL("../../../", import.meta.url).href;
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith("@/")) {
      return { shortCircuit: true, url: new URL(`${specifier.slice(2)}.ts`, srcUrl).href };
    }
    return nextResolve(specifier, context);
  },
});

const {
  DEFAULT_LIMIT_CENTS,
  RUN_SPARE_CENTS,
  aggregateTokenSource,
  callHoldKey,
  declaredMetricModel,
  scanMetricModel,
  isSubscriptionModel,
  runFloor,
  runModels,
  startingLimitCents,
} = await import("./run-floor.ts");

test("keyword names do not hide metric declarations", () => {
  const code =
    "from skynet import llm\ndef score(c):\n f(llm=1)\n return llm('x', model='openai/gpt-4.1-mini')";
  assert.deepEqual(scanMetricModel(code), { uses: true, name: "openai/gpt-4.1-mini" });
});

test("rebound helpers are unsafe", () => {
  for (const code of [
    "from skynet import llm\nllm = partial(llm, model='a')\nllm('x', model='a')",
    "from skynet import llm as judge\njudge = partial(judge, model='a')\njudge('x', model='a')",
    "from skynet import *\nllm = partial(llm, model='a')\nllm('x', model='a')",
    "llm = partial(llm, model='a')\nllm('x', model='a')",
    "from skynet import llm as judge\njudge = other\njudge('x', model='a')",
  ])
    assert.deepEqual(scanMetricModel(code), { uses: true, name: null });
});

test("finds declarations on an unimported injected helper", () => {
  assert.equal(
    declaredMetricModel("def score(i):\n    return llm(i, model='openai/judge')"),
    "openai/judge",
  );
  for (const code of [
    "llm = 'x'\nllm(model='a')",
    "def llm(x): return x\nllm(model='a')",
    "def score(llm): return llm(model='a')",
    "from other import foo as llm\nllm(model='a')",
    "import other as llm\nllm(model='a')",
    "for llm in items: pass\nllm(model='a')",
    "match x:\n    case llm: pass\nllm(model='a')",
  ]) {
    assert.equal(declaredMetricModel(code), null);
  }
  for (const code of [
    "x = llm\nllm(model='a')",
    "llm(**options)",
    "llm(model=choice)",
    "llm(model='a'); llm(model='b')",
  ]) {
    assert.equal(declaredMetricModel(code), null);
  }
});

test("the floor is the boxes' opening holds plus a dollar of spare", () => {
  const floor = runFloor({ boxHoldCents: 18.08, callHoldCents: [4.6, 0.3, null] });

  assert.equal(RUN_SPARE_CENTS, 100);
  assert.deepEqual(floor, { boxHoldCents: 19, spareCents: 100, floorCents: 119, unpriced: true });
});

test("a model whose one call holds more than the spare raises the spare to that hold", () => {
  assert.equal(runFloor({ boxHoldCents: 19, callHoldCents: [134.15] }).floorCents, 19 + 135);
});

test("unknown holds still leave the spare as the floor", () => {
  assert.equal(runFloor({ boxHoldCents: null, callHoldCents: [] }).floorCents, RUN_SPARE_CENTS);
});

test("a fresh budget starts at the default limit unless the floor or server minimum is higher", () => {
  assert.equal(startingLimitCents(119), DEFAULT_LIMIT_CENTS);
  assert.equal(startingLimitCents(812.4), 813);
  assert.equal(startingLimitCents(119, 640), 640);
});

test("a call hold is keyed by who pays for the model", () => {
  assert.notEqual(callHoldKey("m", "managed"), callHoldKey("m", "byok"));
});

test("the job is BYOK only when every named model is", () => {
  const model = (name: string, token_source: "managed" | "byok") =>
    ({ name, token_source }) as Parameters<typeof aggregateTokenSource>[0][number];
  assert.equal(aggregateTokenSource([model("a", "byok"), model("b", "byok")]), "byok");
  assert.equal(aggregateTokenSource([model("a", "byok"), model("b", "managed")]), "managed");
  assert.equal(aggregateTokenSource([model("", "managed"), model("a", "byok")]), "byok");
  assert.equal(aggregateTokenSource([]), "managed");
});

test("a subscription run holds nothing for its model calls, so its floor is $0", () => {
  const floor = runFloor({ boxHoldCents: 0, callHoldCents: [134.15], tokenSource: "subscription" });
  assert.deepEqual(floor, { boxHoldCents: 0, spareCents: 0, floorCents: 0, unpriced: false });
  // Only the server's own box hold, if any, remains.
  assert.equal(
    runFloor({ boxHoldCents: 18.08, callHoldCents: [], tokenSource: "subscription" }).floorCents,
    19,
  );
});

test("the job is a subscription job when no named model is managed and one runs on a CLI", () => {
  type Source = "managed" | "byok" | "subscription" | undefined;
  const model = (name: string, token_source: Source) =>
    ({
      name,
      token_source,
      cli_provider: token_source === "subscription" ? "claude" : undefined,
    }) as Parameters<typeof aggregateTokenSource>[0][number];
  assert.equal(aggregateTokenSource([model("a", "subscription")]), "subscription");
  assert.equal(
    aggregateTokenSource([model("a", "subscription"), model("b", "byok")]),
    "subscription",
  );
  assert.equal(
    aggregateTokenSource([model("a", "subscription"), model("b", "managed")]),
    "managed",
  );
  // A config saved before token sources existed counts as managed.
  assert.equal(
    aggregateTokenSource([model("a", "subscription"), model("b", undefined)]),
    "managed",
  );
  assert.equal(isSubscriptionModel(model("a", "subscription")), true);
  assert.equal(isSubscriptionModel(model("a", "byok")), false);
});

test("a calling metric uses the reflection model for the run floor", () => {
  const task = { name: "subscription/claude/anthropic/claude-haiku-4.5" };
  const reflection = { name: "openai/gpt-5" };
  const extra = { name: "openai/gpt-5-mini" };
  const optimizer = {
    choice: "gepa" as const,
    taskModel: task,
    reflectionModel: reflection,
    extraReflectionModels: [extra],
  };
  assert.deepEqual(runModels(optimizer, false), [task, reflection]);
  assert.deepEqual(runModels({ ...optimizer, choice: "shinka_evolve" }, true), [
    task,
    reflection,
    extra,
    reflection,
  ]);
  assert.deepEqual(
    runModels({ ...optimizer, taskModel: { name: " " }, reflectionModel: null }, true),
    [],
  );
});

test("the floor prices the literal model declared by the metric", () => {
  const optimizer = {
    choice: "gepa" as const,
    taskModel: null,
    reflectionModel: { name: "openai/cheap" },
    extraReflectionModels: [],
  };
  assert.equal(
    runModels(optimizer, true, "from skynet import llm\nllm('x', model='anthropic/expensive')").at(
      -1,
    )?.name,
    "anthropic/expensive",
  );
});

test("a declared extra reflection model keeps its source for the floor", () => {
  const extra = {
    name: "anthropic/claude-x",
    token_source: "byok" as const,
    byok_provider: "anthropic",
  };
  const optimizer = {
    choice: "shinka_evolve" as const,
    taskModel: null,
    reflectionModel: { name: "openai/gpt-4o-mini", token_source: "managed" as const },
    extraReflectionModels: [extra],
  };
  assert.deepEqual(
    runModels(optimizer, true, "from skynet import llm\nllm(model='anthropic/claude-x')").at(-1),
    extra,
  );
});

test("a stale extra reflection model does not set the source for other optimizers", () => {
  const optimizer = {
    choice: "gepa" as const,
    taskModel: null,
    reflectionModel: { name: "openai/gpt-4o-mini", token_source: "managed" as const },
    extraReflectionModels: [
      { name: "anthropic/claude-x", token_source: "byok" as const, byok_provider: "anthropic" },
    ],
  };
  assert.equal(
    runModels(optimizer, true, "from skynet import llm\nllm(model='anthropic/claude-x')").at(-1)
      ?.token_source,
    "managed",
  );
});

test("the declared model is marked unpriced when its hold is unknown", () => {
  assert.equal(runFloor({ boxHoldCents: 10, callHoldCents: [2, null] }).unpriced, true);
  assert.equal(runFloor({ boxHoldCents: 10, callHoldCents: [2, 5] }).unpriced, false);
});

test("metric model parsing ignores comments and reads aliases", () => {
  const optimizer = {
    choice: "gepa" as const,
    taskModel: null,
    reflectionModel: { name: "openai/cheap" },
    extraReflectionModels: [],
  };
  assert.equal(
    runModels(
      optimizer,
      true,
      "# llm(model='wrong')\nfrom skynet import llm as judge\njudge('x', model='anthropic/a')",
    ).at(-1)?.name,
    "anthropic/a",
  );
  assert.equal(
    runModels(optimizer, true, "from skynet import llm\nllm(model=choice)").at(-1)?.name,
    "openai/cheap",
  );
  assert.equal(
    runModels(optimizer, true, "import skynet\nskynet . llm(model=(r'openai/' 'judge'))").at(-1)
      ?.name,
    "openai/judge",
  );
  assert.equal(
    runModels(optimizer, true, "from skynet import llm\nllm(model='openai/\\x6audge')").at(-1)
      ?.name,
    "openai/judge",
  );
});

test("unsafe helper values fall back and None does not hide a safe declaration", () => {
  const optimizer = {
    choice: "gepa" as const,
    taskModel: null,
    reflectionModel: { name: "openai/reflection" },
    extraReflectionModels: [],
  };
  for (const code of [
    "from skynet import llm\nx = llm",
    "from skynet import llm\npartial(llm, model='other')",
    "from skynet import llm\nf(llm)",
    "import skynet\nx = skynet.llm",
    "from skynet import llm as judge\nx=judge",
  ]) {
    assert.equal(
      runModels(
        optimizer,
        true,
        `${code}\nfrom skynet import llm\nllm(model='openai/declared')`,
      ).at(-1)?.name,
      "openai/reflection",
    );
  }
  assert.equal(
    runModels(
      optimizer,
      true,
      "from skynet import llm\nllm(model=None); llm(model='openai/declared')",
    ).at(-1)?.name,
    "openai/declared",
  );
  assert.equal(
    runModels(
      optimizer,
      true,
      "from skynet import llm\nllm(model=(None)); llm(model='openai/declared')",
    ).at(-1)?.name,
    "openai/declared",
  );
});

test("unrelated llm names do not hide a declared helper model", () => {
  const optimizer = {
    choice: "gepa" as const,
    taskModel: null,
    reflectionModel: { name: "openai/reflection" },
    extraReflectionModels: [],
  };
  for (const code of ["llm = 'judge'", "output.llm", "output.llm(model=choice)"]) {
    assert.equal(
      runModels(
        optimizer,
        true,
        `${code}\nfrom skynet import llm as judge\njudge(model='openai/declared')`,
      ).at(-1)?.name,
      "openai/declared",
    );
  }
  for (const code of [
    "from skynet import *\nllm(model='openai/declared')",
    "import skynet as s\ns.llm(model='openai/declared')",
  ]) {
    assert.equal(runModels(optimizer, true, code).at(-1)?.name, "openai/declared");
  }
});

test("an unmatched metric model does not inherit a subscription", () => {
  const optimizer = {
    choice: "gepa" as const,
    taskModel: null,
    reflectionModel: {
      name: "subscription/codex/model",
      token_source: "subscription" as const,
      cli_provider: "codex" as const,
    },
    extraReflectionModels: [],
  };
  assert.deepEqual(
    runModels(optimizer, true, "from skynet import llm\nllm(model='openai/judge')").at(-1),
    {
      name: "openai/judge",
      token_source: "managed",
      cli_provider: undefined,
    },
  );
  const globals = globalThis as { window?: unknown };
  const previous = globals.window;
  globals.window = { __APP_ENV__: { localMode: true } };
  try {
    assert.deepEqual(
      runModels(
        optimizer,
        true,
        "from skynet import llm\nllm(model='subscription/claude/anthropic/a')",
      ).at(-1),
      {
        name: "subscription/claude/anthropic/a",
        token_source: "subscription",
        cli_provider: "claude",
      },
    );
  } finally {
    globals.window = previous;
  }
});

test("a local subscription metric does not inherit a BYOK reflection source", () => {
  const globals = globalThis as { window?: unknown };
  const previous = globals.window;
  globals.window = { __APP_ENV__: { localMode: true } };
  try {
    const optimizer = {
      choice: "gepa" as const,
      taskModel: null,
      reflectionModel: { name: "openai/reflection", token_source: "byok" as const },
      extraReflectionModels: [],
    };
    const metric = runModels(
      optimizer,
      true,
      "from skynet import llm\nllm(model='subscription/codex/gpt-5')",
    ).at(-1);
    assert.equal(metric?.token_source, "subscription");
    assert.equal(metric?.cli_provider, "codex");
    const managed = {
      ...optimizer,
      reflectionModel: { name: "openai/reflection", token_source: "managed" as const },
    };
    assert.equal(
      runModels(managed, true, "from skynet import llm\nllm(model='subscription/claude/model')").at(
        -1,
      )?.token_source,
      "subscription",
    );
    const invalid = runModels(
      optimizer,
      true,
      "from skynet import llm\nllm(model='subscription/codex///')",
    ).at(-1);
    assert.equal(invalid?.token_source, "byok");
    const matched = {
      name: "subscription/codex/gpt-5",
      token_source: "byok" as const,
      byok_provider: "custom",
    };
    assert.equal(
      runModels(
        { ...optimizer, taskModel: matched },
        true,
        "from skynet import llm\nllm(model='subscription/codex/gpt-5')",
      ).at(-1),
      matched,
    );
  } finally {
    globals.window = previous;
  }
});

test("a hosted subscription declaration keeps the reflection source", () => {
  const optimizer = {
    choice: "gepa" as const,
    taskModel: null,
    reflectionModel: { name: "openai/reflection", token_source: "byok" as const },
    extraReflectionModels: [],
  };
  const metric = runModels(
    optimizer,
    true,
    "from skynet import llm\nllm(model='subscription/codex/gpt-5')",
  ).at(-1);
  assert.equal(metric?.token_source, "byok");
  assert.equal(metric?.cli_provider, undefined);
});
