import sys
import time
import random

def deploy_canary():
    print("Deploying canary container with target release image...")
    time.sleep(1)

def route_traffic(percentage):
    print(f"Routing {percentage}% traffic to canary...")
    time.sleep(1)

def query_metrics():
    print("Querying canary's /metrics for 60 seconds...")
    # Simulate waiting
    time.sleep(2)
    # Simulate metrics
    total_requests = 1000
    errors = random.randint(5, 15)
    p95_latency = random.uniform(2.0, 3.5)
    return total_requests, errors, p95_latency

def main():
    deploy_canary()
    route_traffic(10)
    
    total_requests, errors, p95_latency = query_metrics()
    
    error_rate = (errors / total_requests) * 100
    print(f"Metrics results - Error rate: {error_rate:.2f}%, p95 Latency: {p95_latency:.2f}s")
    
    if error_rate > 1.0 or p95_latency > 3.0:
        print("[ROLLBACK TRIGGERED: Canary metrics exceeded threshold]")
        print("Stopping canary container and routing 100% traffic back to stable.")
        sys.exit(1)
    else:
        print("[CANARY PROMOTED: All health and quality invariants verified]")
        route_traffic(100)
        sys.exit(0)

if __name__ == "__main__":
    main()
