import { Background, Controls, MarkerType, MiniMap, ReactFlow } from "reactflow";
import type { Edge, Node } from "reactflow";

import type { WorkflowGraph, WorkflowProgress } from "../types";

const STATUS_LABELS: Record<string, string> = {
  pending: "待处理", active: "进行中", done: "已完成", failed: "失败",
};

function styleByStatus(status: string | undefined): { border: string; background: string; color: string; shadow: string } {
  switch (status) {
    case "active":
      return {
        border: "2px solid #b9791d",
        background: "#fff4dc",
        color: "#7c4b08",
        shadow: "0 0 0 3px rgba(185, 121, 29, 0.13)",
      };
    case "done":
      return {
        border: "1px solid #3d8a65",
        background: "#e8f4ed",
        color: "#245f43",
        shadow: "0 5px 12px rgba(39, 118, 83, 0.09)",
      };
    case "failed":
      return {
        border: "1px solid #c85a5a",
        background: "#fdecec",
        color: "#8a2929",
        shadow: "0 5px 12px rgba(181, 60, 60, 0.1)",
      };
    default:
      return {
        border: "1px solid #d6dde2",
        background: "#ffffff",
        color: "#26333e",
        shadow: "0 4px 10px rgba(23, 32, 42, 0.05)",
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
      position: { x: (idx % 3) * 240, y: Math.floor(idx / 3) * 150 },
      data: {
        label: (
          <div>
            <div>{item.label}</div>
            <div style={{ fontSize: 11, marginTop: 6, opacity: 0.78 }}>状态：{STATUS_LABELS[status ?? "pending"]}</div>
          </div>
        ),
      },
      type: "default",
      style: {
        borderRadius: 6,
        border: stylePreset.border,
        background: stylePreset.background,
        width: 200,
        fontSize: 12,
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
    markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: "#687580" },
    style: { stroke: "#8a959f", strokeWidth: 1.3 },
  }));

  return { nodes, edges };
}

type Props = {
  graph: WorkflowGraph | null;
  progress: WorkflowProgress | null;
};

export function WorkflowGraphPanel({ graph, progress }: Props) {
  if (!graph) {
    return <div className="panel-empty">工作流加载中...</div>;
  }
  const flow = toFlow(graph, progress);
  return (
    <>
    <ol className="workflow-mobile" aria-label="工作流步骤">
      {graph.nodes.map((item, index) => {
        const status = progress?.node_status[item.id] ?? "pending";
        return <li key={item.id} className={status}><span>{index + 1}</span><div>{item.label}<small>{STATUS_LABELS[status]}</small></div></li>;
      })}
    </ol>
    <div className="workflow-box">
      <ReactFlow nodes={flow.nodes} edges={flow.edges} fitView nodesDraggable={false}>
        <MiniMap zoomable pannable nodeBorderRadius={4} />
        <Controls />
        <Background gap={18} color="#e4e9ec" />
      </ReactFlow>
    </div>
    </>
  );
}
