import pandas as pd
import pytest

from src.core.agent.dag import DAGNode, ExecutionDAG
from src.core.agent.grounding import check_table_grounding
from src.core.infra.queue import get_queue
from src.core.llm.slm_router import SLMRouter
from src.core.security.sandbox.docker_security import SecurityPolicyError, validate_container_config


def test_dag_execution_and_cycles():
    dag = ExecutionDAG()
    n1 = DAGNode(id="step1", name="load", action="load_data")
    n2 = DAGNode(id="step2", name="clean", action="clean_data")
    n3 = DAGNode(id="step3", name="report", action="generate_report")

    dag.add_node(n1)
    dag.add_node(n2)
    dag.add_node(n3)

    dag.add_edge("step1", "step2")
    dag.add_edge("step2", "step3")

    # Cycle test
    with pytest.raises(ValueError, match="cycle"):
        dag.add_edge("step3", "step1")

    order = [n.id for n in dag.topological_sort()]
    assert order == ["step1", "step2", "step3"]

    # Ready nodes test
    ready = dag.get_ready_nodes()
    assert len(ready) == 1
    assert ready[0].id == "step1"

    # Serialization test
    dag_dict = dag.to_dict()
    restored = ExecutionDAG.from_dict(dag_dict)
    assert len(restored.nodes) == 3
    assert [n.id for n in restored.topological_sort()] == ["step1", "step2", "step3"]


def test_table_level_grounding():
    text = "Here are the regional results:\n\n| Region | Revenue |\n| --- | --- |\n| North | 100 |\n| South | 250 |\n"
    df = pd.DataFrame(
        {
            "Region": ["North", "South"],
            "Revenue": [100, 250],
        }
    )
    result = check_table_grounding(text, [df])
    assert result["tables_found"] == 1
    assert result["grounded"] is True
    assert result["grounding_ratio"] >= 0.99

    # Empty text case
    empty_result = check_table_grounding("No tables here", [df])
    assert empty_result["tables_found"] == 0
    assert empty_result["grounded"] is True


def test_slm_router_classification():
    router = SLMRouter()
    intent, tier = router.route("show first 5 rows and list columns")
    assert intent == "metadata"
    assert tier == "fast"

    intent, tier = router.route("hello there")
    assert intent == "chitchat"
    assert tier == "fast"

    intent, tier = router.route("calculate mean of age")
    assert intent == "lightweight"
    assert tier == "fast"

    intent, tier = router.route("investigate root cause of anomaly using regression forecast")
    assert intent == "deep"
    assert tier == "reasoning"


def test_docker_security_validation():
    # Valid config
    valid_cfg = {
        "Privileged": False,
        "ReadonlyRootfs": True,
        "User": "1000",
        "Binds": ["/tmp/safe:/workspace:ro"],
        "CapDrop": ["ALL"],
    }
    validate_container_config(valid_cfg)

    # Disallow root volume mount
    with pytest.raises(SecurityPolicyError):
        validate_container_config(
            {
                "Privileged": False,
                "Binds": ["/var/run/docker.sock:/var/run/docker.sock"],
            }
        )

    # Disallow Privileged
    with pytest.raises(SecurityPolicyError):
        validate_container_config(
            {
                "Privileged": True,
                "Binds": [],
            }
        )


def test_job_queue_instance():
    q = get_queue()
    assert hasattr(q, "submit")
    assert hasattr(q, "get")
