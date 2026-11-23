/* Tier B's KPM values, for the emulator agent's service models.
 *
 * Phase 1 proved FlexRIC's agent API needs no ns-3 under it; what it fed the
 * xApp was rand()%1024. This reads the fast tier's real numbers instead.
 *
 * The source is a flat file the Python side rewrites atomically once a KPM
 * window (nsg_ai/e2_feed.py). A file rather than a socket because the value
 * changes once a window and the fillers run on FlexRIC's own thread: a
 * rename needs no protocol, no lifecycle and no failure mode of its own.
 *
 * nsg_feed_get returns false when there is no feed, when it is stale, or
 * when it does not carry the key — and the caller keeps its stock random
 * value, so the upstream example still runs untouched with nothing else
 * installed.
 */
#ifndef NSG_FEED_H
#define NSG_FEED_H

#include <stdbool.h>
#include <stdint.h>

/* Path override, for tests. NULL restores the default. */
void nsg_feed_set_path(const char* path);

/* Latest published value for (key, imsi). False leaves *out untouched. */
bool nsg_feed_get(const char* key, uint64_t imsi, double* out);

/* How many records the last successful parse held. 0 means no feed. */
int nsg_feed_size(void);

/* The UEs the feed carries, in ascending IMSI order and without repeats.
 *
 * The agent's UE list has to come from here, not from its own constants:
 * sm_kpm.c sets `gnb.amf_ue_ngap_id = 112358132134` for every UE, so an
 * emulator reports one identity and varies only the numbers. Per-UE values
 * need per-UE identities, and the only place that knows them is the feed.
 *
 * nsg_feed_ue_count() returns 0 when there is no feed, and the caller keeps
 * whatever UE list it built for itself.
 */
int nsg_feed_ue_count(void);
uint64_t nsg_feed_ue_at(int index);

/* THE OTHER DIRECTION. An xApp that can read the fast tier but not steer it
 * is a data feed, not a RIC test: the point of a RIC is that a decision has
 * a consequence the next report shows.
 *
 * The agent's RC handler already parses a control message and answers it;
 * it just had nowhere to put it. This appends one line to a control file the
 * Python side drains once a window. Append rather than replace because two
 * controls can arrive between windows and the second must not erase the
 * first, and O_APPEND on a line shorter than PIPE_BUF is atomic enough for
 * a file one process writes.
 *
 * Returns false if the line could not be written, which the caller reports
 * rather than swallowing — a control silently dropped is worse than one
 * refused.
 */
bool nsg_ctrl_append(const char* line);

/* Path override, for tests. NULL restores the default. */
void nsg_ctrl_set_path(const char* path);

#endif
