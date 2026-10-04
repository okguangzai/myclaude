import json
import sys

payload = json.load(sys.stdin)
action = sys.argv[1]
if action == "count":
    text = payload["text"]
    result = {"words": len(text.split()), "characters": len(text)}
elif (
    action == "pre"
    and payload.get("tool_name") in {"write_file", "edit_file"}
    and payload.get("tool_input", {}).get("path", "").endswith(".protected")
):
    result = {
        "hookSpecificOutput": {
            "permissionDecision": "deny",
            "permissionDecisionReason": "学习插件保护 .protected 文件",
        }
    }
elif action == "hello":
    result = {"message": "Hello from the Python plugin", "arguments": payload.get("arguments")}
else:
    result = {"event": action, "ok": True}
print(json.dumps(result, ensure_ascii=False))
