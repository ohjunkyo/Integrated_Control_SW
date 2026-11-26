#!/bin/bash
# run_cpp_script.sh

ROOT_EXECUTABLE="root"
ROOT_OPTIONS="-l -b -q"

SCRIPT_FULL_PATH=$1
CONFIG_FILE_PATH=$2

if [ -z "$SCRIPT_FULL_PATH" ] || [ -z "$CONFIG_FILE_PATH" ]; then
    echo "Error: C++ script path or config path not provided." >&2
    exit 1
fi

CONFIG_DIR=$(dirname "$CONFIG_FILE_PATH")

declare -a cpp_args=()
for arg in "${@:3}"; do
    if [ "$arg" == "interactive" ]; then
        ROOT_OPTIONS="-l" 
    else
        cpp_args+=("$arg")
    fi
done

SCRIPT_DIR=$(dirname "$SCRIPT_FULL_PATH")
SCRIPT_NAME=$(basename "$SCRIPT_FULL_PATH")

COMMAND_ARGS=$(IFS=,; echo "${cpp_args[*]}")
COMMAND_STRING="$SCRIPT_NAME($COMMAND_ARGS)"


export ROOT_INCLUDE_PATH="$CONFIG_DIR:$ROOT_INCLUDE_PATH"

COMMAND_TO_RUN="$ROOT_EXECUTABLE $ROOT_OPTIONS '$COMMAND_STRING'"

echo "----------------------------------------"
echo "Changing directory to: $SCRIPT_DIR"
cd "$SCRIPT_DIR"

echo "Executing with ROOT_INCLUDE_PATH=$ROOT_INCLUDE_PATH"
echo "Command: $COMMAND_TO_RUN"
eval $COMMAND_TO_RUN

echo "Script finished."
