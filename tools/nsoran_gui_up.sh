#!/bin/bash
# Upstream ns-O-RAN GUI'sini ayaga kaldirir (engine distrosunda, root olarak).
#   wsl -d Ubuntu-2004-nsoran -u root bash /tmp/nsoran_gui_up.sh
D=/home/builder/ns-O-RAN-flexric/mmwave-LENA-oran
PAT_T=$(printf 'gui_%s' 'trigger'); PAT_P=$(printf 'sim_data_%s' 'pusher')

# 1) dockerd
pgrep -x dockerd >/dev/null || rm -f /var/run/docker.pid
docker info >/dev/null 2>&1 || (setsid nohup dockerd >/var/log/dockerd.log 2>&1 </dev/null & sleep 12)
docker info >/dev/null 2>&1 || { echo "HATA: dockerd kalkmadi"; exit 1; }
echo "[1/4] dockerd OK"

# 2) uc container (host agi; bridge bu WSL'de calismiyor)
docker start nsoran-influx nsoran-grafana nsoran-gui >/dev/null 2>&1
sleep 10
curl -s -XPOST "http://localhost:8086/query" --data-urlencode "q=CREATE DATABASE influx" >/dev/null 2>&1

# Grafana datasource'u upstream'in docker-compose'undan geliyor ve
# http://influxdb:8086 diyor - bridge agindaki servis adi. Bu WSL'de bridge
# calismadigi icin container'lar HOST aginda kosuyor ve o ad cozulmuyor: Grafana
# sapasaglam gorunurken butun paneller BOS kaliyor. Container yeniden
# yaratilirsa geri gelir, o yuzden her kalkista duzeltiliyor.
DS_ID=$(curl -s --max-time 10 -u admin:admin http://localhost:3001/api/datasources 2>/dev/null \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d[0]['id'] if d else '')" 2>/dev/null)
if [ -n "$DS_ID" ]; then
  DS_BODY=$(curl -s -u admin:admin "http://localhost:3001/api/datasources/$DS_ID" \
    | python3 -c "import sys,json;d=json.load(sys.stdin);d['url']='http://localhost:8086';d['access']='proxy';print(json.dumps(d))" 2>/dev/null)
  [ -n "$DS_BODY" ] && curl -s --max-time 15 -u admin:admin -X PUT \
    "http://localhost:3001/api/datasources/$DS_ID" -H 'Content-Type: application/json' -d "$DS_BODY" >/dev/null 2>&1
  echo "      grafana datasource -> $(curl -s -u admin:admin http://localhost:3001/api/datasources/$DS_ID | python3 -c "import sys,json;print(json.load(sys.stdin)['url'])" 2>/dev/null)"
fi
for n in nsoran-influx nsoran-grafana nsoran-gui; do
  printf '      %-16s %s\n' "$n" "$(docker inspect -f '{{.State.Status}}' $n 2>/dev/null || echo YOK)"
done
echo "[2/4] containerlar OK"

# 3) gui_trigger — MUTLAKA builder olarak (./ns3 root'u reddediyor)
if ! pgrep -f "$PAT_T" >/dev/null; then
  runuser -u builder -- bash -c "cd $D && setsid nohup python3 gui_trigger.py > /tmp/gui_trigger_b.log 2>&1 </dev/null &"
  sleep 8
fi
printf '[3/4] gui_trigger: %s (38866 dinleyen: %s), pusher: %s\n' \
  "$(ps -o user= -p "$(pgrep -f "$PAT_T" | head -1)" 2>/dev/null || echo YOK)" \
  "$(ss -tln 2>/dev/null | grep -c 38866)" "$(pgrep -c -f "$PAT_P" 2>/dev/null || echo 0)"

# 4) simulasyonu GUI'nin kendi ucundan baslat (flags=true sart!)
# Dropdown 11 senaryo listeler ama form TEK bir sabit arguman kumesi gonderir, her
# upstream senaryosu kendi argumanlarini kaydeder. Panodan gercekten KOSAN dortu
# (2026-09-18 olculdu, kaynagi okumak yanlis soyledi):
#   scenario-zero-with_parallel_loging   <- varsayilan, panolari en iyi dolduran
#   Energy_saving_with_cell_utilization_scenario
#   Energy_Saving_with_load_balancing_scenario
#   orange-rf-channel-reconfiguration
# scenario-zero (--N_MmWaveEnbNodes yok) ve scenario-one (--simTime yok) duser.
curl -s -X POST http://localhost:8010/start_simulation -H 'Content-Type: application/json' \
  -d '{"scenario":"scenario-zero-with_parallel_loging","flags":"true","N_Ues":"6",
       "N_MmWaveEnbNodes":"4","N_LteEnbNodes":"1","simTime":"300","indicationPeriodicity":"0.1"}' >/dev/null
echo "[4/4] simulasyon tetiklendi"
echo
echo "  Pano   : http://localhost:8010"
echo "  Grafana: http://localhost:3001  (admin/admin)"
echo "  Veri ~40-60 sn sonra dolar."
echo
echo "  Canli mi? Dolu bir pano eski kosunun verisi de olabilir - zaman damgasina bak:"
echo "    docs/ns3-kanit.md bolum 6"
