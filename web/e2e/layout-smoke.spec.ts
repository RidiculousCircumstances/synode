import { expect, test, type Page, type Route } from "@playwright/test";

const runId = "run-layout-smoke-0001";
const threadId = "thread-layout-smoke-0001";
const streamRunId = "run-stream-smoke-0001";
const streamThreadId = "thread-stream-smoke-0001";
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
  "/workflows",
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

test("thread chat renders structured run report compactly", async ({ page }) => {
  await page.goto(`/threads/${threadId}`, { waitUntil: "domcontentloaded" });
  await expect(page.locator(".thread-chat-shell")).toBeVisible();
  await expect(page.locator(".page-cockpit")).toHaveCount(0);
  await expect(page.locator(".chat-code-block code", { hasText: "const stable = true;" })).toBeVisible();
  await expect(page.locator(".run-report-head strong", { hasText: "Changes applied and verified" })).toBeVisible();
  await expect(page.locator(".run-report-tile", { hasText: "Patch" })).toBeVisible();
  await expect(page.locator(".run-report-tile", { hasText: "Verification" })).toBeVisible();
  await expect(page.getByText("Synode run summary:")).toHaveCount(0);
  await expect(page.locator(".thread-service-event")).toHaveCount(0);
  await expect(page.locator(".thread-approval-event")).toHaveCount(1);
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

  await expect(page.locator(".run-report-plan .status-badge[title='data_analyst']")).toHaveCount(2);
  await expect(page.locator(".run-report-plan .status-badge[title='coder']")).toHaveCount(1);

  await page.getByRole("button", { name: /runs/i }).first().click();
  await expect(page.getByRole("dialog", { name: "Run history" })).toBeVisible();
});

test("thread approval approve resumes the run", async ({ page }) => {
  await page.goto(`/threads/${threadId}`, { waitUntil: "domcontentloaded" });
  const approveRequest = page.waitForRequest(
    (request) => request.method() === "POST" && request.url().endsWith("/approvals/approval-1/approve"),
  );
  const resumeRequest = page.waitForRequest(
    (request) => request.method() === "POST" && request.url().endsWith(`/runs/${runId}/resume`),
  );

  await page.getByRole("button", { name: "Approve" }).click();

  await approveRequest;
  await resumeRequest;
});

test("thread chat renders streamed agent output", async ({ page }) => {
  await page.goto(`/threads/${streamThreadId}`, { waitUntil: "domcontentloaded" });
  await expect(page.locator(".thread-streaming-message")).toBeVisible();
  await expect(page.locator(".thread-streaming-message")).toContainText("Inspecting the repository and preparing a concise answer.");
  await expect(page.locator(".thread-streaming-message")).not.toContainText("streaming output");
  await expect(page.locator(".thread-streaming-message")).not.toContainText("streamed output");
  await expect(page.locator(".thread-service-event.live")).toContainText("Receiving output");
});

test("run events group repeated role labels", async ({ page }) => {
  await page.goto(`/runs/${runId}`, { waitUntil: "domcontentloaded" });
  await expect(page.locator(".overview-grid .event-role-header .status-badge[title='coder']")).toHaveCount(1);
  await expect(page.locator(".event-row-compact .status-badge[title='coder']")).toHaveCount(0);

  await page.goto(`/runs/${runId}?tab=timeline`, { waitUntil: "domcontentloaded" });
  await expect(page.locator(".timeline-list .event-role-header .status-badge[title='coder']")).toHaveCount(1);
  await expect(page.locator(".timeline-row .status-badge[title='coder']")).toHaveCount(0);
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
  await page.getByRole("button", { name: /^Roles/ }).click();
  await newRoleButton.scrollIntoViewIfNeeded();
  const beforeRole = await layoutBox(page);
  await newRoleButton.click();
  await expect(page.getByRole("dialog", { name: "New role" })).toBeVisible();
  expectLayoutStable(beforeRole, await layoutBox(page));
  await page.locator(".modal-header .icon-button").click();
  await expect(page.locator(".modal-layer")).toHaveCount(0);

  const newGraphButton = page.getByRole("button", { name: "New graph" });
  await page.getByRole("button", { name: /^Graphs/ }).click();
  await newGraphButton.scrollIntoViewIfNeeded();
  const beforeGraph = await layoutBox(page);
  await newGraphButton.click();
  await expect(page.getByRole("dialog", { name: "New graph" })).toBeVisible();
  expectLayoutStable(beforeGraph, await layoutBox(page));
});

test("configuration screens edit profiles roles and graph templates", async ({ page }) => {
  await page.goto("/settings", { waitUntil: "networkidle" });
  const profileRow = page.locator(".model-profile-row").first();
  await expect(profileRow).toBeVisible();

  const testRequestPromise = page.waitForRequest(
    (request) => request.method() === "POST" && request.url().endsWith("/model-profiles/profile-ollama/test"),
  );
  await profileRow.getByRole("button", { name: "Test" }).click();
  await testRequestPromise;
  await expect(profileRow.locator(".profile-test-result")).toContainText("structured output: ok");

  const profilePatchPromise = page.waitForRequest(
    (request) => request.method() === "PATCH" && request.url().endsWith("/model-profiles/profile-ollama"),
  );
  await profileRow.getByRole("button", { name: "Edit" }).click();
  await expect(page.getByRole("dialog", { name: "Edit model profile" })).toBeVisible();
  await page.getByRole("textbox", { name: "Model" }).fill("qwen2.5-coder:7b-instruct");
  await page.getByRole("button", { name: "Save profile" }).click();
  await profilePatchPromise;

  await page.goto("/agents", { waitUntil: "networkidle" });
  await page.getByRole("button", { name: /^Roles/ }).click();
  const rolePatchPromise = page.waitForRequest(
    (request) => request.method() === "PATCH" && request.url().endsWith("/agents/role-coder"),
  );
  await page.locator("tr", { hasText: "coder" }).getByRole("button", { name: "Edit" }).click();
  await expect(page.getByRole("dialog", { name: "Edit role" })).toBeVisible();
  await page.getByRole("textbox", { name: "Mission" }).fill("Inspect codebases and prepare scoped patches");
  await page.getByRole("button", { name: "Save role" }).click();
  await rolePatchPromise;

  const graphCreatePromise = page.waitForRequest(
    (request) => request.method() === "POST" && request.url().endsWith("/agent-graphs"),
  );
  await page.getByRole("button", { name: /^Graphs/ }).click();
  await page.getByRole("button", { name: "New graph" }).click();
  await expect(page.getByRole("dialog", { name: "New graph" })).toBeVisible();
  await page.getByRole("button", { name: "Create graph" }).click();
  const graphCreateRequest = await graphCreatePromise;
  const graphPayload = graphCreateRequest.postDataJSON() as {
    nodes: Array<{ id: string; role_id: string }>;
    node_edges: Array<{ from_node: string; to_node: string }>;
  };
  expect(graphPayload.nodes.map((node) => node.role_id)).toEqual([
    "role-supervisor",
    "role-coder",
    "role-reviewer",
  ]);
  expect(graphPayload.node_edges).toEqual([
    { from_node: "supervisor", to_node: "coder" },
    { from_node: "coder", to_node: "reviewer" },
  ]);
});

test("workflows uses compact tabbed tables", async ({ page }) => {
  await page.goto("/workflows", { waitUntil: "networkidle" });
  await expect(page.locator(".agents-layout")).toHaveCount(0);
  await expect(page.locator(".compact-table")).toBeVisible();
  await expect(page.locator("tr", { hasText: "default" })).toBeVisible();

  await page.getByRole("button", { name: /^Roles/ }).click();
  await expect(page.locator(".compact-table")).toBeVisible();
  await expect(page.locator("tr", { hasText: "coder" })).toBeVisible();

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(2);
});

test("browser API auto-resolution uses the current host", async ({ page }) => {
  await page.goto("/settings", { waitUntil: "domcontentloaded" });
  await expect(page.locator(".header-title code")).toHaveText("http://127.0.0.1:8787");
});

test("theme switcher applies Synode themes", async ({ page }) => {
  await page.goto("/threads", { waitUntil: "networkidle" });
  const menuButton = page.getByRole("button", { name: "Open navigation" });
  if (await menuButton.isVisible()) {
    await expect(menuButton).toBeVisible();
    await menuButton.click();
    await expect(page.locator(".mobile-nav")).toBeVisible();
  }
  const themeSelect = page.locator('select[aria-label="Theme"]:visible');

  await expect(themeSelect).toBeVisible();
  await themeSelect.selectOption("moss-lantern");
  await expect(page.locator("html")).toHaveClass(/moss-lantern/);

  await themeSelect.selectOption("gruvbox-material-light");
  await expect(page.locator("html")).toHaveClass(/gruvbox-material-light/);
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
    const method = route.request().method();
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
    if (url.pathname === `/threads/${streamThreadId}`) {
      await fulfillJson(route, streamThreadDetailFixture());
      return;
    }
    if (url.pathname === `/threads/${threadId}/messages`) {
      await fulfillJson(route, threadDetailFixture().messages);
      return;
    }
    if (url.pathname === "/agents") {
      if (method === "POST") {
        await fulfillJson(route, { ...agentFixture("role-created", "new_role", "Created from smoke test", []), ...routeJsonBody(route) });
        return;
      }
      await fulfillJson(route, agentsFixture());
      return;
    }
    if (url.pathname.startsWith("/agents/") && method === "PATCH") {
      const roleId = url.pathname.split("/").at(-1) ?? "role-coder";
      const role = agentsFixture().find((agent) => agent.id === roleId) ?? agentFixture(roleId, "patched_role", "Patched role", []);
      await fulfillJson(route, { ...role, ...routeJsonBody(route), updated_at: now });
      return;
    }
    if (url.pathname === "/agent-graphs") {
      if (method === "POST") {
        await fulfillJson(route, { ...agentGraphsFixture()[0], id: "graph-created", ...routeJsonBody(route), created_at: now, updated_at: now });
        return;
      }
      await fulfillJson(route, agentGraphsFixture());
      return;
    }
    if (url.pathname.startsWith("/agent-graphs/") && method === "PATCH") {
      const graphId = url.pathname.split("/").at(-1) ?? "graph-default";
      const graph = agentGraphsFixture().find((item) => item.id === graphId) ?? agentGraphsFixture()[0];
      await fulfillJson(route, { ...graph, ...routeJsonBody(route), updated_at: now });
      return;
    }
    if (url.pathname === "/model-profiles") {
      if (method === "POST") {
        await fulfillJson(route, { ...modelProfilesFixture()[0], id: "profile-created", ...routeJsonBody(route), created_at: now, updated_at: now });
        return;
      }
      await fulfillJson(route, modelProfilesFixture());
      return;
    }
    if (url.pathname.startsWith("/model-profiles/") && url.pathname.endsWith("/test") && method === "POST") {
      const profileId = url.pathname.split("/").at(-2) ?? "profile-ollama";
      await fulfillJson(route, modelProfileTestFixture(profileId));
      return;
    }
    if (url.pathname.startsWith("/model-profiles/") && method === "PATCH") {
      const profileId = url.pathname.split("/").at(-1) ?? "profile-ollama";
      const profile = modelProfilesFixture().find((item) => item.id === profileId) ?? modelProfilesFixture()[0];
      await fulfillJson(route, { ...profile, ...routeJsonBody(route), updated_at: now });
      return;
    }
    if (url.pathname === "/secrets") {
      await fulfillJson(route, []);
      return;
    }
    if (url.pathname === "/tools") {
      await fulfillJson(route, { tools: ["native.fs_read", "native.git_diff", "native.apply_patch"] });
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
    if (url.pathname === "/runtime/status") {
      await fulfillJson(route, runtimeFixture());
      return;
    }
    if (url.pathname === "/runtime/sandbox") {
      await fulfillJson(route, runtimeFixture().sandbox);
      return;
    }
    if (url.pathname === `/runs/${runId}`) {
      await fulfillJson(route, runFixture());
      return;
    }
    if (url.pathname === `/runs/${streamRunId}`) {
      await fulfillJson(route, streamRunFixture());
      return;
    }
    if (url.pathname === `/runs/${runId}/resume`) {
      await fulfillJson(route, { status: "scheduled" });
      return;
    }
    if (url.pathname === `/runs/${runId}/events`) {
      await fulfillJson(route, eventsFixture());
      return;
    }
    if (url.pathname === `/runs/${runId}/report`) {
      await fulfillJson(route, runReportFixture());
      return;
    }
    if (url.pathname === `/runs/${streamRunId}/events`) {
      await fulfillJson(route, streamEventsFixture());
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
    if (url.pathname === `/runs/${streamRunId}/events/stream`) {
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
    if (url.pathname === "/approvals/approval-1/approve") {
      await fulfillJson(route, { status: "approved" });
      return;
    }
    if (url.pathname === "/approvals/approval-1/reject") {
      await fulfillJson(route, { status: "rejected" });
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

function routeJsonBody(route: Route): Record<string, unknown> {
  const raw = route.request().postData();
  if (!raw) {
    return {};
  }
  const parsed: unknown = JSON.parse(raw);
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
    return {};
  }
  return parsed as Record<string, unknown>;
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

function streamRunFixture() {
  return {
    ...runFixture(),
    id: streamRunId,
    thread_id: streamThreadId,
    status: "running",
    task: "Explain the current coding plan while streaming public agent output",
    final_answer: null,
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

function modelProfileTestFixture(profileId: string) {
  return {
    profile_id: profileId,
    ok: true,
    provider_type: "ollama",
    model: "qwen2.5-coder:7b",
    capabilities: {
      streaming: true,
      structured_output: true,
    },
    checks: [
      { name: "health", ok: true, supported: true, latency_ms: 12, error: null },
      { name: "structured_output", ok: true, supported: true, latency_ms: 18, error: null },
      { name: "streaming", ok: true, supported: true, latency_ms: 24, error: null },
    ],
  };
}

function agentGraphsFixture() {
  return [
    {
      id: "graph-default",
      name: "default",
      graph_schema_version: 2,
      nodes: [
        { id: "supervisor", role_id: "role-supervisor", label: "supervisor", kind: "control" },
        { id: "coder", role_id: "role-coder", label: "coder", kind: "worker" },
        { id: "reviewer", role_id: "role-reviewer", label: "reviewer", kind: "control" },
      ],
      node_edges: [
        { from_node: "supervisor", to_node: "coder" },
        { from_node: "coder", to_node: "reviewer" },
      ],
      default_model_profile_id: "profile-ollama",
      role_model_profile_ids: {},
      node_runtime_bindings: {
        supervisor: "native_langgraph",
        coder: "native_langgraph",
        reviewer: "native_langgraph",
      },
      node_contracts: {
        supervisor: "supervisor_decision",
        coder: "worker_agent_output",
        reviewer: "reviewer_decision",
      },
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

function streamThreadFixture() {
  return {
    id: streamThreadId,
    title: "Stream agent output",
    status: "active",
    latest_run_id: streamRunId,
    latest_run_status: "running",
    last_message: "Explain the current coding plan while streaming public agent output",
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
        message_type: "run_report",
        content: "Changes applied and verified\nVerification: passed\nPatch: ok (1 file)",
        metadata: { status: "completed", run_report: runReportFixture() },
        created_at: now,
      },
    ],
  };
}

function streamThreadDetailFixture() {
  return {
    thread: streamThreadFixture(),
    runs: [streamRunFixture()],
    messages: [
      {
        id: 1,
        thread_id: streamThreadId,
        run_id: streamRunId,
        author_type: "user",
        author_name: "user",
        message_type: "text",
        content: "Explain the current coding plan while streaming public agent output",
        metadata: {},
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
    { id: 4, run_id: runId, event_type: "tool_started", role: "coder", payload: { tool_name: "native.git_diff", display: { title: "native.git_diff started", status: "running", tone: "info" } }, created_at: now },
    { id: 5, run_id: runId, event_type: "tool_completed", role: "coder", payload: { tool_name: "native.git_diff", status: "ok", display: { title: "native.git_diff completed", status: "ok", tone: "success" } }, created_at: now },
    { id: 6, run_id: runId, event_type: "node_completed", role: "coder", payload: { ok: true }, created_at: now },
    { id: 7, run_id: runId, event_type: "run_completed", role: "reviewer", payload: { ok: true }, created_at: now },
  ];
}

function runReportFixture() {
  return {
    version: 1,
    run_id: runId,
    thread_id: threadId,
    mode: "coding",
    status: "completed",
    headline: "Changes applied and verified",
    summary: "Implemented a focused layout fixture with full-size artifacts and diff panels.",
    plan: [
      { role: "data_analyst", task: "Profile the sample metrics", status: "planned", tool_count: 0 },
      { role: "data_analyst", task: "Compare trend changes", status: "planned", tool_count: 0 },
      { role: "coder", task: "Inspect the UI and compact technical output", status: "planned", tool_count: 1 },
    ],
    role_outputs: [
      {
        role: "coder",
        summary: "Implemented a focused layout fixture with full-size artifacts and diff panels.",
        tool_count: 1,
        failed_tool_count: 0,
        risks: [],
      },
    ],
    patch_results: {
      status: "ok",
      raw_count: 1,
      files: [
        {
          path: "web/src/app/globals.css",
          operation: "modified",
          status: "ok",
          summary: "Thread workbench stays compact.",
          error: null,
        },
      ],
    },
    verification: {
      status: "passed",
      reason: null,
      commands: [{ command: "npm run test:e2e", status: "passed", summary: "layout smoke passed" }],
    },
    tool_activity: [
      {
        role: "coder",
        tool_name: "native.git_diff",
        status: "ok",
        risk: "read",
        title: "native.git_diff completed",
        target: "web/src/app/globals.css",
        approval_id: null,
      },
    ],
    blockers: [],
    advisory: [],
    diagnostics: {},
    raw_refs: {},
    artifact_id: "artifact-report",
    created_at: now,
  };
}

function streamEventsFixture() {
  return [
    { id: 1, run_id: streamRunId, event_type: "run_started", role: null, payload: {}, created_at: now },
    { id: 2, run_id: streamRunId, event_type: "node_started", role: "coder", payload: { node: "graph_worker" }, created_at: now },
    {
      id: 3,
      run_id: streamRunId,
      event_type: "model_stream_started",
      role: "coder",
      payload: { stream_id: "stream-1", role: "coder" },
      created_at: now,
    },
    {
      id: 4,
      run_id: streamRunId,
      event_type: "model_token_delta",
      role: "coder",
      payload: { stream_id: "stream-1", role: "coder", index: 1, delta: "Inspecting the repository " },
      created_at: now,
    },
    {
      id: 5,
      run_id: streamRunId,
      event_type: "model_token_delta",
      role: "coder",
      payload: { stream_id: "stream-1", role: "coder", index: 2, delta: "and preparing a concise answer." },
      created_at: now,
    },
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

function runtimeFixture() {
  return {
    queue_depth: 1,
    running_count: 1,
    cancelling_count: 0,
    stale_running_count: 0,
    worker_concurrency: 2,
    secrets_configured: true,
    queue: {
      backend: "procrastinate",
      available: true,
      detail: "Procrastinate queue is reachable",
      queue_name: "synode_runs",
      pending_jobs: 1,
      running_jobs: 1,
      failed_jobs: 0,
    },
    execution_backends: {
      native_langgraph: {
        backend: "native_langgraph",
        available: true,
        detail: "native LangGraph backend is available",
      },
      openhands: {
        backend: "openhands",
        available: false,
        detail: "OpenHands backend is disabled",
      },
    },
    workers: [
      {
        worker_id: "layout-worker:slot-1",
        hostname: "layout-host",
        pid: 42,
        status: "running",
        current_run_id: streamRunId,
        started_at: now,
        heartbeat_at: now,
      },
    ],
    sandbox: {
      backend: "process",
      available: true,
      detail: "process backend with local limits",
      cpu_seconds: 30,
      memory_mb: 512,
      disk_mb: 1024,
      output_max_bytes: 12000,
    },
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
