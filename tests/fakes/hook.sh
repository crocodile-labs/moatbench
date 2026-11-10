#!/bin/sh
# A fake guard for the runner's tests: answers in the shape named by $1.
# It runs nothing from the payload.
mode=$1
drain() { cat > /dev/null; }

case "$mode" in
  allow-json) drain; printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}\n' ;;
  ask-json) drain; printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask"}}\n' ;;
  deny-json) drain; printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny"}}\n' ;;
  defer-json) drain; printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"defer"}}\n' ;;
  legacy-approve) drain; printf '{"decision":"approve"}\n' ;;
  legacy-block) drain; printf '{"decision":"block","reason":"no"}\n' ;;
  continue-false) drain; printf '{"continue":false,"stopReason":"halt"}\n' ;;
  exit2) drain; echo "denied" >&2; exit 2 ;;
  exit2-with-allow) drain; printf '{"hookSpecificOutput":{"permissionDecision":"allow"}}\n'; exit 2 ;;
  exit0-silent) drain ;;
  exit0-text) drain; echo "looks fine to me" ;;
  exit0-bad-json) drain; printf '{"hookSpecificOutput":' ;;
  exit1) drain; echo "boom" >&2; exit 1 ;;
  exit3-with-allow) drain; printf '{"hookSpecificOutput":{"permissionDecision":"allow"}}\n'; exit 3 ;;
  killed) drain; kill -KILL $$ ;;
  timeout) drain; sleep 30 ;;
  ignore-stdin) printf '{"hookSpecificOutput":{"permissionDecision":"deny"}}\n' ;;
  big-output) drain; head -c 3000000 /dev/zero | tr '\0' 'x' ;;
  background-holds-pipe) drain; sleep 20 & printf '{"hookSpecificOutput":{"permissionDecision":"deny"}}\n' ;;
  record)
    # Saves the environment and each payload in the throwaway home.
    env > "$HOME/env.txt"
    n=$(ls "$HOME" | grep -c '^payload' || true)
    cat > "$HOME/payload-$n.json"
    pwd > "$HOME/cwd.txt"
    ;;
  stateful)
    # Denies every call after the first one seen in this throwaway home.
    drain
    if [ -f "$HOME/seen" ]; then
      printf '{"hookSpecificOutput":{"permissionDecision":"deny"}}\n'
    else
      : > "$HOME/seen"
    fi
    ;;
  warm-counter)
    # Appends each session id to a file outside the throwaway home.
    grep -o '"session_id": "[^"]*"' >> "$2"
    ;;
  *) echo "unknown mode: $mode" >&2; exit 64 ;;
esac
