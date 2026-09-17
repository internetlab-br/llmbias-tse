#!/usr/bin/env bash
# Sincroniza as conversas entre as duas VMs do Gemini (uniao, preferindo completas).
set -e
K=~/.ssh/llmbias_azure
SP=$(mktemp -d); mkdir -p "$SP/g1" "$SP/g2" "$SP/uniao"
scp -i $K -o BatchMode=yes -q azureuser@IP_DA_VM_GEMINI:'~/llmbias-tse/data/experimento_2026_08/conversations/gemini_*.json' "$SP/g1/" 2>/dev/null || true
scp -i $K -o BatchMode=yes -q azureuser@IP_DA_VM_GEMINI2:'~/llmbias-tse/data/experimento_2026_08/conversations/gemini_*.json' "$SP/g2/" 2>/dev/null || true
python - "$SP" <<'PY'
import json, shutil, sys
from pathlib import Path
sp = Path(sys.argv[1]); esp = {"voto": 7, "genero": 10, "integridade": 10}
def st(p):
    try: rec = json.loads(p.read_text(encoding="utf-8"))
    except Exception: return False, 0
    e = rec.get("eixo") or p.stem.split("_")[-1]; t = rec.get("turns", [])
    return (len(t) == esp.get(e, 10) and all(x.get("ok") for x in t)), sum(1 for x in t if x.get("ok"))
best = {}
for d in ("g1", "g2"):
    for p in (sp / d).glob("*.json"):
        ok, n = st(p); cur = best.get(p.name)
        if cur is None or (ok, n) > (cur[1], cur[2]): best[p.name] = (p, ok, n)
for nome, (p, ok, n) in best.items(): shutil.copy2(p, sp / "uniao" / nome)
print(f"uniao: {len(best)} conversas ({sum(1 for _,o,_ in best.values() if o)} completas)")
PY
for ip in IP_DA_VM_GEMINI IP_DA_VM_GEMINI2; do
  scp -i $K -o BatchMode=yes -q "$SP/uniao/"*.json azureuser@$ip:~/llmbias-tse/data/experimento_2026_08/conversations/
done
echo "sincronizado nas duas VMs"
rm -rf "$SP"
