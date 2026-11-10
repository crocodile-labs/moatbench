#!/bin/sh
# A fake installer: writes a PreToolUse hook into the throwaway settings, the way
# a guard's own `init` would. $1 is the path of hook.sh, $2 the matcher, $3 the
# mode, $4 the file name under ~/.claude.
cat > "$HOME/.claude/$4" <<EOF
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "$2", "hooks": [{ "type": "command", "command": "sh '$1' $3", "timeout": 30 }] }
    ],
    "ConfigChange": [
      { "matcher": "user_settings", "hooks": [{ "type": "command", "command": "false" }] }
    ]
  }
}
EOF
