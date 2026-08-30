from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"

@dataclass
class DAGNode:
    id: str
    name: str
    action: str
    payload: dict = field(default_factory=dict)
    status: NodeStatus = NodeStatus.PENDING
    result: Any = None
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None

@dataclass
class DAGEdge:
    source_id: str
    target_id: str
    condition: Callable[['DAGNode'], bool] | None = None

class ExecutionDAG:
    def __init__(self):
        self.nodes: dict[str, DAGNode] = {}
        self.edges: list[DAGEdge] = []
        self._adjacency_list: dict[str, list[str]] = {}
        self._in_degree: dict[str, int] = {}

    def add_node(self, node: DAGNode) -> None:
        self.nodes[node.id] = node
        if node.id not in self._adjacency_list:
            self._adjacency_list[node.id] = []
            self._in_degree[node.id] = 0

    def add_edge(self, source_id: str, target_id: str, condition: Callable[[DAGNode], bool] | None = None) -> None:
        if source_id not in self.nodes or target_id not in self.nodes:
            raise ValueError("Source or target node not in DAG")
        
        self.edges.append(DAGEdge(source_id=source_id, target_id=target_id, condition=condition))
        self._adjacency_list[source_id].append(target_id)
        self._in_degree[target_id] += 1
        
        # Check for cycles
        if self._has_cycle():
            # Rollback
            self.edges.pop()
            self._adjacency_list[source_id].remove(target_id)
            self._in_degree[target_id] -= 1
            raise ValueError("Adding this edge introduces a cycle")

    def _has_cycle(self) -> bool:
        visited = set()
        rec_stack = set()

        def dfs(node_id: str) -> bool:
            visited.add(node_id)
            rec_stack.add(node_id)
            
            for neighbor in self._adjacency_list.get(node_id, []):
                if neighbor not in visited:
                    if dfs(neighbor):
                        return True
                elif neighbor in rec_stack:
                    return True
            
            rec_stack.remove(node_id)
            return False

        for node_id in self.nodes:
            if node_id not in visited:
                if dfs(node_id):
                    return True
        return False

    def topological_sort(self) -> list[DAGNode]:
        in_degree = self._in_degree.copy()
        queue = [node_id for node_id, degree in in_degree.items() if degree == 0]
        sorted_nodes = []
        
        while queue:
            current_id = queue.pop(0)
            sorted_nodes.append(self.nodes[current_id])
            
            for neighbor in self._adjacency_list.get(current_id, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
                    
        return sorted_nodes

    def get_ready_nodes(self) -> list[DAGNode]:
        ready = []
        for node in self.nodes.values():
            if node.status != NodeStatus.PENDING:
                continue
            
            is_ready = True
            for edge in self.edges:
                if edge.target_id == node.id:
                    source_node = self.nodes[edge.source_id]
                    if source_node.status != NodeStatus.COMPLETED:
                        is_ready = False
                        break
            if is_ready:
                ready.append(node)
        return ready

    def to_dict(self) -> dict:
        nodes_dict = {
            node_id: {
                "id": node.id,
                "name": node.name,
                "action": node.action,
                "payload": node.payload,
                "status": node.status.value,
                "result": node.result,
                "error": node.error,
                "started_at": node.started_at,
                "finished_at": node.finished_at,
            }
            for node_id, node in self.nodes.items()
        }
        edges_list = [
            {
                "source_id": edge.source_id,
                "target_id": edge.target_id,
                # Conditions are not serialized as they are Callables
            }
            for edge in self.edges
        ]
        return {"nodes": nodes_dict, "edges": edges_list}

    @classmethod
    def from_dict(cls, data: dict) -> 'ExecutionDAG':
        dag = cls()
        for node_data in data.get("nodes", {}).values():
            node = DAGNode(
                id=node_data["id"],
                name=node_data["name"],
                action=node_data["action"],
                payload=node_data.get("payload", {}),
                status=NodeStatus(node_data["status"]),
                result=node_data.get("result"),
                error=node_data.get("error"),
                started_at=node_data.get("started_at"),
                finished_at=node_data.get("finished_at"),
            )
            dag.add_node(node)
            
        for edge_data in data.get("edges", []):
            dag.add_edge(edge_data["source_id"], edge_data["target_id"])
            
        return dag
