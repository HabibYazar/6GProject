#!/usr/bin/env python3
"""Wire Tier B's feed into FlexRIC's emulator agent, in place.

Edits rather than a .patch file: the upstream tree moves and a context diff
that fails to apply halfway leaves a broken agent, where an edit that cannot
find its anchor refuses and changes nothing. Every change is guarded so the
script is safe to run twice.

What it does, all inside examples/emulator/agent:
  * copies nsg_feed.c/h in and adds the .c to the object library
  * makes emulate_ues_fullfilling_pred build one UE per feed IMSI
  * makes the KPM fillers read the feed for that UE

With no feed present every one of those falls through to the stock random
data, so the upstream example still runs untouched.
"""
import os
import shutil
import sys

AGENT = sys.argv[1] if len(sys.argv) > 1 else "/root/nsoran/flexric/examples/emulator/agent"
HERE = os.path.dirname(os.path.abspath(__file__))


def edit(path, old, new, what, marker):
    """`marker` is a string that exists ONLY after this edit.

    The first version checked the first line of `new`, which for an edit that
    inserts after an existing line is the same as the first line of `old` —
    so it reported two edits as already applied on a clean tree and left the
    file half-patched and unbuildable.
    """
    s = open(path).read()
    if marker in s:
        print(f"  = {what} (already applied)")
        return s
    if old not in s:
        print(f"  ! {what}: anchor not found — NOTHING CHANGED")
        raise SystemExit(2)
    open(path, "w").write(s.replace(old, new, 1))
    print(f"  + {what}")


for f in ("nsg_feed.c", "nsg_feed.h"):
    shutil.copy(os.path.join(HERE, f), os.path.join(AGENT, f))
print(f"  + copied nsg_feed.c/h into {AGENT}")

cm = os.path.join(AGENT, "CMakeLists.txt")
if "nsg_feed.c" not in open(cm).read():
    edit(cm, "  sm_gtp.c", "  nsg_feed.c\n  sm_gtp.c", "CMakeLists builds nsg_feed.c",
         marker="nsg_feed.c")
else:
    print("  = CMakeLists (already applied)")

km = os.path.join(AGENT, "sm_kpm.c")
edit(km, '#include "sm_kpm.h"', '#include "sm_kpm.h"\n#include "nsg_feed.h"',
     "sm_kpm.c includes nsg_feed.h", marker='#include "nsg_feed.h"')

# The UE list. With a feed, the agent's UEs ARE the feed's UEs — sm_kpm.c
# otherwise gives every one of them amf_ue_ngap_id 112358132134, so per-UE
# values would all land on the same identity.
edit(km, '''  assert(info != NULL);
  size_t const num_ues = 16;

  seq_arr_t dst = {0};
  seq_init(&dst, sizeof(ue_id_e2sm_t)); // 

  for(size_t i = 0; i < num_ues; ++i){''',
'''  assert(info != NULL);
  size_t const num_ues = 16;

  seq_arr_t dst = {0};
  seq_init(&dst, sizeof(ue_id_e2sm_t)); // 

  // NSG: when the fast tier is publishing, its UEs are the agent's UEs. The
  // stock path below gives every UE the same amf_ue_ngap_id, so per-UE
  // measurements would all be reported against one identity.
  int const nsg_ues = nsg_feed_ue_count();
  if (nsg_ues > 0) {
    for (int i = 0; i < nsg_ues; ++i) {
      ue_id_e2sm_t ue = fill_rnd_ue_id_data();   // keeps every mandatory field
      ue.gnb.amf_ue_ngap_id = nsg_feed_ue_at(i); // ...and carries a real id
      seq_push_back(&dst, &ue, sizeof(ue_id_e2sm_t));
    }
    return dst;
  }

  for(size_t i = 0; i < num_ues; ++i){''',
     "the UE list comes from the feed", marker="int const nsg_ues")

# The fillers. One helper, then each filler asks for its own key first.
edit(km, '''static
meas_record_lst_t fill_rnd_int_data(void)''',
'''// NSG: the published value for this UE, or the stock random one. A filler
// that invented a number when the feed was silent would be indistinguishable
// from one that read it.
static
bool nsg_int(const char* key, ue_id_e2sm_t const* ue, meas_record_lst_t* dst)
{
  double v = 0.0;
  if (ue == NULL || !nsg_feed_get(key, ue->gnb.amf_ue_ngap_id, &v))
    return false;
  dst->value = INTEGER_MEAS_VALUE;
  dst->int_val = (int64_t)(v + 0.5);
  return true;
}

static
bool nsg_real(const char* key, ue_id_e2sm_t const* ue, meas_record_lst_t* dst)
{
  double v = 0.0;
  if (ue == NULL || !nsg_feed_get(key, ue->gnb.amf_ue_ngap_id, &v))
    return false;
  dst->value = REAL_MEAS_VALUE;
  dst->real_val = v;
  return true;
}

// NSG: once the fast tier is publishing, a counter it does NOT produce must
// not come back as rand(). E2SM-KPM has NO_VALUE_MEAS_VALUE for exactly this
// and it is the honest answer: an xApp reading a random uplink figure beside
// a real downlink one has no way to tell them apart, and nothing in the
// report would say so. With no feed at all the stock behaviour is kept, so
// the upstream example still runs.
static
bool nsg_feed_live(void)
{
  return nsg_feed_ue_count() > 0;
}

static
meas_record_lst_t nsg_no_value(void)
{
  meas_record_lst_t dst = {0};
  dst.value = NO_VALUE_MEAS_VALUE;
  return dst;
}

static
meas_record_lst_t fill_rnd_int_data(void)''',
     "feed helpers", marker="bool nsg_int(")

# key=None means Tier B has no such measurement: report no value rather than
# a plausible one.
for key, fn, kind in (("DRB.PdcpSduVolumeDL", "fill_DRB_PdcpSduVolumeDL", "int"),
                      ("DRB.UEThpDl", "fill_DRB_UEThpDl", "real"),
                      ("RRU.PrbTotDl", "fill_RRU_PrbTotDl", "int"),
                      (None, "fill_DRB_PdcpSduVolumeUL", "int"),
                      (None, "fill_DRB_RlcSduDelayDl", "real"),
                      (None, "fill_DRB_UEThpUl", "real"),
                      (None, "fill_RRU_PrbTotUl", "int")):
    fallback = "fill_rnd_int_data()" if kind == "int" else "fill_rnd_float_data()"
    s = open(km).read()
    at = s.find(f'meas_record_lst_t {fn}(')
    # Idempotent on the FILLER, not on the file: the fallback this inserts is
    # another `return fill_rnd_int_data();`, so a second pass matched its own
    # output and produced two `dst` declarations in one function. Look for the
    # key inside this filler's body instead.
    nxt = s.find("\nstatic", at + 1)
    fn_body = s[at:nxt if nxt > 0 else len(s)]
    if "nsg_feed_live()" in fn_body:
        print(f"  = {fn} (already applied)")
        continue
    if at < 0:
        print(f"  ! {fn}: not found — NOTHING CHANGED")
        raise SystemExit(2)
    ret = s.find(f"return {fallback};", at)
    if ret < 0:
        print(f"  = {fn} (already applied)")
        continue
    if key is None:
        new = (f'if (nsg_feed_live())\n'
               f'    return nsg_no_value();\n'
               f'  return {fallback};')
    else:
        new = (f'meas_record_lst_t dst = {{0}};\n'
               f'  if (nsg_{kind}("{key}", ue, &dst))\n'
               f'    return dst;\n'
               f'  if (nsg_feed_live())\n'
               f'    return nsg_no_value();\n'
               f'  return {fallback};')
    open(km, "w").write(s[:ret] + new + s[ret + len(f"return {fallback};"):])
    print(f"  + {fn} " + (f"reads {key}" if key else "reports no value when fed"))

# Counters the fast tier measures and the stock emulator never declared. Two
# places have to agree: the fillers table, which maps a name to a function,
# and the advertised set in fill_kpm_report_style_4, which is what an xApp can
# subscribe to at all. Adding one without the other gives either a name nobody
# can ask for or a subscription that arrives with nothing behind it.
NSG_EXTRA = (("TB.TotNbrDl.1", "fill_nsg_TB_TotNbrDl_1"),
             ("TB.ErrTotalNbrDl.1", "fill_nsg_TB_ErrTotalNbrDl_1"),
             ("DRB.BufferSize.Qos", "fill_nsg_DRB_BufferSize_Qos"))

s = open(km).read()
if "fill_nsg_TB_TotNbrDl_1" in s:
    print("  = extra counters (already applied)")
else:
    fns = ""
    for key, fn in NSG_EXTRA:
        fns += (
            "\nstatic\n"
            "meas_record_lst_t " + fn + "(ue_id_e2sm_t const* ue)\n"
            "{\n"
            "  meas_record_lst_t dst = {0};\n"
            '  if (nsg_int("' + key + '", ue, &dst))\n'
            "    return dst;\n"
            "  return nsg_no_value();\n"
            "}\n")
    anchor = "static\nassoc_ht_open_t ht;"
    if anchor not in s:
        print("  ! extra counters: function anchor not found — NOTHING CHANGED")
        raise SystemExit(2)
    s = s.replace(anchor, fns + "\n" + anchor, 1)

    tbl = '  (kv_measure_t){.key = "RRU.PrbTotUl", .value =  fill_RRU_PrbTotUl }, \n'
    if tbl not in s:
        print("  ! extra counters: table row not found — NOTHING CHANGED")
        raise SystemExit(2)
    rows = "".join('  (kv_measure_t){.key = "' + k + '", .value = ' + f + ' }, \n'
                   for k, f in NSG_EXTRA)
    s = s.replace(tbl, tbl + rows, 1)

    adv = '    "RRU.PrbTotUl",\n  };'
    if adv not in s:
        print("  ! extra counters: advertised set not found — NOTHING CHANGED")
        raise SystemExit(2)
    s = s.replace(adv, '    "RRU.PrbTotUl",\n'
                  + "".join('    "' + k + '",\n' for k, _ in NSG_EXTRA) + "  };", 1)
    open(km, "w").write(s)
    print("  + " + str(len(NSG_EXTRA)) + " extra counters, declared and filled")

# THE OTHER DIRECTION. The RC handler already parsed the control and answered
# it; it had nowhere to put it, so an xApp could read the fast tier and not
# steer it. One line per control, appended for the Python side to drain.
rc = os.path.join(AGENT, "sm_rc.c")
s = open(rc).read()
if "nsg_ctrl_append" in s:
    print("  = sm_rc.c control channel (already applied)")
else:
    if '#include "sm_rc.h"' not in s:
        print("  ! sm_rc.c: include anchor not found — NOTHING CHANGED")
        raise SystemExit(2)
    s = s.replace('#include "sm_rc.h"',
                  '#include "sm_rc.h"\n#include "nsg_feed.h"', 1)
    anchor = 'printf("qfi = %ld dir %ld \\n", qfi, dir);'
    if anchor not in s:
        print("  ! sm_rc.c: qfi anchor not found — NOTHING CHANGED")
        raise SystemExit(2)
    s = s.replace(anchor, anchor + '''
            {
              // NSG: hand the control to the fast tier. The stock message
              // says WHICH flow and WHICH direction and carries no
              // magnitude, so what the tier does with it is defined on the
              // Python side and documented there — this end only delivers.
              char nsg_line[128];
              snprintf(nsg_line, sizeof(nsg_line), "qos_flow %ld %ld",
                       (long)qfi, (long)dir);
              if (!nsg_ctrl_append(nsg_line))
                printf("NSG: control NOT delivered to the fast tier\\n");
            }''', 1)
    open(rc, "w").write(s)
    print("  + sm_rc.c hands RC controls to the fast tier")

print("\napplied. Build with:  cmake --build <build> --target emu_agent_gnb")
