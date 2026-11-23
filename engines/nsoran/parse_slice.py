"""Attribute each DRB.UEThpDl sample in the xApp log to its UE, then report
per-slice mean throughput (UEs 1-5 = slice A/qci9, 6-10 = slice B/qci7)."""
import re
import sys

# The DU indication identifies the UE as amf_ue_ngap_id, which packs the IMSI
# in the high bits (consecutive UEs differ by exactly 0xFF000000; the serving
# cell only moves the low bits, so this survives handovers).
_IMSI_UNIT = 0xFF000000

cur = None                      # imsi-slot of the KPM section being read
thp = {}                        # imsi-slot -> [kbps samples]
for line in sys.stdin:
    m = re.search(r"amf_ue_ngap_id = (\d+)", line)
    if m:
        cur = int(m.group(1)) // _IMSI_UNIT
        continue
    m = re.search(r"DRB\.UEThpDl\S*\s*=\s*([\d.]+)", line)
    if m and cur is not None:
        thp.setdefault(cur, []).append(float(m.group(1)))

if not thp:
    print("  NO DU THROUGHPUT SAMPLES (run died?)")
    sys.exit(1)

# rank the slots: lowest slot = IMSI 1, next = IMSI 2, ...
imsi_of = {slot: rank + 1 for rank, slot in enumerate(sorted(thp))}
thp = {imsi_of[s]: v for s, v in thp.items()}
ids = sorted(thp)
print(f"  UEs seen (as IMSI): {ids}")
a = [v for i in ids if i <= 5 for v in thp[i]]
b = [v for i in ids if i > 5 for v in thp[i]]
ma = sum(a) / len(a) / 1000 if a else 0.0
mb = sum(b) / len(b) / 1000 if b else 0.0
ratio = ma / mb if mb > 0 else float("inf")
print(f"  slice qci9 (UE 1-5) : {ma:6.2f} Mbps/UE   ({len(a)} samples)")
print(f"  slice qci7 (UE 6-10): {mb:6.2f} Mbps/UE   ({len(b)} samples)")
print(f"  ratio qci9/qci7     : {ratio:5.2f}x")
