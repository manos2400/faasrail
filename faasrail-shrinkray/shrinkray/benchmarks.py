from bisect import bisect
from collections import OrderedDict
import json
from typing import Optional

import pandas as pd

from utils import flatten2d


# Memory values with at most 2 set bits, which are relatively
# close to each other (at most 3 positions away).
MEM_MIB_VALUES = [
    # Sorted by construction
    (1 << e) + ((1 << e - i) if i else 0)
    for e in [7, 8, 9, 10, 11]
    for i in [0, 3, 2, 1]
]
MICROVM_EXTRA_MEM_MIB = 50


def _quantize_mem_size(mem_val: int) -> int:
    idx = bisect(MEM_MIB_VALUES, mem_val + MICROVM_EXTRA_MEM_MIB)
    if idx >= len(MEM_MIB_VALUES):
        raise ValueError(
            f"mem_val={mem_val} MiB exceeds maximum quantizable size "
            f"({MEM_MIB_VALUES[-1] - MICROVM_EXTRA_MEM_MIB} MiB)"
        )
    return MEM_MIB_VALUES[idx]


def load_benchmarks_config(benchmarks_file_path: Optional[str] = None) -> OrderedDict:
    """
    Load benchmark configurations from file.
    
    :param benchmarks_file_path: Path to benchmarks JSON file (optional)
    :return: OrderedDict of benchmark configurations. If no file is provided, returns an empty OrderedDict.
    """
    benchmarks = OrderedDict()
    
    if benchmarks_file_path:
        with open(benchmarks_file_path, "r") as f:
            user_benchmarks = json.load(f)
            for name, config in user_benchmarks.items():
                benchmarks[name] = config
    
    return benchmarks

# Default benchmark configurations for FaasCell functions
FAASCELL_FUNCTIONS = OrderedDict(
    # These "memory_mb" fields exist only for compatibility with 0.0.2-dev
    {
        "chameleon": {
            "image": "docker.io/ckatsak/snaplace-fbpml-chameleon:0.0.3",
            "memory_mb": 640,
        },
        "rnn_serving": {
            "image": "docker.io/ckatsak/snaplace-fbpml-rnn_serving:0.0.3",
            "memory_mb": 384,
            "process_args": "/usr/local/bin/python3 /bench/server.py",  # XXX
        },
        "cnn_serving_geol": {
            "image": "docker.io/ckatsak/snaplace-fbpml-cnn_serving_geol:0.0.3",
            "memory_mb": 384,
        },
        "cnn_serving": {
            "image": "docker.io/ckatsak/snaplace-fbpml-cnn_serving:0.0.2-dev",  # XXX
            "memory_mb": 256,
        },
        "helloworld": {
            "image": "docker.io/ckatsak/snaplace-fbpml-helloworld:0.0.3",
            "memory_mb": 128,
        },
        "image_rotate": {
            "image": "docker.io/ckatsak/snaplace-fbpml-image_rotate:0.0.3",
            "memory_mb": 128,
        },
        "json_serdes": {
            "image": "docker.io/ckatsak/snaplace-fbpml-json_serdes:0.0.3",
            "memory_mb": 256,
        },
        "new_lr_serving": {
            "image": "docker.io/ckatsak/snaplace-fbpml-new_lr_serving:0.0.3",
            "memory_mb": 256,
        },
        "lr_serving": {
            "image": "docker.io/ckatsak/snaplace-fbpml-lr_serving:0.0.3",
            "memory_mb": 256,
        },
        "lr_training": {
            "image": "docker.io/ckatsak/snaplace-fbpml-new_lr_training:0.0.3",  # XXX
            "memory_mb": 448,
        },
        "matmul_fb": {
            "image": "docker.io/ckatsak/snaplace-fbpml-matmul_fb:0.0.3",
            "memory_mb": 192,
        },
        "pyaes": {
            "image": "docker.io/ckatsak/snaplace-fbpml-pyaes:0.0.3",
            "memory_mb": 128,
        },
        "video_processing": {
            "image": "docker.io/ckatsak/snaplace-fbpml-video_processing:0.0.3",
            "memory_mb": 128,
        },
    }
)

def workload_json_entries(workloads: pd.DataFrame, benchmarks_config: Optional[OrderedDict] = None) -> list[str]:
    """
    :param workloads: A dataframe produced by `preprocess.workloads_preprocess`
    :param benchmarks_config: Optional benchmark configurations (uses default if None)
    """
    if benchmarks_config is None:
        benchmarks_config = FAASCELL_FUNCTIONS
    ret = []
    wl_names = set()  # wl: workload
    # Record all unique Workloads and their memory footprint
    for workload in flatten2d(workloads["workloads"].tolist()):
        benchmark_info = benchmarks_config[workload.benchmark]
        # The "or" holds for either every iteration (icy2) or none of them (icy1)
        memory_mb = workload.memory_mb or benchmark_info["memory_mb"]
        wl_name = workload.get_name()
        # Verify the uniqueness of wl_name
        assert wl_name not in wl_names, f'"{wl_name}" inserted twice'
        wl_names.add(wl_name)
        workload_info = {
            "id": wl_name,
            "image": benchmark_info["image"],
            "memory": _quantize_mem_size(memory_mb),
        }
        if "process_args" in benchmark_info:
            # Only applies to rnn_serving for now
            workload_info["process_args"] = benchmark_info["process_args"]
        ret.append(workload_info)
    ret.sort(key=lambda wl: wl["id"])
    return ret


def workload_knative_services(workloads: pd.DataFrame, benchmarks_config: Optional[OrderedDict] = None) -> str:
    """
    Generate Knative Service YAML manifests for all workloads.
    
    :param workloads: A dataframe produced by `preprocess.workloads_preprocess`
    :param benchmarks_config: Optional benchmark configurations (uses default if None)
    :return: YAML string containing all Knative Service definitions
    """
    if benchmarks_config is None:
        benchmarks_config = FAASCELL_FUNCTIONS
    
    yaml_services = []
    wl_names = set()
    
    for workload in flatten2d(workloads["workloads"].tolist()):
        benchmark_info = benchmarks_config[workload.benchmark]
        memory_mb = workload.memory_mb or benchmark_info["memory_mb"]
        wl_name = workload.get_name()
        
        # Verify uniqueness
        assert wl_name not in wl_names, f'"{wl_name}" inserted twice'
        wl_names.add(wl_name)
        
        service_name = wl_name.lower()
        memory_limit = f"{_quantize_mem_size(memory_mb)}Mi"
        
        # Build container args if present
        container_args = ""
        if "process_args" in benchmark_info:
            args_list = benchmark_info["process_args"].split()
            args_yaml = "\n".join([f"            - {arg}" for arg in args_list])
            container_args = f"\n          args:\n{args_yaml}"
        
        service_yaml = f"""---
apiVersion: serving.knative.dev/v1
kind: Service
metadata:
  name: {service_name}
spec:
  template:
    spec:
      containers:
        - image: {benchmark_info["image"]}
          ports:
            - containerPort: 80
          resources:
            limits:
              memory: {memory_limit}
            requests:
              memory: {memory_limit}{container_args}
"""
        yaml_services.append(service_yaml)
    
    return "\n".join(sorted(yaml_services))

def workload_dirigent_services(workloads: pd.DataFrame, benchmarks_config: Optional[OrderedDict] = None) -> str:
    """
    Generate Dirigent Service YAML manifests for all workloads.
    
    :param workloads: A dataframe produced by `preprocess.workloads_preprocess`
    :param benchmarks_config: Optional benchmark configurations (uses default if None)
    :return: YAML string containing all Dirigent Service definitions

    YAML looks like:
        ---
        name: "test"
        image: "docker.io/function/dirigent_empty_function:latest"
        port_forwarding:
        guest_port: 80
        protocol: "tcp"
        requested_cpu: 1
        requested_memory: 1
        scaling_upper_bound: 1
        scaling_lower_bound: 0
        cold_start_sweep: false
    """

    if benchmarks_config is None:
        benchmarks_config = FAASCELL_FUNCTIONS
    
    yaml_services = []
    wl_names = set()
    
    for workload in flatten2d(workloads["workloads"].tolist()):
        benchmark_info = benchmarks_config[workload.benchmark]
        memory_mb = workload.memory_mb or benchmark_info["memory_mb"]
        wl_name = workload.get_name()
        
        # Verify uniqueness
        assert wl_name not in wl_names, f'"{wl_name}" inserted twice'
        wl_names.add(wl_name)
        
        memory_request = _quantize_mem_size(memory_mb)
        
        service_yaml = f"""---
name: "{wl_name}"
image: "{benchmark_info["image"]}"
port_forwarding:
    guest_port: 80
    protocol: "tcp"
requested_cpu: 1
requested_memory: {memory_request}
cold_start_sweep: false
        """
        yaml_services.append(service_yaml)

    return "\n".join(sorted(yaml_services))
