import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function fixture(t) {
  const dir = mkdtempSync(path.join(tmpdir(), "compass-tasks-test-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  mkdirSync(path.join(dir, "scripts"));
  mkdirSync(path.join(dir, "node_modules/.bin"), { recursive: true });
  mkdirSync(path.join(dir, "bin"));
  copyFileSync(path.join(root, "scripts/tasks.mjs"), path.join(dir, "scripts/tasks.mjs"));
  copyFileSync(
    path.join(root, "scripts/workspace-filters.mjs"),
    path.join(dir, "scripts/workspace-filters.mjs"),
  );
  symlinkSync(process.execPath, path.join(dir, "bin/node"));
  writeFileSync(path.join(dir, ".env"), "COMPASS_TASK_TEST_SECRET=local-only\n");
  writeFileSync(path.join(dir, "bin/pnpm"), "#!/usr/bin/env node\nconsole.log('11.3.0');\n", {
    mode: 0o755,
  });
  writeFileSync(
    path.join(dir, "node_modules/.bin/turbo"),
    `#!/usr/bin/env node
console.log(JSON.stringify({
  args: process.argv.slice(2),
  secret: process.env.COMPASS_TASK_TEST_SECRET,
  nodeToolchain: process.env.CI_TOOLCHAIN_ID,
  pythonToolchain: process.env.PYTHON_TOOLCHAIN_ID
}));
`,
    { mode: 0o755 },
  );
  return {
    dir,
    run(...args) {
      const env = { ...process.env, PATH: path.join(dir, "bin") };
      for (const key of ["COMPASS_TASK_TEST_SECRET", "CI_TOOLCHAIN_ID", "PYTHON_TOOLCHAIN_ID"])
        delete env[key];
      const result = spawnSync(process.execPath, ["scripts/tasks.mjs", ...args], {
        cwd: dir,
        env,
        encoding: "utf8",
      });
      assert.equal(result.status, 0, result.stderr);
      return JSON.parse(result.stdout);
    },
  };
}

test("dev forwards Turbo filters and task arguments, loads .env, and needs no Python tools", (t) => {
  const { run } = fixture(t);
  const args = ["dev", "-F", "@compass/home", "--", "--port", "3101"];
  const result = run(...args);
  assert.deepEqual(result.args, ["run", ...args]);
  assert.equal(result.secret, "local-only");
  assert.equal(result.nodeToolchain, undefined);
  assert.equal(result.pythonToolchain, undefined);
});

test("build without uv or a virtualenv stays environment-free and reaches Turbo", (t) => {
  const { run } = fixture(t);
  const result = run("build", "-F", "@compass/home");
  assert.deepEqual(result.args, ["run", "build", "-F", "@compass/home"]);
  assert.equal(result.secret, undefined);
  assert.ok(result.nodeToolchain.includes(process.version));
  assert.ok(result.nodeToolchain.includes("11.3.0"));
  assert.equal(result.pythonToolchain, "unavailable|unavailable");
});

test("cached tasks fingerprint the installed Python toolchain separately", (t) => {
  const { dir, run } = fixture(t);
  mkdirSync(path.join(dir, ".venv/bin"), { recursive: true });
  writeFileSync(
    path.join(dir, "bin/uv"),
    "#!/usr/bin/env node\nconsole.log('uv test-version');\n",
    { mode: 0o755 },
  );
  writeFileSync(
    path.join(dir, ".venv/bin/python"),
    "#!/usr/bin/env node\nconsole.log('Python test-version');\n",
    { mode: 0o755 },
  );
  const result = run("test", "-F", "@compass/agent");
  assert.equal(result.pythonToolchain, "uv test-version|Python test-version");
  assert.ok(!result.nodeToolchain.includes("Python"));
  assert.equal(result.secret, undefined);
});

function dryRun(...args) {
  const result = spawnSync(process.execPath, ["scripts/tasks.mjs", ...args, "--dry-run=json"], {
    cwd: root,
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}

test("home dev/build/typecheck select only home and never schedule Python contracts", () => {
  for (const task of ["dev", "build", "typecheck"]) {
    const graph = dryRun(task, "-F", "home");
    assert.deepEqual(
      graph.tasks.map((entry) => entry.taskId),
      [`@compass/home#${task}`],
    );
    if (task === "dev") {
      assert.equal(graph.tasks[0].resolvedTaskDefinition.cache, false);
      assert.equal(graph.tasks[0].resolvedTaskDefinition.persistent, true);
    }
  }
});

test("Agent seed receives the Retail import credentials while dev gets endpoint policy only", () => {
  const graph = dryRun("dev", "seed", "-F", "agent", "-F", "web");
  const agentDev = graph.tasks.find((entry) => entry.taskId === "@compass/agent#dev");
  const agentSeed = graph.tasks.find((entry) => entry.taskId === "@compass/agent#seed");
  const webSeed = graph.tasks.find((entry) => entry.taskId === "@compass/web#seed");
  for (const key of ["COMPASS_RETAIL_RUN_SCOPE", "COMPASS_RETAIL_RUN_READONLY_DSN"]) {
    assert.ok(agentSeed.resolvedTaskDefinition.passThroughEnv.includes(key));
    assert.ok(!agentDev.resolvedTaskDefinition.passThroughEnv.includes(key));
    assert.ok(!webSeed.resolvedTaskDefinition.passThroughEnv.includes(key));
  }
  assert.ok(
    agentDev.resolvedTaskDefinition.passThroughEnv.includes("COMPASS_DATA_ALLOWED_ENDPOINTS"),
  );
  assert.ok(
    !webSeed.resolvedTaskDefinition.passThroughEnv.includes("COMPASS_DATA_ALLOWED_ENDPOINTS"),
  );
  for (const task of [agentDev, agentSeed]) {
    for (const key of [
      "DATABASE_URL",
      "COMPASS_RETAIL_ALLOWED_ENDPOINTS",
      "COMPASS_MODEL_ENCRYPTION_KEY",
    ])
      assert.ok(task.resolvedTaskDefinition.passThroughEnv.includes(key));
  }
});

test("Web build/typecheck schedule the shared uncached contract generation once", () => {
  const graph = dryRun("build", "typecheck", "-F", "web");
  assert.deepEqual(graph.tasks.map((entry) => entry.taskId).sort(), [
    "//#contracts",
    "@compass/web#build",
    "@compass/web#typecheck",
  ]);
  const contracts = graph.tasks.find((entry) => entry.taskId === "//#contracts");
  assert.equal(contracts.resolvedTaskDefinition.cache, false);
  for (const task of graph.tasks.filter((entry) => entry.package === "@compass/web")) {
    assert.ok(task.dependencies.includes("//#contracts"));
  }
});

test("Python apps and offline tools build and check independently without other apps or contracts", () => {
  for (const app of ["agent", "memory", "data-seed"]) {
    const graph = dryRun("build", "lint", "typecheck", "test", "-F", app);
    assert.equal(graph.tasks.length, 4);
    for (const task of graph.tasks) {
      assert.equal(task.package, `@compass/${app}`);
      assert.ok(task.resolvedTaskDefinition.env.includes("PYTHON_TOOLCHAIN_ID"));
      assert.deepEqual(task.dependencies, []);
    }
  }
});

test("one-shot interactive tasks forward stdin without logging secrets or losing workspace discovery", (t) => {
  const { dir } = fixture(t);
  const app = path.join(dir, "apps/maintenance");
  mkdirSync(app, { recursive: true });
  writeFileSync(
    path.join(dir, "turbo.json"),
    JSON.stringify({ tasks: { "db:manage": { cache: false, interactive: true } } }),
  );
  writeFileSync(
    path.join(dir, "bin/pnpm"),
    `#!/usr/bin/env node
if (process.argv.includes('--recursive')) console.log(JSON.stringify([{name:'@probe/maintenance',path:${JSON.stringify(app)}}]));
else {
 let input=''; for await (const chunk of process.stdin) input+=chunk;
 console.log(JSON.stringify({args:process.argv.slice(2),bytes:Buffer.byteLength(input),secret:process.env.COMPASS_TASK_TEST_SECRET}));
}
`,
    { mode: 0o755 },
  );
  const input = "synthetic-pipe-input\n";
  const result = spawnSync(
    process.execPath,
    [
      "scripts/tasks.mjs",
      "db:manage",
      "-F",
      "maintenance",
      "--",
      "create-user",
      "--password-stdin",
    ],
    { cwd: dir, env: { ...process.env, PATH: path.join(dir, "bin") }, input, encoding: "utf8" },
  );
  assert.equal(result.status, 0, result.stderr);
  assert.ok(!result.stdout.includes(input.trim()));
  assert.deepEqual(JSON.parse(result.stdout), {
    args: ["--filter", "@probe/maintenance", "run", "db:manage", "create-user", "--password-stdin"],
    bytes: Buffer.byteLength(input),
    secret: "local-only",
  });
});

test("offline data tool checks include canonical SQL and keep database acceptance uncached", () => {
  const graph = dryRun("test", "-F", "data-seed");
  const task = graph.tasks[0];
  assert.ok(
    task.resolvedTaskDefinition.inputs.includes(
      "../../mai-specs/998-business-data-seeding-tool/design/business-schema.sql",
    ),
  );
  const integration = dryRun("test:integration", "-F", "data-seed").tasks[0];
  assert.equal(integration.resolvedTaskDefinition.cache, false);
  assert.ok(
    integration.resolvedTaskDefinition.passThroughEnv.includes("COMPASS_RETAIL_TEST_ADMIN_URL"),
  );
  const dev = dryRun("dev");
  assert.equal(
    dev.tasks.find((entry) => entry.taskId === "@compass/data-seed#dev")?.command,
    "<NONEXISTENT>",
  );
});

test("strict Turbo dev environment forwards the Agent runtime and Scheduler settings", () => {
  const agent = dryRun("dev", "-F", "agent").tasks[0];
  for (const key of [
    "COMPASS_SCHEDULER_TOKEN",
    "COMPASS_RUNTIME_CONSUMER_SLOTS",
    "COMPASS_CONTEXT_SNAPSHOT_TTL_DAYS",
    "COMPASS_CONTEXT_SECURITY_MODE",
    "COMPASS_CONTEXTOPS_MCP_ENABLED",
    "COMPASS_RUNTIME_KEY_VERSION",
    "COMPASS_RUNTIME_MAX_ACTIVE",
    "COMPASS_RUNTIME_LEASE_SECONDS",
  ])
    assert.ok(agent.environmentVariables.specified.passThroughEnv.includes(key), key);
});

test("Agent integration runs use the explicitly selected sandbox and its private volumes", () => {
  const task = dryRun("test:integration", "-F", "agent").tasks[0];
  const allowed = task.resolvedTaskDefinition.passThroughEnv;
  for (const key of [
    "COMPASS_SANDBOX_TEST_DOMAIN",
    "COMPASS_SANDBOX_TEST_HOST",
    "COMPASS_SANDBOX_TEST_SLOTS",
    "COMPASS_SANDBOX_TEST_VOLUME_PREFIX",
    "COMPASS_SANDBOX_TEST_CONTROL_CONTAINER",
    "COMPASS_SANDBOX_TEST_NAS_CONTAINER",
  ]) {
    assert.ok(allowed.includes(key), key);
  }
});
