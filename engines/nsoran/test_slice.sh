#!/bin/bash
# NSG slice-quota differential test (go/no-go gate for the slicing feature).
# Same saturating 2-slice scenario three times; only the quota env changes:
#   A) no quotas          -> slices share roughly equally
#   B) qci9:60% qci7:10%  -> slice 9 must out-serve slice 7
#   C) qci9:10% qci7:60%  -> and the ratio must FLIP
# UEs 1-5 are slice qci=9, UEs 6-10 are slice qci=7, every UE asks 30 Mbps
# (300 Mbps total >> cell capacity, so the quotas are the binding constraint).
set -u
GP="1550.0,1750.0;2050.0,1750.0;1550.0,2250.0;2050.0,2250.0;1800.0,2000.0"
UP="2050,2000;1950,2170;1750,2240;1580,2130;1550,1930;1650,1780;1850,1760;2020,1850;1700,2100;1900,2050"
SL="9,30;9,30;9,30;9,30;9,30;7,30;7,30;7,30;7,30;7,30"

run_one () {  # $1=label  $2=quota spec
  docker exec -e NSG_N_GNB=5 -e NSG_N_UE=10 -e NSG_BW_HZ=50000000.0 \
    -e NSG_GNB_POS="$GP" -e NSG_UE_POS="$UP" -e NSG_GNB_POW="44;44;44;44;44" \
    -e NSG_UE_SLICES="$SL" -e NSG_SLICE_QUOTAS="$2" \
    nsg-engine bash /root/nsoran/run_scenario.sh 20 >/dev/null 2>&1
  echo "---- $1 (quotas='$2') ----"
  docker exec nsg-engine bash -c 'grep -E "\[NSG\] (slice quota|per-IMSI)" /tmp/scn0.log | head -3'
  docker exec nsg-engine cat /tmp/xapp.log | python3 /root/nsoran/parse_slice.py
}

run_one "A baseline"   ""
run_one "B favor qci9" "9:60;7:10"
run_one "C favor qci7" "9:10;7:60"
