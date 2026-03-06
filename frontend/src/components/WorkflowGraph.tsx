import { Background, Controls, MarkerType, MiniMap, ReactFlow } from "reactflow";
import type { Edge, Node } from "reactflow";

import type { WorkflowGraph, WorkflowProgress } from "../types";

function styleByStatus(status: string | undefined): { border: string; background: string; color: string; shadow: string } {
  switch (status) {
    case "active":
      return {
        border: "2px solid #d97706",
        background: "#fff7e6",
        color: "#92400e",
        shadow: "0 0 0 3px rgba(217, 119, 6, 0.18)",
      };
    case "done":
      return {
        border: "1px solid #2f855a",
        background: "#ebfff2",
        color: "#1f5136",
        shadow: "0 8px 18px rgba(47, 133, 90, 0.12)",
      };
    case "failed":
      return {
        border: "1px solid #c53030",
        background: "#fff0f0",
        color: "#7f1d1d",
        shadow: "0 8px 18px rgba(197, 48, 48, 0.15)",
      };
    default:
      return {
        border: "1px solid #0f172a1a",
        background: "#fffef9",
        color: "#1f2937",
        shadow: "0 10px 20px rgba(15, 23, 42, 0.05)",
      };
  }
}

function toFlow(graph: WorkflowGraph, progress: WorkflowProgress | null): { nodes: Node[]; edges: Edge[] } {
  const nodeStatus = progress?.node_status ?? {};

  const nodes: Node[] = graph.nodes.map((item, idx) => {
    const status = nodeStatus[item.id];
    const stylePreset = styleByStatus(status);
    return {
      id: item.id,
      position: { x: (idx % 3) * 260, y: Math.floor(idx / 3) * 170 },
      data: {
        label: (
          <div>
            <div>{item.label}</div>
            <div style={{ fontSize: 12, marginTop: 6, opacity: 0.85 }}>状态: {status ?? "pending"}</div>
          </div>
        ),
      },
      type: "default",
      style: {
        borderRadius: 14,
        border: stylePreset.border,
        background: stylePreset.background,
        width: 220,
        fontSize: 13,
        fontWeight: 600,
        color: stylePreset.color,
        padding: 10,
        boxShadow: stylePreset.shadow,
      },
    };
  });

  const edges: Edge[] = graph.edges.map((item, idx) => ({
    id: `edge-${idx}`,
    source: item.source,
    target: item.target,
    markerEnd: { type: MarkerType.ArrowClosed, width: 18, height: 18, color: "#334155" },
    style: { stroke: "#334155", strokeWidth: 1.5 },
  }));

  return { nodes, edges };
}

type Props = {
  graph: WorkflowGraph | null;
  progress: WorkflowProgress | null;
};

export function WorkflowGraphPanel({ graph, progress }: Props) {
  if (!graph) {
    return <div className="panel-empty">Workflow 图加载中...</div>;
  }
  const flow = toFlow(graph, progress);
  return (
    <div className="workflow-box">
      <ReactFlow nodes={flow.nodes} edges={flow.edges} fitView>
        <MiniMap zoomable pannable />
        <Controls />
        <Background gap={16} color="#e2e8f0" />
      </ReactFlow>
    </div>
  );
}
