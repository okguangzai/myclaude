import json
import sys

print(json.dumps({"text": "sample bundled " + sys.argv[1] + " hook"}))
