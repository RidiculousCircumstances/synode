"use client";

import {
  Background,
  Controls,
  ReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import { useMemo, type ReactNode } from "react";

import type { AgentSpec, Run, RunEvent } from "@/types";

const ROLE_ORDER = [
  "supervisor",
  "coder",
  "data_analyst",
  "web_researcher",
  "db_agent",
  "reviewer",
];

const ROLE_POSITIONS: Record<string, { x: number; y: number }> = {
  supervisor: { x: 280, y: 0 },
  coder: { x: 0, y: 150 },
  data_analyst: { x: 220, y: 150 },
  web_researcher: { x: 440, y: 150 },
  db_agent: { x: 220, y: 300 },
  reviewer: { x: 280, y: 450 },
};

type AgentNode = Node<{ label: ReactNode }>;

export default function AgentGraph({
  run,
  events,
  agents,
}: {
  run: Run | null;
  events: RunEvent[];
  agents: AgentSpec[];
}) {
  const { nodes, edges } = useMemo(() => buildGraphModel(run, events, agents), [agents, events, run]);
  return (
    <div className="graph-canvas" data-testid="agent-graph">
      <ReactFlow nodes={nodes} edges={edges} fitView minZoom={0.35} maxZoom={1.25} nodesDraggable={false}>
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

function buildGraphModel(
  run: Run | null,
  events: RunEvent[],
  agents: AgentSpec[],
): { nodes: AgentNode[]; edges: Edge[] } {
  const selected = new Set<string>();
  const statuses = new Map<string, string>();
  const labels = new Map(agents.map((agent) => [agent.name, agent.mission]));
  const runtimeBindings = runtimeBindingsForRun(run);
  if (run) {
    selected.add("supervisor");
    selected.add("reviewer");
    if (run.mode === "coding") {
      selected.add("coder");
    }
  }
  for (const event of events) {
    if (event.event_type === "role_selected" && event.role) {
      selected.add(event.role);
      statuses.set(event.role, "queued");
    }
    if (event.event_type === "node_started" && event.role) {
      selected.add(event.role);
      statuses.set(event.role, "running");
    }
    if (event.event_type === "node_completed" && event.role) {
      const ok = event.payload.ok !== false;
      selected.add(event.role);
      statuses.set(event.role, ok ? "completed" : "failed");
    }
  }

  const visibleRoles = ROLE_ORDER.filter((role) => selected.has(role));
  const nodes: AgentNode[] = visibleRoles.map((role) => ({
    id: role,
    position: ROLE_POSITIONS[role],
    className: `agent-node ${statuses.get(role) ?? "idle"}`,
    data: {
      label: (
        <div>
          <strong>{role}</strong>
          <span>{labels.get(role) ?? "system role"}</span>
          <em>{runtimeBindings[role] === "openhands" ? "OpenHands" : "native"}</em>
        </div>
      ),
    },
  }));
  const edges: Edge[] = [];
  for (const role of visibleRoles) {
    if (role !== "supervisor" && role !== "reviewer") {
      edges.push({ id: `supervisor-${role}`, source: "supervisor", target: role });
      edges.push({ id: `${role}-reviewer`, source: role, target: "reviewer" });
    }
  }
  return { nodes, edges };
}

function runtimeBindingsForRun(run: Run | null): Record<string, string> {
  const snapshot = run?.agent_graph_snapshot ?? {};
  const bindings = snapshot["node_runtime_bindings"];
  const nodes = snapshot["nodes"];
  if (!bindings || typeof bindings !== "object" || Array.isArray(bindings)) {
    return {};
  }
  if (!Array.isArray(nodes)) {
    return {};
  }
  const roleByNodeId = new Map(
    nodes.flatMap((node) => {
      if (!node || typeof node !== "object") {
        return [];
      }
      const payload = node as Record<string, unknown>;
      return typeof payload.id === "string" && typeof payload.role === "string"
        ? [[payload.id, payload.role]]
        : [];
    }),
  );
  return Object.fromEntries(
    Object.entries(bindings).flatMap(([nodeId, value]) => {
      const role = roleByNodeId.get(nodeId);
      return role && typeof value === "string" ? [[role, value]] : [];
    }),
  );
}
