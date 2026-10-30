"""Génère dv-full-review-0.1.0-racc001-draft.1 depuis draft.1 historique + content racc001."""
import copy
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "contracts"
draft1 = json.loads((BASE / "dv-full-review-0.1.0-draft.1.schema.json").read_text(encoding="utf-8"))
content = json.loads((BASE / "dv-content-0.4.0-racc001-draft.1.schema.json").read_text(encoding="utf-8"))
racc = copy.deepcopy(draft1)
racc["$id"] = "urn:dv:full-review:0.1.0-racc001-draft.1"
racc["properties"]["format"]["const"] = "dv.full-review/0.1.0-racc001-draft.1"
for key, value in content.get("$defs", {}).items():
    racc["$defs"].setdefault(key, value)
speech = copy.deepcopy(content["$defs"]["speechVote"])
hist = draft1["$defs"]["speechVote"]
if "allOf" in hist:
    speech["allOf"] = copy.deepcopy(hist["allOf"])
racc["$defs"]["speechVote"] = speech
out = BASE / "dv-full-review-0.1.0-racc001-draft.1.schema.json"
out.write_text(json.dumps(racc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(out)
