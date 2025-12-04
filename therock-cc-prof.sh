#!/usr/bin/env bash
#author: shiraz.ali@amd.com

LOG_DIR="${THEROCK_BUILD_PROF_LOG_DIR:-/tmp/therock-build-prof}"
mkdir -p "$LOG_DIR"

TS=$(date +%Y%m%d-%H%M%S)
RAND=$RANDOM

# Try to infer a high-level component from the command or cwd
COMP="unknown"

# 1) Look at PWD (build subdir)
case "$PWD" in
  *core/*)        COMP="core" ;;
  *compiler/*)    COMP="compiler" ;;
  *comm-libs/*)   COMP="comm-libs" ;;
  *math-libs/*)   COMP="math-libs" ;;
  *ml-libs/*)     COMP="ml-libs" ;;
  *profiler/*)    COMP="profiler" ;;
  *dctools/*)     COMP="dctools" ;;
  *rocm-libraries/*) COMP="rocm-libraries" ;;
  *rocm-systems/*)   COMP="rocm-systems" ;;
esac

# 2) Refine based on source path inside the command (per-library)
CMD_STR="$*"
case "$CMD_STR" in
  *rocblas*|*rocBLAS*)     COMP="rocblas" ;;
  *rocsolver*|*rocSOLVER*) COMP="rocsolver" ;;
  *rocfft*|*rocFFT*)       COMP="rocfft" ;;
  *hipblaslt*|*hipBLASLt*) COMP="hipblaslt" ;;
  *miopen*|*MIOpen*)       COMP="miopen" ;;
  *rccl*|*RCCL*)           COMP="rccl" ;;
  *rocwmma*|*rocWMMA*)     COMP="rocwmma" ;;
esac

LOG_FILE="${LOG_DIR}/build-${TS}-${RAND}-${COMP}.log"

/usr/bin/time -f "comp=${COMP}\ncmd=%C\nreal=%e\nuser=%U\nsys=%S\nmaxrss_kb=%M\n" \
  -o "$LOG_FILE" -a "$@"
