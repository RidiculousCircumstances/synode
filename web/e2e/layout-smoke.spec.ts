import { expect, test, type Page, type Route } from "@playwright/test";

const runId = "run-layout-smoke-0001";
const threadId = "thread-layout-smoke-0001";
const now = new Date("2026-06-30T00:00:00.000Z").toISOString();

test.beforeEach(async ({ page }) => {
  await installApiRoutes(page);
});

const routes = [
  "/threads",
  `/threads/${threadId}`,
  "/chat",
  "/runs",
  `/runs/${runId}`,
  `/runs/${runId}?tab=timeline`,
  `/runs/${runId}?tab=artifacts`,
  `/runs/${runId}?tab=diff-tests`,
  `/runs/${runId}?tab=agents`,
  `/runs/${runId}?tab=metrics`,
  "/agents",
  "/observability",
  "/settings",
];

for (const path of routes) {
  test(`renders without horizontal overflow: ${path}`, async ({ page }) => {
    await page.goto(path, { waitUntil: "domcontentloaded" });
    await expect(page.locator("#main-content")).toBeVisible();
    await expect(page.getByText("Application error")).toHaveCount(0);

    const overflow = await page.evaluate(() => {
      const root = document.documentElement;
      return Math.max(0, root.scrollWidth - root.clientWidth);
    });
    expect(overflow).toBeLessThanOrEqual(2);
  });
}

test("artifacts and diff tests use full-size work panels", async ({ page }) => {
  await page.goto(`/runs/${runId}?tab=artifacts`, { waitUntil: "domcontentloaded" });
  const artifactCode = page.locator(".payload-panel .large-code").first();
  await expect(artifactCode).toBeVisible();
  await expect.poll(async () => {
    const box = await artifactCode.boundingBox();
    return box?.height ?? 0;
  }).toBeGreaterThan(300);

  await page.goto(`/runs/${runId}?tab=diff-tests`, { waitUntil: "domcontentloaded" });
  const diffPanels = page.locator(".coding-workbench .payload-panel .large-code");
  await expect(diffPanels).toHaveCount(2);
  for (const index of [0, 1]) {
    await expect.poll(async () => {
      const box = await diffPanels.nth(index).boundingBox();
      return box?.height ?? 0;
    }).toBeGreaterThan(300);
  }
});

test("thread chat renders technical run summary compactly", async ({ page }) => {
  await page.goto(`/threads/${threadId}`, { waitUntil: "domcontentloaded" });
  await expect(page.locator(".thread-chat-shell")).toBeVisible();
  await expect(page.locator(".page-cockpit")).toHaveCount(0);
  await expect(page.locator(".chat-code-block code", { hasText: "const stable = true;" })).toBeVisible();
  await expect(page.locator(".run-summary-topline strong", { hasText: "Run summary" })).toBeVisible();
  await expect(page.getByText("mode coding")).toBeVisible();
  await expect(page.getByText("Tool and raw output")).toBeVisible();
  await expect(page.locator(".thread-service-event")).toHaveCount(1);
  await expect(page.locator(".thread-approval-event")).toHaveCount(1);
  await expect(page.locator(".thread-service-event .thread-message-body")).toHaveCount(0);
  await expect(page.locator(".thread-approval-event .thread-message-body")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Approve" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Reject" })).toBeVisible();
  await expect(page.locator(".thread-composer-input")).toBeVisible();
  await expect(page.locator(".thread-followup-form textarea")).toHaveCount(0);
  await expect(page.locator(".thread-runs-panel")).toHaveCount(0);
  await expect.poll(async () => {
    const box = await page.locator(".thread-topbar").boundingBox();
    return box?.height ?? 0;
  }).toBeLessThan(55);
  await expect.poll(async () => {
    const approvalBox = await page.locator(".thread-approval-event").boundingBox();
    const scrollBox = await page.locator(".thread-message-scroll").boundingBox();
    if (!approvalBox || !scrollBox) {
      return 999;
    }
    return approvalBox.x - scrollBox.x;
  }).toBeLessThan(48);
  await expect.poll(async () => {
    const box = await page.locator(".thread-message-scroll").boundingBox();
    return box?.height ?? 0;
  }).toBeGreaterThan(300);
  await expect.poll(async () =>
    page.locator(".thread-message-scroll").evaluate((element) =>
      Math.abs(element.scrollHeight - element.scrollTop - element.clientHeight),
    ),
  ).toBeLessThan(4);

  await page.getByRole("button", { name: /runs/i }).first().click();
  await expect(page.getByRole("dialog", { name: "Run history" })).toBeVisible();
});

test("overlays do not shift the page layout", async ({ page }) => {
  await page.goto("/threads", { waitUntil: "domcontentloaded" });
  await expect(page.locator("#main-content")).toBeVisible();
  await expect(page.locator(".thread-list")).toBeVisible();
  await forcePageScrollbar(page);

  const beforeModal = await layoutBox(page);
  await page.getByRole("button", { name: "New" }).click();
  await expect(page.locator(".modal-layer")).toBeVisible();
  expectLayoutStable(beforeModal, await layoutBox(page));
  await page.locator(".modal-header .icon-button").click();
  await expect(page.locator(".modal-layer")).toHaveCount(0);

  const menuButton = page.getByRole("button", { name: "Open navigation" });
  if (await menuButton.isVisible()) {
    const beforeMenu = await layoutBox(page);
    await menuButton.click();
    await expect(page.getByRole("dialog")).toBeVisible();
    expectLayoutStable(beforeMenu, await layoutBox(page));
    await page.locator(".mobile-close-button").click();
  }

  await page.goto(`/threads/${threadId}`, { waitUntil: "domcontentloaded" });
  await expect(page.locator(".thread-chat-shell")).toBeVisible();
  await forcePageScrollbar(page);
  const beforeDrawer = await layoutBox(page);
  await page.getByRole("button", { name: /runs/i }).first().click();
  await expect(page.getByRole("dialog", { name: "Run history" })).toBeVisible();
  expectLayoutStable(beforeDrawer, await layoutBox(page));
});

test("entity creation opens modal dialogs from list actions", async ({ page }) => {
  await page.goto("/settings", { waitUntil: "networkidle" });
  await expect(page.locator("#main-content")).toBeVisible();
  await expect(page.locator(".entity-modal-form")).toHaveCount(0);
  await forcePageScrollbar(page);

  const newProfileButton = page.getByRole("button", { name: "New profile" });
  await newProfileButton.scrollIntoViewIfNeeded();
  const beforeProfile = await layoutBox(page);
  await newProfileButton.click();
  await expect(page.getByRole("dialog", { name: "New model profile" })).toBeVisible();
  expectLayoutStable(beforeProfile, await layoutBox(page));
  await page.locator(".modal-header .icon-button").click();
  await expect(page.locator(".modal-layer")).toHaveCount(0);

  const newSecretButton = page.getByRole("button", { name: "New secret" });
  await newSecretButton.scrollIntoViewIfNeeded();
  const beforeSecret = await layoutBox(page);
  await newSecretButton.click();
  await expect(page.getByRole("dialog", { name: "New secret" })).toBeVisible();
  expectLayoutStable(beforeSecret, await layoutBox(page));
  await page.locator(".modal-header .icon-button").click();
  await expect(page.locator(".modal-layer")).toHaveCount(0);

  await page.goto("/agents", { waitUntil: "networkidle" });
  await expect(page.locator("#main-content")).toBeVisible();
  await expect(page.locator(".entity-modal-form")).toHaveCount(0);
  await forcePageScrollbar(page);

  const newRoleButton = page.getByRole("button", { name: "New role" });
  await newRoleButton.scrollIntoViewIfNeeded();
  const beforeRole = await layoutBox(page);
  await newRoleButton.click();
  await expect(page.getByRole("dialog", { name: "New role" })).toBeVisible();
  expectLayoutStable(beforeRole, await layoutBox(page));
  await page.locator(".modal-header .icon-button").click();
  await expect(page.locator(".modal-layer")).toHaveCount(0);

  const newGraphButton = page.getByRole("button", { name: "New graph" });
  await newGraphButton.scrollIntoViewIfNeeded();
  const beforeGraph = await layoutBox(page);
  await newGraphButton.click();
  await expect(page.getByRole("dialog", { name: "New graph" })).toBeVisible();
  expectLayoutStable(beforeGraph, await layoutBox(page));
});

test("browser API auto-resolution uses the current host", async ({ page }) => {
  await page.goto("/settings", { waitUntil: "domcontentloaded" });
  await expect(page.locator(".header-title code")).toHaveText("http://127.0.0.1:8787");
});

type LayoutBox = {
  x: number;
  y: number;
  width: number;
};

async function layoutBox(page: Page): Promise<LayoutBox> {
  return page.locator("#main-content").evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return { x: rect.x, y: rect.y, width: rect.width };
  });
}

async function forcePageScrollbar(page: Page) {
  await page.addStyleTag({
    content: `
      body::after {
        content: "";
        display: block;
        height: 1200px;
        pointer-events: none;
      }
    `,
  });
}

function expectLayoutStable(before: LayoutBox, after: LayoutBox) {
  expect(Math.abs(after.x - before.x)).toBeLessThanOrEqual(1);
  expect(Math.abs(after.y - before.y)).toBeLessThanOrEqual(1);
  expect(Math.abs(after.width - before.width)).toBeLessThanOrEqual(1);
}

async function installApiRoutes(page: Page) {
  await page.route("http://127.0.0.1:8787/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/runs") {
      await fulfillJson(route, [runFixture()]);
      return;
    }
    if (url.pathname === "/threads") {
      await fulfillJson(route, [threadFixture()]);
      return;
    }
    if (url.pathname === `/threads/${threadId}`) {
      await fulfillJson(route, threadDetailFixture());
      return;
    }
    if (url.pathname === `/threads/${threadId}/messages`) {
      await fulfillJson(route, threadDetailFixture().messages);
      return;
    }
    if (url.pathname === "/agents") {
      await fulfillJson(route, agentsFixture());
      return;
    }
    if (url.pathname === "/agent-graphs") {
      await fulfillJson(route, agentGraphsFixture());
      return;
    }
    if (url.pathname === "/model-profiles") {
      await fulfillJson(route, modelProfilesFixture());
      return;
    }
    if (url.pathname === "/secrets") {
      await fulfillJson(route, []);
      return;
    }
    if (url.pathname === "/models/health") {
      await fulfillJson(route, [{ provider: "ollama", ok: true, model: "qwen2.5-coder:7b" }]);
      return;
    }
    if (url.pathname === "/metrics/system") {
      await fulfillJson(route, systemFixture());
      return;
    }
    if (url.pathname === `/runs/${runId}`) {
      await fulfillJson(route, runFixture());
      return;
    }
    if (url.pathname === `/runs/${runId}/events`) {
      await fulfillJson(route, eventsFixture());
      return;
    }
    if (url.pathname === `/runs/${runId}/events/stream`) {
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: "",
      });
      return;
    }
    if (url.pathname === `/runs/${runId}/artifacts`) {
      await fulfillJson(route, artifactsFixture());
      return;
    }
    if (url.pathname === `/runs/${runId}/tool-audit`) {
      await fulfillJson(route, auditFixture());
      return;
    }
    if (url.pathname === `/runs/${runId}/approvals`) {
      await fulfillJson(route, approvalsFixture());
      return;
    }
    if (url.pathname === `/runs/${runId}/metrics`) {
      await fulfillJson(route, runMetricsFixture());
      return;
    }
    await route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"not mocked"}' });
  });
}

async function fulfillJson(route: Route, value: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(value),
  });
}

function runFixture() {
  return {
    id: runId,
    thread_id: threadId,
    status: "completed",
    mode: "coding",
    task: "Refactor the layout smoke fixture and verify the diff output stays readable",
    workspace: "/workspace/synode",
    model_provider: "ollama",
    default_model_profile_id: "profile-ollama",
    role_model_profile_ids: {},
    agent_graph_id: "graph-default",
    agent_graph_snapshot: {},
    observability_trace_id: "trace-layout-smoke",
    final_answer: "Implemented a focused layout fixture with full-size artifacts and diff panels.",
    created_at: now,
    updated_at: now,
  };
}

function modelProfilesFixture() {
  return [
    {
      id: "profile-ollama",
      name: "ollama default",
      provider_type: "ollama",
      base_url: "http://127.0.0.1:11434",
      model: "qwen2.5-coder:7b",
      options: {},
      secret_id: null,
      secret_set: false,
      enabled: true,
      created_at: now,
      updated_at: now,
    },
  ];
}

function agentGraphsFixture() {
  return [
    {
      id: "graph-default",
      name: "default",
      role_ids: ["role-supervisor", "role-coder", "role-reviewer"],
      edges: [
        { from_role: "role-supervisor", to_role: "role-coder" },
        { from_role: "role-coder", to_role: "role-reviewer" },
      ],
      default_model_profile_id: "profile-ollama",
      role_model_profile_ids: {},
      is_default: true,
      enabled: true,
      created_at: now,
      updated_at: now,
    },
  ];
}

function threadFixture() {
  return {
    id: threadId,
    title: "Refactor layout smoke fixture",
    status: "active",
    latest_run_id: runId,
    latest_run_status: "completed",
    last_message: "Implemented a focused layout fixture with full-size artifacts and diff panels.",
    created_at: now,
    updated_at: now,
  };
}

function threadDetailFixture() {
  return {
    thread: threadFixture(),
    runs: [runFixture()],
    messages: [
      {
        id: 1,
        thread_id: threadId,
        run_id: runId,
        author_type: "user",
        author_name: "user",
        message_type: "text",
        content:
          "Refactor the layout smoke fixture and verify the diff output stays readable\n\n```ts\nconst stable = true;\n```\n\n- keep chat markdown readable",
        metadata: {},
        created_at: now,
      },
      {
        id: 2,
        thread_id: threadId,
        run_id: runId,
        author_type: "system",
        author_name: "runtime",
        message_type: "run_summary",
        content: "Run started.",
        metadata: { status: "running" },
        created_at: now,
      },
      {
        id: 3,
        thread_id: threadId,
        run_id: runId,
        author_type: "system",
        author_name: "approval",
        message_type: "approval_request",
        content: "Approval required for native.apply_patch: Patch requires human approval",
        metadata: { approval_id: "approval-1", tool_name: "native.apply_patch" },
        created_at: now,
      },
      ...Array.from({ length: 12 }, (_, index) => ({
        id: 4 + index,
        thread_id: threadId,
        run_id: runId,
        author_type: index % 2 === 0 ? "user" : "agent",
        author_name: index % 2 === 0 ? "user" : "coder",
        message_type: "text",
        content:
          index % 2 === 0
            ? `Follow-up context ${index + 1}: keep the chat viewport pinned to relevant recent work.`
            : `Intermediate model note ${index + 1}: the visible area should prioritize actual reasoning output over runtime noise.`,
        metadata: {},
        created_at: now,
      })),
      {
        id: 16,
        thread_id: threadId,
        run_id: runId,
        author_type: "agent",
        author_name: "synode",
        message_type: "final",
        content:
          "Synode run summary:\nMode: coding\n- coder: Inspect the UI and compact technical output\n\n[coder]\nImplemented a focused layout fixture with full-size artifacts and diff panels.\n- native.git_diff: ok {\"stdout\":\"diff --git a/web/src/app/globals.css b/web/src/app/globals.css\\n+ .thread-workbench { display: block; }\"}\n\n[verification]\n{\"ok\":true,\"commands\":[{\"command\":\"npm run test:e2e\",\"status\":\"passed\"}]}",
        metadata: { status: "completed" },
        created_at: now,
      },
    ],
  };
}

function eventsFixture() {
  return [
    { id: 1, run_id: runId, event_type: "run_started", role: null, payload: {}, created_at: now },
    { id: 2, run_id: runId, event_type: "role_selected", role: "supervisor", payload: { confidence: 0.91 }, created_at: now },
    { id: 3, run_id: runId, event_type: "node_started", role: "coder", payload: {}, created_at: now },
    { id: 4, run_id: runId, event_type: "tool_called", role: "coder", payload: { tool: "native.git_diff" }, created_at: now },
    { id: 5, run_id: runId, event_type: "node_completed", role: "coder", payload: { ok: true }, created_at: now },
    { id: 6, run_id: runId, event_type: "run_completed", role: "reviewer", payload: { ok: true }, created_at: now },
  ];
}

function artifactsFixture() {
  return [
    {
      id: "artifact-1",
      run_id: runId,
      kind: "plan",
      path: "artifacts/plan.json",
      content: {
        selected_roles: ["supervisor", "coder", "reviewer"],
        plan: ["inspect repo", "apply patch", "run focused tests"],
        confidence: 0.88,
      },
      created_at: now,
    },
    {
      id: "artifact-2",
      run_id: runId,
      kind: "review",
      path: "artifacts/review.json",
      content: {
        status: "approved",
        findings: [],
      },
      created_at: now,
    },
  ];
}

function auditFixture() {
  return [
    {
      id: 1,
      run_id: runId,
      role: "coder",
      tool_name: "native.git_diff",
      risk: "read",
      status: "completed",
      input: {},
      output: {
        output: {
          stdout: "diff --git a/web/src/app/globals.css b/web/src/app/globals.css\n+ .large-code { min-height: 64vh; }\n",
        },
      },
      approval_id: null,
      created_at: now,
    },
    {
      id: 2,
      run_id: runId,
      role: "coder",
      tool_name: "native.verify",
      risk: "read",
      status: "completed",
      input: {},
      output: {
        output: {
          commands: [{ command: "npm run build", status: "passed", duration_ms: 1200 }],
        },
      },
      approval_id: null,
      created_at: now,
    },
  ];
}

function approvalsFixture() {
  return [
    {
      id: "approval-1",
      run_id: runId,
      tool_name: "native.apply_patch",
      action: "apply patch",
      reason: "Patch requires human approval",
      payload: {},
      status: "approved",
      decision_reason: "approved from smoke fixture",
      created_at: now,
      decided_at: now,
    },
  ];
}

function runMetricsFixture() {
  return {
    run_id: runId,
    status: "completed",
    duration_ms: 4523,
    event_count: 6,
    model_call_count: 4,
    tool_call_count: 3,
    approval_count: 1,
    pending_approval_count: 0,
    failed_tool_call_count: 0,
    token_usage: { input_tokens: 1200, output_tokens: 800, total_tokens: 2000 },
    provider_usage: {
      ollama: { input_tokens: 1200, output_tokens: 800, total_tokens: 2000 },
    },
    latency_ms_by_role: { supervisor: 320, coder: 1200, reviewer: 540 },
  };
}

function systemFixture() {
  return {
    process: {
      pid: 42,
      uptime_seconds: 600,
      cpu_percent: 12.5,
      memory_rss_bytes: 180 * 1024 * 1024,
      memory_percent: 4.2,
    },
    gpu: [
      {
        available: true,
        name: "layout-gpu",
        utilization_percent: 18,
        memory_used_mb: 900,
        memory_total_mb: 8192,
        error: null,
      },
    ],
  };
}

function agentsFixture() {
  return [
    agentFixture("role-supervisor", "supervisor", "Route tasks and synthesize plans", []),
    agentFixture("role-coder", "coder", "Inspect repositories and propose patches", ["native.git_diff"]),
    agentFixture("role-reviewer", "reviewer", "Review outputs and verification", []),
  ];
}

function agentFixture(id: string, name: string, mission: string, allowedTools: string[]) {
  return {
    id,
    name,
    mission,
    non_goals: [],
    allowed_tools: allowedTools,
    requires_approval_for: [],
    output_contract: "",
    builtin: true,
    enabled: true,
    created_at: now,
    updated_at: now,
  };
}
