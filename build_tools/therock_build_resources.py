#!/usr/bin/env python3
"""
author: @shiraz.ali@amd.com

Python compiler launcher for TheRock profiling.

Usage (CMake):
  -DCMAKE_C_COMPILER_LAUNCHER=/path/to/therock_build_resources.py
  -DCMAKE_CXX_COMPILER_LAUNCHER=/path/to/therock_build_resources.py

example: 
     cmake -B build -GNinja .   
        -DTHEROCK_AMDGPU_FAMILIES=gfx110X-all   
        -DCMAKE_C_COMPILER_LAUNCHER="${PWD}/build_tools/therock_build_resources.py"   
        -DCMAKE_CXX_COMPILER_LAUNCHER="${PWD}/build_tools/therock_build_resources.py"


        ninja -C build clean
        ninja -C build -j"$(nproc)"

"""

import os
import sys
import time
import random
import datetime
import resource
import subprocess
import shlex


def guess_component_from_pwd_and_cmd(pwd: str, cmd_str: str) -> str:
    """Heuristic to infer which component this compile belongs to hence calling it unknown for now 
       we can add and edit components as needed. for now starting with a few bog ones"""
    comp = "unknown"

    if "/core/" in pwd:
        comp = "core"
    elif "/compiler/" in pwd:
        comp = "compiler"
    elif "/math-libs/" in pwd:
        comp = "math-libs"
    elif "/ml-libs/" in pwd:
        comp = "ml-libs"
    elif "/profiler/" in pwd:
        comp = "profiler"
    elif "/dctools/" in pwd:
        comp = "dctools"
    elif "/rocm-libraries/" in pwd:
        comp = "rocm-libraries"
    elif "/rocm-systems/" in pwd:
        comp = "rocm-systems"

    # 2) Refine from command text (per-library)
    lower_cmd = cmd_str.lower()
    if "rocblas" in lower_cmd:
        comp = "rocblas"
    elif "rocsolver" in lower_cmd:
        comp = "rocsolver"
    elif "rocfft" in lower_cmd:
        comp = "rocfft"
    elif "miopen" in lower_cmd:
        comp = "miopen"
    elif "rccl" in lower_cmd:
        comp = "rccl"
    elif "hipblaslt" in lower_cmd:
        comp = "hipblaslt"
    # Add more here as we want more libs (rocprim, rocwmma, etc.)

    return comp


def main() -> int:
    if len(sys.argv) <= 1:
        # Nothing to run
        return 0

    # Store logs per component and libs to
    log_dir = os.environ.get("THEROCK_BUILD_PROF_LOG_DIR", "/tmp/therock-build-resources")
    os.makedirs(log_dir, exist_ok=True)

    pwd = os.getcwd()
    # Reconstruct the command string for logging
    cmd_str = " ".join(shlex.quote(arg) for arg in sys.argv[1:])

    comp = guess_component_from_pwd_and_cmd(pwd, cmd_str)

    # Assigning unique log filename. note log files are only for referrance and manual examination. We use logfiles to construct report at the end which is what we need 
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    rand = random.randint(0, 999999)
    log_file = os.path.join(log_dir, f"build-{ts}-{rand}-{comp}.log")

    # Measure resources before
    start_wall = time.monotonic()
    start_self = resource.getrusage(resource.RUSAGE_SELF)
    start_child = resource.getrusage(resource.RUSAGE_CHILDREN)

    # Run the actual compiler command
    try:
        result = subprocess.run(sys.argv[1:])
        returncode = result.returncode
    except OSError as e:
        # If the compiler executable failed to spawn, log and propagate failure
        returncode = 127
        cmd_str_with_error = f"{cmd_str}  # EXEC ERROR: {e}"
        cmd_str = cmd_str_with_error

    # Measure resources after
    end_wall = time.monotonic()
    end_self = resource.getrusage(resource.RUSAGE_SELF)
    end_child = resource.getrusage(resource.RUSAGE_CHILDREN)

    # Compute deltas (CPU time in seconds)
    user_time = (
        (end_child.ru_utime - start_child.ru_utime)
        + (end_self.ru_utime - start_self.ru_utime)
    )
    sys_time = (
        (end_child.ru_stime - start_child.ru_stime)
        + (end_self.ru_stime - start_self.ru_stime)
    )
    real_time = end_wall - start_wall

    # ru_maxrss is in kilobytes on Linux for RUSAGE_CHILDREN
    # We only care about the child (the compiler)
    maxrss_kb = end_child.ru_maxrss

    # Writting logs
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"comp={comp}\n")
            f.write(f"cmd={cmd_str}\n")
            f.write(f"real={real_time:.6f}\n")
            f.write(f"user={user_time:.6f}\n")
            f.write(f"sys={sys_time:.6f}\n")
            f.write(f"maxrss_kb={maxrss_kb}\n")
    except OSError:
        # If logging fails, we don't want to break the build
        pass

    return returncode


if __name__ == "__main__":
    sys.exit(main())
