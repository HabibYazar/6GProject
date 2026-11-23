# Engine source snapshot — the complete diff against upstream

Taken 2026-09-24 from the build trees that produced `nsg-engine:1.32-rxtrace`,
read-only (`git diff` of each tree; nothing in the trees was changed to take it).

**Why this exists.** `../*.patch` is the history: 39 patches, each with a long
header saying what it changed and why. But they were written one at a time over
two months and were never re-checked as a SET, and two trees have changes no
patch carries at all — the e2sim memory fixes below were only ever in the build
tree. These three diffs are the ground truth: apply them to the upstream commits
listed here and you have exactly the source the shipped engine was built from.
Read the patches to understand a change; apply these to reproduce the tree.

| Diff | Tree | Upstream | Commit | Files |
|---|---|---|---|---|
| `mmwave-LENA-oran.diff` | ns-3.42 mmWave (O-RAN) | https://github.com/MinaYonan123/mmwave-LENA-oran | `0ae720c977ee3dac61e3d2fde7843cb482ba44f4` | 33 |
| `e2sim-kpmv3.diff` | E2 simulator (the RAN's E2 agent side) | https://github.com/MinaYonan123/e2sim-kpmv3 | `acf4f6b2baa8c645af566ea210146abd97de1f48` | 3 |
| `flexric.diff` | FlexRIC near-RT RIC + xApp + emulator agent | https://gitlab.eurecom.fr/mosaic5g/flexric (branch `oie-ric-taap-xapps`) | `307e1d0a5c26751c9e5595805b668a4f91d09550` | 5 |

The first two are submodules of https://github.com/Orange-OpenSource/ns-O-RAN-flexric
at `0b76f11e8f14b6efa44fcbe02f0a7c44011211b5`.

**Not in the diffs, because git does not track them:** the two new FlexRIC files
`examples/emulator/agent/nsg_feed.{c,h}`. They are `../../flexric/nsg_feed.{c,h}`
in this repo (verified byte-identical to the tree by md5). Everything else the
trees hold untracked is backups (`*.bak`, `*.pre_*`, `*.nsg-orig`), run output
(`energyfilecell*.csv`) and alternative build directories — no source.

**What the e2sim diff fixes** (it is small; read it): `calloc(1, 8192)` followed by
`memcpy(message_length)`, and an unclamped `data.len` sent from a 10000-byte
SCTP buffer. Together they crashed dense scenarios (10+ UEs per cell).

## Rebuilding from upstream (only if the dev image is lost)

The normal path is the `nsg-engine-dev:20.04` image, which already holds these
trees configured and built — see `docs/tasarim/08-gelistirme.md`. From scratch,
on **Ubuntu 20.04** (the fork builds most cleanly there) with internet:

```bash
git clone https://github.com/Orange-OpenSource/ns-O-RAN-flexric
cd ns-O-RAN-flexric && git checkout 0b76f11e8f14b6efa44fcbe02f0a7c44011211b5
git submodule update --init
git -C mmwave-LENA-oran checkout 0ae720c977ee3dac61e3d2fde7843cb482ba44f4
git -C e2sim-kpmv3      checkout acf4f6b2baa8c645af566ea210146abd97de1f48
git -C mmwave-LENA-oran apply <repo>/engines/nsoran/tree-snapshot/mmwave-LENA-oran.diff
git -C e2sim-kpmv3      apply <repo>/engines/nsoran/tree-snapshot/e2sim-kpmv3.diff

git clone -b oie-ric-taap-xapps https://gitlab.eurecom.fr/mosaic5g/flexric
git -C flexric checkout 307e1d0a5c26751c9e5595805b668a4f91d09550
git -C flexric apply <repo>/engines/nsoran/tree-snapshot/flexric.diff
cp <repo>/engines/flexric/nsg_feed.[ch] flexric/examples/emulator/agent/
```

Then build in upstream's order (their READMEs): e2sim, then FlexRIC with
`-DE2AP_VERSION=E2AP_V1 -DKPM_VERSION=KPM_V3_00` (needs cmake >= 3.19; 20.04 ships
3.16), then ns-3 as the `builder` user (`./ns3 build`; ns-3 refuses to run as
root). Keep the `default` build profile: `-d optimized` silently drops every
`[NSG-*]` log line the platform parses. The apt packages the dev distro had:
build-essential (gcc 9.4), libsctp-dev, autoconf, automake, libtool, bison, flex,
libboost-all-dev, swig 4.0.1, sqlite3, libsqlite3-dev, libeigen3-dev,
pkg-config, python3.8-dev, python3.8-venv. `/root/nsoran` is `../` (the scripts),
and `build-slim.sh` packages the result.

## Keeping it current

After an engine change, regenerate and commit. Run each as the tree's OWNER:
git answers a different user's repository with empty output (its "dubious
ownership" guard), which reads exactly like "no changes".

```bash
# ns-3 and e2sim belong to builder
docker exec -u builder nsg-engine-dev git -C /home/builder/ns-O-RAN-flexric/mmwave-LENA-oran diff > engines/nsoran/tree-snapshot/mmwave-LENA-oran.diff
docker exec -u builder nsg-engine-dev git -C /home/builder/ns-O-RAN-flexric/e2sim-kpmv3 diff > engines/nsoran/tree-snapshot/e2sim-kpmv3.diff
# FlexRIC belongs to root
docker exec nsg-engine-dev git -C /root/nsoran/flexric diff > engines/nsoran/tree-snapshot/flexric.diff
```
