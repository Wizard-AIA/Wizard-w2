import time
from collections import deque
from threading import Lock

class MetricsCollector:
    def __init__(self, window_size=1000):
        self.window_size = window_size
        self._lock = Lock()
        
        # Histograms store a tuple of (count, sum, deque_of_values)
        self.histograms = {
            "wizard_request_duration_seconds": {},
            "wizard_llm_ttft_seconds": {},
            "wizard_llm_tokens_per_sec": {},
            "wizard_turn_duration_seconds": {},
        }
        self.histogram_labels = {
            "wizard_request_duration_seconds": ("method", "endpoint", "status"),
            "wizard_llm_ttft_seconds": ("provider", "model"),
            "wizard_llm_tokens_per_sec": ("provider", "model"),
            "wizard_turn_duration_seconds": (),
        }
        
        self.counters = {
            "wizard_errors_total": {},
            "wizard_llm_tokens_total": {},
        }
        self.counter_labels = {
            "wizard_errors_total": ("type",),
            "wizard_llm_tokens_total": ("type",),
        }

    def _get_or_create_histogram(self, name, label_values):
        key = tuple(label_values)
        if key not in self.histograms[name]:
            self.histograms[name][key] = {"count": 0, "sum": 0.0, "values": deque(maxlen=self.window_size)}
        return self.histograms[name][key]

    def _get_or_create_counter(self, name, label_values):
        key = tuple(label_values)
        if key not in self.counters[name]:
            self.counters[name][key] = 0
        return key

    def record_histogram(self, name, label_values, value):
        with self._lock:
            if name not in self.histograms:
                return
            h = self._get_or_create_histogram(name, label_values)
            h["count"] += 1
            h["sum"] += value
            h["values"].append(value)
            
    def record_ttft(self, provider, model, ttft):
        self.record_histogram("wizard_llm_ttft_seconds", (provider, model), ttft)

    def record_request(self, method: str, endpoint: str, status: int | str, duration: float):
        self.record_histogram("wizard_request_duration_seconds", (method, endpoint, str(status)), duration)

    def record_error(self, error_type: str, amount: int = 1):
        self.increment_counter("wizard_errors_total", (error_type,), amount)

    def increment_counter(self, name, label_values, amount=1):
        with self._lock:
            if name not in self.counters:
                return
            key = self._get_or_create_counter(name, label_values)
            self.counters[name][key] += amount

    def _format_labels(self, label_names, label_values, extra_labels=None):
        pairs = []
        for k, v in zip(label_names, label_values):
            pairs.append(f'{k}="{v}"')
        if extra_labels:
            for k, v in extra_labels.items():
                pairs.append(f'{k}="{v}"')
        if not pairs:
            return ""
        return "{" + ",".join(pairs) + "}"

    def generate_prometheus_text(self):
        lines = []
        with self._lock:
            # Counters
            for name, data in self.counters.items():
                if not data: continue
                lines.append(f"# HELP {name} Counter metric")
                lines.append(f"# TYPE {name} counter")
                label_names = self.counter_labels[name]
                for label_values, count in data.items():
                    label_str = self._format_labels(label_names, label_values)
                    lines.append(f"{name}{label_str} {count}")
            
            # Histograms
            for name, data in self.histograms.items():
                if not data: continue
                lines.append(f"# HELP {name} Histogram metric")
                lines.append(f"# TYPE {name} summary")
                label_names = self.histogram_labels[name]
                for label_values, h in data.items():
                    count = h["count"]
                    h_sum = h["sum"]
                    values = sorted(list(h["values"]))
                    
                    if values:
                        p50 = values[int(len(values) * 0.5)]
                        p90 = values[int(len(values) * 0.9)]
                        p95 = values[int(len(values) * 0.95)]
                        p99 = values[int(len(values) * 0.99)]
                    else:
                        p50 = p90 = p95 = p99 = 0.0

                    base_labels = dict(zip(label_names, label_values))
                    
                    # Quantiles
                    for q, v in (("0.5", p50), ("0.9", p90), ("0.95", p95), ("0.99", p99)):
                        l_str = self._format_labels(label_names, label_values, {"quantile": q})
                        lines.append(f"{name}{l_str} {v}")
                        
                    l_str_sum_count = self._format_labels(label_names, label_values)
                    lines.append(f"{name}_sum{l_str_sum_count} {h_sum}")
                    lines.append(f"{name}_count{l_str_sum_count} {count}")
        
        return "\n".join(lines) + "\n"

metrics = MetricsCollector()
