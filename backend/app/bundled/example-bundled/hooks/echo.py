import json
import sys

print(json.dumps({"text": "example bundled " + sys.argv[1] + " hook"}))
