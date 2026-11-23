/*
 * Licensed to the OpenAirInterface (OAI) Software Alliance under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The OpenAirInterface Software Alliance licenses this file to You under
 * the OAI Public License, Version 1.1  (the "License"); you may not use this file
 * except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.openairinterface.org/?page_id=698
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *-------------------------------------------------------------------------------
 * For more information about the OpenAirInterface (OAI) Software Alliance:
 *      contact@openairinterface.org
 */

#include "../../../../src/xApp/e42_xapp_api.h"
#include "../../../../src/sm/rc_sm/ie/ir/ran_param_struct.h"
#include "../../../../src/sm/rc_sm/ie/ir/ran_param_list.h"
#include "../../../../src/util/time_now_us.h"
#include "../../../../src/util/alg_ds/ds/lock_guard/lock_guard.h"
#include "../../../../src/util/e.h"
#include <stdlib.h>
#include <stdio.h>
#include <time.h>
#include <unistd.h>
#include <fcntl.h>     // NSG bridge: FIFO open/read for live external control
#include <string.h>    // NSG bridge: parse control commands (strstr/strchr)
#include <pthread.h>

static
ue_id_e2sm_t ue_id;

static
uint64_t const period_ms = 100;

static
pthread_mutex_t mtx;

static
void log_gnb_ue_id(ue_id_e2sm_t ue_id)
{
  if (ue_id.gnb.gnb_cu_ue_f1ap_lst != NULL) {
    for (size_t i = 0; i < ue_id.gnb.gnb_cu_ue_f1ap_lst_len; i++) {
      printf("UE ID type = gNB-CU, gnb_cu_ue_f1ap = %u\n", ue_id.gnb.gnb_cu_ue_f1ap_lst[i]);
    }
  } else {
    printf("UE ID type = gNB, amf_ue_ngap_id = %lu\n", ue_id.gnb.amf_ue_ngap_id);
  }
  if (ue_id.gnb.ran_ue_id != NULL) {
    printf("ran_ue_id = %lx\n", *ue_id.gnb.ran_ue_id); // RAN UE NGAP ID
  }
}

static
void log_du_ue_id(ue_id_e2sm_t ue_id)
{
  printf("UE ID type = gNB-DU, gnb_cu_ue_f1ap = %u\n", ue_id.gnb_du.gnb_cu_ue_f1ap);
  if (ue_id.gnb_du.ran_ue_id != NULL) {
    printf("ran_ue_id = %lx\n", *ue_id.gnb_du.ran_ue_id); // RAN UE NGAP ID
  }
}

static
void log_cuup_ue_id(ue_id_e2sm_t ue_id)
{
  printf("UE ID type = gNB-CU-UP, gnb_cu_cp_ue_e1ap = %u\n", ue_id.gnb_cu_up.gnb_cu_cp_ue_e1ap);
  if (ue_id.gnb_cu_up.ran_ue_id != NULL) {
    printf("ran_ue_id = %lx\n", *ue_id.gnb_cu_up.ran_ue_id); // RAN UE NGAP ID
  }
}

typedef void (*log_ue_id)(ue_id_e2sm_t ue_id);

static
log_ue_id log_ue_id_e2sm[END_UE_ID_E2SM] = {
    log_gnb_ue_id, // common for gNB-mono, CU and CU-CP
    log_du_ue_id,
    log_cuup_ue_id,
    NULL,
    NULL,
    NULL,
    NULL,
};

static
void log_int_value(byte_array_t name, meas_record_lst_t meas_record)
{
  if (cmp_str_ba("RRU.PrbTotDl", name) == 0) {
    printf("RRU.PrbTotDl = %d [PRBs]\n", meas_record.int_val);
  } else if (cmp_str_ba("RRU.PrbTotUl", name) == 0) {
    printf("RRU.PrbTotUl = %d [PRBs]\n", meas_record.int_val);
  } else if (cmp_str_ba("DRB.PdcpSduVolumeDL", name) == 0) {
    printf("DRB.PdcpSduVolumeDL = %d [kb]\n", meas_record.int_val);
  } else if (cmp_str_ba("DRB.PdcpSduVolumeUL", name) == 0) {
    printf("DRB.PdcpSduVolumeUL = %d [kb]\n", meas_record.int_val);
  } else {
    printf("%.*s = %d\n", (int)name.len, (char*)name.buf, meas_record.int_val);
  }
}

static
void log_real_value(byte_array_t name, meas_record_lst_t meas_record)
{
  if (cmp_str_ba("DRB.RlcSduDelayDl", name) == 0) {
    printf("DRB.RlcSduDelayDl = %.2f [μs]\n", meas_record.real_val);
  } else if (cmp_str_ba("DRB.UEThpDl", name) == 0) {
    printf("DRB.UEThpDl = %.2f [kbps]\n", meas_record.real_val);
  } else if (cmp_str_ba("DRB.UEThpUl", name) == 0) {
    printf("DRB.UEThpUl = %.2f [kbps]\n", meas_record.real_val);
  } else {
    printf("%.*s = %.2f\n", (int)name.len, (char*)name.buf, meas_record.real_val);
  }
}

typedef void (*log_meas_value)(byte_array_t name, meas_record_lst_t meas_record);

static
log_meas_value get_meas_value[END_MEAS_VALUE] = {
    log_int_value,
    log_real_value,
    NULL,
};

static
void match_meas_name_type(meas_type_t meas_type, meas_record_lst_t meas_record)
{
  // NSG fix: idle cells (0 connected UEs) emit a record with an EMPTY measurement
  // name; cmp_str_ba() asserts ba.len > 0, which core-dumps the whole xApp before
  // the DU throughput/PRB/SINR reports are ever decoded. Skip empty names.
  if (meas_type.name.len == 0 || meas_type.name.buf == NULL)
    return;
  // NSG fix (scale): get_meas_value[] only has handlers for INT (0) and REAL (1);
  // the 3rd slot (NO_VALUE / byte-array) is NULL. At 6+ gNB / many UEs some records
  // carry that value-type, and calling the NULL slot segfaults the whole xApp mid-
  // collection (this is what capped scenarios at "3 gNB"). Skip unhandled types.
  if (meas_record.value >= END_MEAS_VALUE)
    return;
  log_meas_value const handler = get_meas_value[meas_record.value];
  if (handler == NULL)
    return;
  // Get the value of the Measurement
  handler(meas_type.name, meas_record);
}

static
void match_id_meas_type(meas_type_t meas_type, meas_record_lst_t meas_record)
{
  (void)meas_type;
  (void)meas_record;
  assert(false && "ID Measurement Type not yet supported");
}

typedef void (*check_meas_type)(meas_type_t meas_type, meas_record_lst_t meas_record);

static
check_meas_type match_meas_type[END_MEAS_TYPE] = {
    match_meas_name_type,
    match_id_meas_type,
};

static
void log_kpm_measurements(kpm_ind_msg_format_1_t const* msg_frm_1)
{
  assert(msg_frm_1->meas_info_lst_len > 0 && "Cannot correctly print measurements");

  // UE Measurements per granularity period
  for (size_t j = 0; j < msg_frm_1->meas_data_lst_len; j++) {
    meas_data_lst_t const data_item = msg_frm_1->meas_data_lst[j];

    for (size_t z = 0; z < data_item.meas_record_len; z++) {
      // ns-O-RAN encodes each measurement as its own data item (record_len==1)
      // instead of one data item with record_len==info_len. So when there are as
      // many data items as measurement names, the name for data_item[j] is
      // meas_info_lst[j] — using meas_info_lst[z] mislabels everything as item 0.
      size_t const name_idx =
          (data_item.meas_record_len == 1 &&
           msg_frm_1->meas_data_lst_len == msg_frm_1->meas_info_lst_len)
              ? j
              : z;
      // NSG fix (scale): the DU-level report at 6+ gNB breaks the "one record per
      // data item" assumption, so the fallback name_idx=z overruns meas_info_lst ->
      // a garbage meas_type -> match_meas_type[garbage] jumps to a bogus pointer
      // (SIGSEGV, PC=0x1). Skip any record we cannot safely label/dispatch.
      if (name_idx >= msg_frm_1->meas_info_lst_len)
        continue;
      meas_type_t const meas_type = msg_frm_1->meas_info_lst[name_idx].meas_type;
      meas_record_lst_t const record_item = data_item.meas_record_lst[z];

      if (meas_type.type >= END_MEAS_TYPE)
        continue;
      match_meas_type[meas_type.type](meas_type, record_item);

      if (data_item.incomplete_flag && *data_item.incomplete_flag == TRUE_ENUM_VALUE)
        printf("Measurement Record not reliable");
    }
  }

}

static
void sm_cb_kpm(sm_ag_if_rd_t const* rd)
{
  assert(rd != NULL);
  assert(rd->type == INDICATION_MSG_AGENT_IF_ANS_V0);
  assert(rd->ind.type == KPM_STATS_V3_0);

  // Reading Indication Message Format 3
  kpm_ind_data_t const* ind = &rd->ind.kpm.ind;
  kpm_ric_ind_hdr_format_1_t const* hdr_frm_1 = &ind->hdr.kpm_ric_ind_hdr_format_1;
  kpm_ind_msg_format_3_t const* msg_frm_3 = &ind->msg.frm_3;

  int64_t const now = time_now_us();
  static int counter = 1;
  {
    lock_guard(&mtx);
    printf("\n%7d KPM ind_msg latency = %ld [μs]\n", counter, now - hdr_frm_1->collectStartTime); // xApp <-> E2 Node


    // Reported list of measurements per UE
    for (size_t i = 0; i < msg_frm_3->ue_meas_report_lst_len; i++) {
      // log UE ID
      ue_id_e2sm_t const ue_id_e2sm = msg_frm_3->meas_report_per_ue[i].ue_meas_report_lst;
      ue_id_e2sm_e const type = ue_id_e2sm.type;
      log_ue_id_e2sm[type](ue_id_e2sm);
      // Save UE ID for filling RC Control message
      free_ue_id_e2sm(&ue_id);
      ue_id = cp_ue_id_e2sm(&ue_id_e2sm);

      // log measurements
      log_kpm_measurements(&msg_frm_3->meas_report_per_ue[i].ind_msg_format_1);
      
    }
    counter++;
  }
  fflush(stdout);  // NSG bridge: flush each indication so the backend tails KPM live
}

typedef enum {
  DRB_QoS_Configuration_7_6_2_1 = 1,
  QoS_flow_mapping_configuration_7_6_2_1 = 2,
  Logical_channel_configuration_7_6_2_1 = 3,
  Radio_admission_control_7_6_2_1 = 4,
  DRB_termination_control_7_6_2_1 = 5,
  DRB_split_ratio_control_7_6_2_1 = 6,
  PDCP_Duplication_control_7_6_2_1 = 7,
} rc_ctrl_service_style_1_e;

typedef enum {
  DRB_ID_8_4_2_2 = 1,
  LIST_OF_QOS_FLOWS_MOD_IN_DRB_8_4_2_2 = 2,
  QOS_FLOW_ITEM_8_4_2_2 = 3,
  QOS_FLOW_ID_8_4_2_2 = 4,
  QOS_FLOW_MAPPING_IND_8_4_2_2 = 5,
} qos_flow_mapping_conf_e;

static
seq_ran_param_t fill_drb_id_param(void)
{
  seq_ran_param_t drb_param = {0};

  drb_param.ran_param_id = DRB_ID_8_4_2_2;
  drb_param.ran_param_val.type = ELEMENT_KEY_FLAG_TRUE_RAN_PARAMETER_VAL_TYPE;
  drb_param.ran_param_val.flag_true = calloc(1, sizeof(ran_parameter_value_t));
  assert(drb_param.ran_param_val.flag_true != NULL && "Memory exhausted");
  // Let's suppose that it is the DRB 5
  drb_param.ran_param_val.flag_true->type = INTEGER_RAN_PARAMETER_VALUE;
  drb_param.ran_param_val.flag_true->int_ran = 5;

  return drb_param;
}

static
seq_ran_param_t fill_qos_flows_param(void)
{
  seq_ran_param_t qos_param = {0};

  qos_param.ran_param_id = LIST_OF_QOS_FLOWS_MOD_IN_DRB_8_4_2_2;
  qos_param.ran_param_val.type = LIST_RAN_PARAMETER_VAL_TYPE;
  qos_param.ran_param_val.lst = calloc(1, sizeof(ran_param_list_t));
  assert(qos_param.ran_param_val.lst != NULL && "Memory exhausted");
  ran_param_list_t* rpl = qos_param.ran_param_val.lst;

  rpl->sz_lst_ran_param = 1;
  rpl->lst_ran_param = calloc(1, sizeof(lst_ran_param_t));
  assert(rpl->lst_ran_param != NULL && "Memory exhausted");

  // QoS Flow Item
  // Bug in the standard. RAN Parameter List 9.3.13
  // has a mandatory ie RAN Parameter ID 9.3.8
  // and a mandatory ie RAN Parameter Structure 9.3.12
  // However, the ASN
  // RANParameter-LIST ::= SEQUENCE {
  // list-of-ranParameter  SEQUENCE (SIZE(1..maxnoofItemsinList)) OF RANParameter-STRUCTURE,
  // ..
  // }
  //
  // Misses RAN Parameter ID and only has RAN Parameter Structure

  // rpl->lst_ran_param[0].ran_param_id = QOS_FLOW_ITEM_8_4_2_2;

  rpl->lst_ran_param[0].ran_param_struct.sz_ran_param_struct = 2;
  rpl->lst_ran_param[0].ran_param_struct.ran_param_struct = calloc(2, sizeof(seq_ran_param_t));
  assert(rpl->lst_ran_param[0].ran_param_struct.ran_param_struct != NULL && "Memory exhausted");
  seq_ran_param_t* rps = rpl->lst_ran_param[0].ran_param_struct.ran_param_struct;

  // QoS Flow Identifier
  rps[0].ran_param_id = QOS_FLOW_ID_8_4_2_2;
  rps[0].ran_param_val.type = ELEMENT_KEY_FLAG_TRUE_RAN_PARAMETER_VAL_TYPE;
  rps[0].ran_param_val.flag_true = calloc(1, sizeof(ran_parameter_value_t));
  assert(rps[0].ran_param_val.flag_true != NULL && "Memory exhausted");
  rps[0].ran_param_val.flag_true->type = INTEGER_RAN_PARAMETER_VALUE;
  // Let's suppose that we have QFI 10
  rps[0].ran_param_val.flag_true->int_ran = 10;

  // QoS Flow Mapping Indication
  rps[1].ran_param_id = QOS_FLOW_MAPPING_IND_8_4_2_2;
  rps[1].ran_param_val.type = ELEMENT_KEY_FLAG_FALSE_RAN_PARAMETER_VAL_TYPE;
  rps[1].ran_param_val.flag_false = calloc(1, sizeof(ran_parameter_value_t));
  assert(rps[1].ran_param_val.flag_false != NULL && "Memory exhausted");

  // ENUMERATED (ul, dl, ...)
  rps[1].ran_param_val.flag_false->type = INTEGER_RAN_PARAMETER_VALUE;
  rps[1].ran_param_val.flag_false->int_ran = 1;

  return qos_param;
}

static
void fill_rc_ctrl_act(seq_ctrl_act_2_t const* ctrl_act,
                             size_t const sz,
                             e2sm_rc_ctrl_hdr_frmt_1_t* hdr,
                             e2sm_rc_ctrl_msg_frmt_1_t* msg)
{
  assert(ctrl_act != NULL);

  for (size_t i = 0; i < sz; i++) {
    assert(cmp_str_ba("QoS flow mapping configuration", ctrl_act[i].name) == 0 && "Add requested CONTROL Action. At the moment, only QoS flow mapping configuration supported");

    hdr->ctrl_act_id = QoS_flow_mapping_configuration_7_6_2_1;

    msg->sz_ran_param = ctrl_act[i].sz_seq_assoc_ran_param;
    assert(msg->sz_ran_param == 2);
    msg->ran_param = calloc(msg->sz_ran_param, sizeof(seq_ran_param_t));
    assert(msg->ran_param != NULL && "Memory exhausted");

    /* Fill RAN Parameters in Control Message */

    // DRB ID
    assert(ctrl_act[i].assoc_ran_param[0].id == DRB_ID_8_4_2_2);
    msg->ran_param[0] = fill_drb_id_param();

    // List of QoS Flows to be modified in DRB
    assert(ctrl_act[i].assoc_ran_param[1].id == LIST_OF_QOS_FLOWS_MOD_IN_DRB_8_4_2_2);
    msg->ran_param[1] = fill_qos_flows_param();
  }
}

static
rc_ctrl_req_data_t gen_rc_ctrl_msg(ran_func_def_ctrl_t const* ran_func)
{
  assert(ran_func != NULL);

  rc_ctrl_req_data_t rc_ctrl = {0};

  for (size_t i = 0; i < ran_func->sz_seq_ctrl_style; i++) {
    assert(cmp_str_ba("Radio Bearer Control", ran_func->seq_ctrl_style[i].name) == 0 && "Add requested CONTROL Style. At the moment, only Radio Bearer Control supported");

    // CONTROL HEADER
    rc_ctrl.hdr.format = ran_func->seq_ctrl_style[i].hdr;
    assert(rc_ctrl.hdr.format == FORMAT_1_E2SM_RC_CTRL_HDR && "Indication Header Format received not valid");
    rc_ctrl.hdr.frmt_1.ric_style_type = 1;
    // 6.2.2.6
    {
      lock_guard(&mtx);
      rc_ctrl.hdr.frmt_1.ue_id = cp_ue_id_e2sm(&ue_id);
    }

    // CONTROL MESSAGE
    rc_ctrl.msg.format = ran_func->seq_ctrl_style[i].msg;
    assert(rc_ctrl.msg.format == FORMAT_1_E2SM_RC_CTRL_MSG && "Indication Message Format received not valid");

    fill_rc_ctrl_act(ran_func->seq_ctrl_style[i].seq_ctrl_act,
                     ran_func->seq_ctrl_style[i].sz_seq_ctrl_act,
                     &rc_ctrl.hdr.frmt_1,
                     &rc_ctrl.msg.frmt_1);
  }

  return rc_ctrl;
}

static
test_info_lst_t filter_predicate(test_cond_type_e type, test_cond_e cond, int value)
{
  test_info_lst_t dst = {0};

  dst.test_cond_type = type;
  // It can only be TRUE_TEST_COND_TYPE so it does not matter the type
  // but ugly ugly...
  dst.S_NSSAI = TRUE_TEST_COND_TYPE;

  dst.test_cond = calloc(1, sizeof(test_cond_e));
  assert(dst.test_cond != NULL && "Memory exhausted");
  *dst.test_cond = cond;

  dst.test_cond_value = calloc(1, sizeof(test_cond_value_t));
  assert(dst.test_cond_value != NULL && "Memory exhausted");
  dst.test_cond_value->type = OCTET_STRING_TEST_COND_VALUE;

  dst.test_cond_value->octet_string_value = calloc(1, sizeof(byte_array_t));
  assert(dst.test_cond_value->octet_string_value != NULL && "Memory exhausted");
  const size_t len_nssai = 1;
  dst.test_cond_value->octet_string_value->len = len_nssai;
  dst.test_cond_value->octet_string_value->buf = calloc(len_nssai, sizeof(uint8_t));
  assert(dst.test_cond_value->octet_string_value->buf != NULL && "Memory exhausted");
  dst.test_cond_value->octet_string_value->buf[0] = value;

  return dst;
}

static
label_info_lst_t fill_kpm_label(void)
{
  label_info_lst_t label_item = {0};

  label_item.noLabel = ecalloc(1, sizeof(enum_value_e));
  *label_item.noLabel = TRUE_ENUM_VALUE;

  return label_item;
}

static
kpm_act_def_format_1_t fill_act_def_frm_1(ric_report_style_item_t const* report_item)
{
  assert(report_item != NULL);

  kpm_act_def_format_1_t ad_frm_1 = {0};

  size_t const sz = report_item->meas_info_for_action_lst_len;

  // [1, 65535]
  ad_frm_1.meas_info_lst_len = sz;
  ad_frm_1.meas_info_lst = calloc(sz, sizeof(meas_info_format_1_lst_t));
  assert(ad_frm_1.meas_info_lst != NULL && "Memory exhausted");

  for (size_t i = 0; i < sz; i++) {
    meas_info_format_1_lst_t* meas_item = &ad_frm_1.meas_info_lst[i];
    // 8.3.9
    // Measurement Name
    meas_item->meas_type.type = NAME_MEAS_TYPE;
    meas_item->meas_type.name = copy_byte_array(report_item->meas_info_for_action_lst[i].name);

    // [1, 2147483647]
    // 8.3.11
    meas_item->label_info_lst_len = 1;
    meas_item->label_info_lst = ecalloc(1, sizeof(label_info_lst_t));
    meas_item->label_info_lst[0] = fill_kpm_label();
  }

  // 8.3.8 [0, 4294967295]
  ad_frm_1.gran_period_ms = period_ms;

  // 8.3.20 - OPTIONAL
  ad_frm_1.cell_global_id = NULL;

#if defined KPM_V2_03 || defined KPM_V3_00
  // [0, 65535]
  ad_frm_1.meas_bin_range_info_lst_len = 0;
  ad_frm_1.meas_bin_info_lst = NULL;
#endif

  return ad_frm_1;
}

static
kpm_act_def_t fill_report_style_4(ric_report_style_item_t const* report_item)
{
  assert(report_item != NULL);
  assert(report_item->act_def_format_type == FORMAT_4_ACTION_DEFINITION);

  kpm_act_def_t act_def = {.type = FORMAT_4_ACTION_DEFINITION};

  // Fill matching condition
  // [1, 32768]
  act_def.frm_4.matching_cond_lst_len = 1;
  act_def.frm_4.matching_cond_lst = calloc(act_def.frm_4.matching_cond_lst_len, sizeof(matching_condition_format_4_lst_t));
  assert(act_def.frm_4.matching_cond_lst != NULL && "Memory exhausted");
  // Filter connected UEs by S-NSSAI criteria
  test_cond_type_e const type = S_NSSAI_TEST_COND_TYPE; // CQI_TEST_COND_TYPE
  // mmWave CheckReportingFlag() gates DU reporting on PRB-avg vs this threshold;
  // EQUAL 1 was ~never satisfied, so the DU report never fired. Report whenever
  // there is any PRB usage (avg > 0).
  test_cond_e const condition = GREATERTHAN_TEST_COND;
  int const value = 0;
  act_def.frm_4.matching_cond_lst[0].test_info_lst = filter_predicate(type, condition, value);

  // Fill Action Definition Format 1
  // 8.2.1.2.1
  act_def.frm_4.action_def_format_1 = fill_act_def_frm_1(report_item);

  return act_def;
}

typedef kpm_act_def_t (*fill_kpm_act_def)(ric_report_style_item_t const* report_item);

static
fill_kpm_act_def get_kpm_act_def[END_RIC_SERVICE_REPORT] = {
    NULL,
    NULL,
    NULL,
    fill_report_style_4,
    NULL,
};

static
kpm_sub_data_t gen_kpm_subs(kpm_ran_function_def_t const* ran_func)
{
  assert(ran_func != NULL);
  assert(ran_func->ric_event_trigger_style_list != NULL);

  kpm_sub_data_t kpm_sub = {0};

  // Generate Event Trigger
  assert(ran_func->ric_event_trigger_style_list[0].format_type == FORMAT_1_RIC_EVENT_TRIGGER);
  kpm_sub.ev_trg_def.type = FORMAT_1_RIC_EVENT_TRIGGER;
  kpm_sub.ev_trg_def.kpm_ric_event_trigger_format_1.report_period_ms = period_ms;

  // Generate Action Definition
  kpm_sub.sz_ad = 1;
  kpm_sub.ad = calloc(kpm_sub.sz_ad, sizeof(kpm_act_def_t));
  assert(kpm_sub.ad != NULL && "Memory exhausted");

  // Multiple Action Definitions in one SUBSCRIPTION message is not supported in this project
  // Multiple REPORT Styles = Multiple Action Definition = Multiple SUBSCRIPTION messages
  ric_report_style_item_t* const report_item = &ran_func->ric_report_style_list[0];
  ric_service_report_e const report_style_type = report_item->report_style_type;
  *kpm_sub.ad = get_kpm_act_def[report_style_type](report_item);

  return kpm_sub;
}

static
bool eq_sm(sm_ran_function_t const* elem, int const id)
{
  if (elem->id == id)
    return true;

  return false;
}

static
size_t find_sm_idx(sm_ran_function_t* rf, size_t sz, bool (*f)(sm_ran_function_t const*, int const), int const id)
{
  for (size_t i = 0; i < sz; i++) {
    if (f(&rf[i], id))
      return i;
  }

  assert(0 != 0 && "SM ID could not be found in the RAN Function List");
}

// ─── NSG bridge: send one RC CONTROL (QoS flow mapping) to every RC-capable node.
// Extracted from the original one-shot block so it can be fired LIVE, on demand,
// by an external controller (the customer's own xApp) relayed through the backend.
static void nsg_send_rc_control(e2_node_arr_xapp_t* nodes)
{
  int const RC_ran_function = 3;
  for (size_t i = 0; i < nodes->len; ++i) {
    e2_node_connected_xapp_t* n = &nodes->n[i];
    size_t const idx = find_sm_idx(n->rf, n->len_rf, eq_sm, RC_ran_function);
    if (n->rf[idx].defn.type != RC_RAN_FUNC_DEF_E) continue;
    // if CONTROL Service is supported by E2 node, send CONTROL message
    if (n->rf[idx].defn.rc.ctrl != NULL) {
      rc_ctrl_req_data_t rc_ctrl = gen_rc_ctrl_msg(n->rf[idx].defn.rc.ctrl);
      control_sm_xapp_api(&n->id, RC_ran_function, &rc_ctrl);
      free_rc_ctrl_req_data(&rc_ctrl);
    }
  }
}

// ─── NSG Phase-3: live cell on/off via an Energy_state (style 300) RC control.
// Build the nested "Target Primary Cell ID" RAN parameter the ns-3 gNB's
// GetTargetCell() decodes (8.4.4.1): a 4-level STRUCTURE ending in an NR-CGI
// element whose BIT_STRING holds the cell id as an ASCII digit (the gNB reads
// byte[0] - '0'). Cell ids are single-digit (2..8) across our validated envelope.
static seq_ran_param_t nsg_fill_target_cell_param(int cell_id)
{
  uint8_t digit = (uint8_t) ('0' + (cell_id % 10));
  byte_array_t src = { .len = 1, .buf = &digit };

  seq_ran_param_t p = {0};                          // L1: Target Primary Cell ID
  p.ran_param_id = 1;
  p.ran_param_val.type = STRUCTURE_RAN_PARAMETER_VAL_TYPE;
  p.ran_param_val.strct = calloc (1, sizeof (ran_param_struct_t));
  p.ran_param_val.strct->sz_ran_param_struct = 1;
  p.ran_param_val.strct->ran_param_struct = calloc (1, sizeof (seq_ran_param_t));

  seq_ran_param_t* l2 = &p.ran_param_val.strct->ran_param_struct[0];  // L2: CHOICE Target Cell
  l2->ran_param_id = 2;
  l2->ran_param_val.type = STRUCTURE_RAN_PARAMETER_VAL_TYPE;
  l2->ran_param_val.strct = calloc (1, sizeof (ran_param_struct_t));
  l2->ran_param_val.strct->sz_ran_param_struct = 1;
  l2->ran_param_val.strct->ran_param_struct = calloc (1, sizeof (seq_ran_param_t));

  seq_ran_param_t* l3 = &l2->ran_param_val.strct->ran_param_struct[0];  // L3: NR Cell
  l3->ran_param_id = 3;
  l3->ran_param_val.type = STRUCTURE_RAN_PARAMETER_VAL_TYPE;
  l3->ran_param_val.strct = calloc (1, sizeof (ran_param_struct_t));
  l3->ran_param_val.strct->sz_ran_param_struct = 1;
  l3->ran_param_val.strct->ran_param_struct = calloc (1, sizeof (seq_ran_param_t));

  seq_ran_param_t* l4 = &l3->ran_param_val.strct->ran_param_struct[0];  // L4: NR CGI (element, BIT_STRING)
  l4->ran_param_id = 4;
  l4->ran_param_val.type = ELEMENT_KEY_FLAG_FALSE_RAN_PARAMETER_VAL_TYPE;
  l4->ran_param_val.flag_false = calloc (1, sizeof (ran_parameter_value_t));
  l4->ran_param_val.flag_false->type = BIT_STRING_RAN_PARAMETER_VALUE;
  l4->ran_param_val.flag_false->bit_str_ran = copy_byte_array (src);

  return p;
}

static rc_ctrl_req_data_t nsg_gen_energy_ctrl_msg(int cell_id, int power_dbm)
{
  rc_ctrl_req_data_t rc_ctrl = {0};
  rc_ctrl.hdr.format = FORMAT_1_E2SM_RC_CTRL_HDR;
  rc_ctrl.hdr.frmt_1.ric_style_type = 300;         // Energy_state -> ns-3 gNB SetBSTX
  // NSG: carry the target TX power (dBm) in the action id, +1 because
  // RIC-ControlAction-ID is ASN.1-constrained to 1..65535 (0 fails to encode).
  // The gNB subtracts 1: action id 1 => 0 dBm (sleep), 31 => 30 dBm (wake).
  rc_ctrl.hdr.frmt_1.ctrl_act_id = power_dbm + 1;
  {
    lock_guard (&mtx);
    rc_ctrl.hdr.frmt_1.ue_id = cp_ue_id_e2sm (&ue_id);
  }
  rc_ctrl.msg.format = FORMAT_1_E2SM_RC_CTRL_MSG;
  rc_ctrl.msg.frmt_1.sz_ran_param = 1;
  rc_ctrl.msg.frmt_1.ran_param = calloc (1, sizeof (seq_ran_param_t));
  rc_ctrl.msg.frmt_1.ran_param[0] = nsg_fill_target_cell_param (cell_id);
  return rc_ctrl;
}

// Set cell_id's TX power (dBm) live: 0 sleeps it, >0 wakes it. The gNB handler
// loops all cells and toggles the matching one, so one send to any RC node suffices.
static void nsg_send_energy_control(e2_node_arr_xapp_t* nodes, int cell_id, int power_dbm)
{
  int const RC_ran_function = 3;
  for (size_t i = 0; i < nodes->len; ++i) {
    e2_node_connected_xapp_t* n = &nodes->n[i];
    size_t const idx = find_sm_idx (n->rf, n->len_rf, eq_sm, RC_ran_function);
    if (n->rf[idx].defn.type != RC_RAN_FUNC_DEF_E) continue;
    if (n->rf[idx].defn.rc.ctrl != NULL) {
      rc_ctrl_req_data_t rc_ctrl = nsg_gen_energy_ctrl_msg (cell_id, power_dbm);
      control_sm_xapp_api (&n->id, RC_ran_function, &rc_ctrl);
      free_rc_ctrl_req_data (&rc_ctrl);
      printf ("[NSG-BRIDGE] energy control sent: cell %d -> %d dBm (style 300)\n", cell_id, power_dbm);
      fflush (stdout);
      return;
    }
  }
}

// ─── NSG: live SLICE control (style 2, Radio Resource Allocation Control).
// (QCI, ceiling%) ride in the control action id as (qci << 8) | pct — the id is
// ASN.1-constrained to 1..65535 and qci >= 1 keeps it >= 256, so it always
// encodes. The gNB pushes the quota into the MAC scheduler's network-wide map,
// so ONE send to any RC node re-partitions every cell from the next subframe.
static rc_ctrl_req_data_t nsg_gen_slice_ctrl_msg(int qci, int pct)
{
  rc_ctrl_req_data_t rc_ctrl = {0};
  rc_ctrl.hdr.format = FORMAT_1_E2SM_RC_CTRL_HDR;
  rc_ctrl.hdr.frmt_1.ric_style_type = 2;   // Radio_Resource_Allocation_Control
  rc_ctrl.hdr.frmt_1.ctrl_act_id = ((qci & 0xFF) << 8) | (pct & 0xFF);
  {
    lock_guard (&mtx);
    rc_ctrl.hdr.frmt_1.ue_id = cp_ue_id_e2sm (&ue_id);
  }
  rc_ctrl.msg.format = FORMAT_1_E2SM_RC_CTRL_MSG;
  rc_ctrl.msg.frmt_1.sz_ran_param = 1;
  rc_ctrl.msg.frmt_1.ran_param = calloc (1, sizeof (seq_ran_param_t));
  // filler param — the gNB's style-2 handler reads only the header action id,
  // but the message still needs one well-formed RAN parameter to PER-encode.
  rc_ctrl.msg.frmt_1.ran_param[0] = nsg_fill_target_cell_param (1);
  return rc_ctrl;
}

static void nsg_send_slice_control(e2_node_arr_xapp_t* nodes, int qci, int pct)
{
  if (pct < 0) pct = 0;
  if (pct > 100) pct = 100;
  int const RC_ran_function = 3;
  for (size_t i = 0; i < nodes->len; ++i) {
    e2_node_connected_xapp_t* n = &nodes->n[i];
    size_t const idx = find_sm_idx (n->rf, n->len_rf, eq_sm, RC_ran_function);
    if (n->rf[idx].defn.type != RC_RAN_FUNC_DEF_E) continue;
    if (n->rf[idx].defn.rc.ctrl != NULL) {
      rc_ctrl_req_data_t rc_ctrl = nsg_gen_slice_ctrl_msg (qci, pct);
      control_sm_xapp_api (&n->id, RC_ran_function, &rc_ctrl);
      free_rc_ctrl_req_data (&rc_ctrl);
      printf ("[NSG-BRIDGE] slice control sent: qci %d ceiling %d%% (style 2)\n", qci, pct);
      fflush (stdout);
      return;
    }
  }
}

// ─── NSG: live HANDOVER (style 3, Connected_Mode_Mobility / Handover_Control).
// The gNB moves the UE named in the control header (its ran_UEID = IMSI) to the
// target cell (same nested Target-Cell RAN parameter as cell_off). We move the
// last UE seen in KPM (the global ue_id) to target_cell.
static rc_ctrl_req_data_t nsg_gen_ho_ctrl_msg(int target_cell, int ue_imsi)
{
  rc_ctrl_req_data_t rc_ctrl = {0};
  rc_ctrl.hdr.format = FORMAT_1_E2SM_RC_CTRL_HDR;
  rc_ctrl.hdr.frmt_1.ric_style_type = 3;   // Connected_Mode_Mobility
  rc_ctrl.hdr.frmt_1.ctrl_act_id = 1;      // Handover_Control
  {
    lock_guard (&mtx);
    rc_ctrl.hdr.frmt_1.ue_id = cp_ue_id_e2sm (&ue_id);   // start from a live UE id
  }
  // Target a SPECIFIC UE by IMSI (the KPM's per-UE id IS the IMSI). The gNB reads
  // the IMSI from ran_UEID; overwrite it so the customer can move any served UE,
  // not just whichever one KPM reported last (which may be an LTE-anchored UE).
  if (ue_imsi > 0 && rc_ctrl.hdr.frmt_1.ue_id.type == GNB_UE_ID_E2SM
      && rc_ctrl.hdr.frmt_1.ue_id.gnb.ran_ue_id != NULL) {
    *rc_ctrl.hdr.frmt_1.ue_id.gnb.ran_ue_id = (uint64_t) ue_imsi;
  }
  rc_ctrl.msg.format = FORMAT_1_E2SM_RC_CTRL_MSG;
  rc_ctrl.msg.frmt_1.sz_ran_param = 1;
  rc_ctrl.msg.frmt_1.ran_param = calloc (1, sizeof (seq_ran_param_t));
  rc_ctrl.msg.frmt_1.ran_param[0] = nsg_fill_target_cell_param (target_cell);
  return rc_ctrl;
}

static void nsg_send_ho_control(e2_node_arr_xapp_t* nodes, int target_cell, int ue_imsi)
{
  // The handover handler looks the UE up in the RECEIVING gNB's own ueMap, so we
  // must reach the UE's serving gNB. We don't track who serves it, so broadcast to
  // every RC-capable gNB — the serving one performs the HO, the rest log "not found".
  int const RC_ran_function = 3;
  int sent = 0;
  for (size_t i = 0; i < nodes->len; ++i) {
    e2_node_connected_xapp_t* n = &nodes->n[i];
    size_t const idx = find_sm_idx (n->rf, n->len_rf, eq_sm, RC_ran_function);
    if (n->rf[idx].defn.type != RC_RAN_FUNC_DEF_E) continue;
    if (n->rf[idx].defn.rc.ctrl != NULL) {
      rc_ctrl_req_data_t rc_ctrl = nsg_gen_ho_ctrl_msg (target_cell, ue_imsi);
      control_sm_xapp_api (&n->id, RC_ran_function, &rc_ctrl);
      free_rc_ctrl_req_data (&rc_ctrl);
      ++sent;
    }
  }
  printf ("[NSG-BRIDGE] handover control sent to %d gNB(s): UE %d -> cell %d (style 3)\n", sent, ue_imsi, target_cell);
  fflush (stdout);
}

int main(int argc, char* argv[])
{
  fr_args_t args = init_fr_args(argc, argv);

  // Init the xApp
  init_xapp_api(&args);
  sleep(1);

  e2_node_arr_xapp_t nodes = e2_nodes_xapp_api();
  assert(nodes.len > 0);

  printf("[KPM RC]: Connected E2 nodes = %d\n", nodes.len);

  pthread_mutexattr_t attr = {0};
  int rc = pthread_mutex_init(&mtx, &attr);
  assert(rc == 0);

  sm_ans_xapp_t* hndl = calloc(nodes.len, sizeof(sm_ans_xapp_t));
  assert(hndl != NULL);

  ////////////
  // START KPM
  ////////////
  int const KPM_ran_function = 2;

  for (size_t i = 0; i < nodes.len; ++i) {
    e2_node_connected_xapp_t* n = &nodes.n[i];

    size_t const idx = find_sm_idx(n->rf, n->len_rf, eq_sm, KPM_ran_function);
    assert(n->rf[idx].defn.type == KPM_RAN_FUNC_DEF_E && "KPM is not the received RAN Function");
    // if REPORT Service is supported by E2 node, send SUBSCRIPTION
    // e.g. OAI CU-CP
    if (n->rf[idx].defn.kpm.ric_report_style_list != NULL) {
      // Generate KPM SUBSCRIPTION message
      kpm_sub_data_t kpm_sub = gen_kpm_subs(&n->rf[idx].defn.kpm);

      hndl[i] = report_sm_xapp_api(&n->id, KPM_ran_function, &kpm_sub, sm_cb_kpm);
      assert(hndl[i].success == true);

      free_kpm_sub_data(&kpm_sub);
    }
  }
  ////////////
  // END KPM
  ////////////

  sleep(5);

  ////////////
  // START RC — one-shot (default) OR live bridge when NSG_CTRL_FIFO is set
  ////////////
  const char* nsg_fifo = getenv("NSG_CTRL_FIFO");
  if (nsg_fifo != NULL) {
    // NSG bridge mode: an EXTERNAL xApp (the customer's AI), relayed by the
    // backend, drives control LIVE while the ns-3 sim runs. KPM streams on
    // stdout (backend tails it); control commands arrive line-delimited on the
    // FIFO. Each command fires one real RC CONTROL on the live gNBs — a genuine
    // external closed-loop on the real ns-O-RAN engine.
    printf("[NSG-BRIDGE] live control via FIFO %s; KPM on stdout\n", nsg_fifo);
    fflush(stdout);
    int cfd = open(nsg_fifo, O_RDONLY | O_NONBLOCK);
    const char* smax = getenv("NSG_BRIDGE_SECS");
    long tick = 0, max_tick = (smax ? atol(smax) : 3600L) * 5;  // 0.2 s ticks
    char buf[512];
    // Purely time-bounded: do NOT poll try_stop_xapp_api() here — that call
    // actively tears the xApp down, so the normal teardown must stay AFTER the
    // loop (below), exactly as the one-shot path does.
    while (tick < max_tick) {
      if (cfd >= 0) {
        ssize_t k = read(cfd, buf, sizeof(buf) - 1);
        if (k > 0) {
          buf[k] = '\0';
          printf("[NSG-BRIDGE] ctrl cmd rx: %.*s\n", (int)k, buf);
          fflush(stdout);
          // Parse the command → a real Energy_state control (the gNB applies the
          // TX power to that cell): cell_off = 0 dBm (sleep), cell_on = wake at a
          // default power, cell_power = wake at "dbm". Anything else falls back to
          // the QoS-flow-mapping control (proves the loop, no RAN effect).
          char* cp = strstr(buf, "\"cell\"");
          int cell = -1;
          if (cp) { cp = strchr(cp, ':'); if (cp) cell = atoi(cp + 1); }
          if (cell >= 0 && strstr(buf, "cell_off") != NULL) {
            nsg_send_energy_control(&nodes, cell, 0);
          } else if (cell >= 0 && strstr(buf, "cell_on") != NULL) {
            nsg_send_energy_control(&nodes, cell, 30);
          } else if (cell >= 0 && strstr(buf, "cell_power") != NULL) {
            int dbm = 30;
            char* dp = strstr(buf, "\"dbm\"");
            if (dp) { dp = strchr(dp, ':'); if (dp) dbm = atoi(dp + 1); }
            nsg_send_energy_control(&nodes, cell, dbm);
          } else if (cell >= 0 && strstr(buf, "handover") != NULL) {
            int ue = 0;  // IMSI of the UE to move (the KPM per-UE id is the IMSI)
            char* up = strstr(buf, "\"ue\"");
            if (up) { up = strchr(up, ':'); if (up) ue = atoi(up + 1); }
            nsg_send_ho_control(&nodes, cell, ue);
          } else if (strstr(buf, "slice_quota") != NULL) {
            // {"cmd":"slice_quota","qci":9,"pct":30} -> style-2 slice control
            int qci = -1, pct = -1;
            char* qp = strstr(buf, "\"qci\"");
            if (qp) { qp = strchr(qp, ':'); if (qp) qci = atoi(qp + 1); }
            char* pp = strstr(buf, "\"pct\"");
            if (pp) { pp = strchr(pp, ':'); if (pp) pct = atoi(pp + 1); }
            if (qci >= 1 && pct >= 0) nsg_send_slice_control(&nodes, qci, pct);
          } else {
            nsg_send_rc_control(&nodes);
          }
        }
      }
      usleep(200000);  // 0.2 s poll
      ++tick;
    }
    if (cfd >= 0) close(cfd);
  } else {
    // Original behaviour: one-shot control, then block until the run ends.
    nsg_send_rc_control(&nodes);
    xapp_wait_end_api();
  }
  ////////////
  // END RC
  ////////////

  for (int i = 0; i < nodes.len; ++i) {
    // Remove the handle previously returned
    if (hndl[i].success == true)
      rm_report_sm_xapp_api(hndl[i].u.handle);
  }
  free(hndl);

  // Stop the xApp
  while (try_stop_xapp_api() == false)
    usleep(1000);

  free_e2_node_arr_xapp(&nodes);

  rc = pthread_mutex_destroy(&mtx);
  assert(rc == 0);

  printf("[KPM RC]: Test xApp run SUCCESSFULLY\n");
}
