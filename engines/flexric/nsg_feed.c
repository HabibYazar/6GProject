#include "nsg_feed.h"

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#define NSG_FEED_MAX 8192
#define NSG_KEY_MAX  64

/* The feed is rewritten once a KPM window and read on every filler call, so
 * it is parsed at most once per change and served from memory after that. */
typedef struct {
  char key[NSG_KEY_MAX];
  uint64_t imsi;
  double value;
} nsg_rec_t;

#define NSG_UE_MAX 512

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static uint64_t g_ue[NSG_UE_MAX];
static int g_ue_n = 0;
static nsg_rec_t g_rec[NSG_FEED_MAX];
static int g_n = 0;
static time_t g_mtime = 0;
static off_t g_size = -1;
static char g_path[512] = "/tmp/nsg_tier_b_kpm.txt";

void nsg_feed_set_path(const char* path)
{
  pthread_mutex_lock(&g_lock);
  snprintf(g_path, sizeof(g_path), "%s",
           path ? path : "/tmp/nsg_tier_b_kpm.txt");
  /* Force a reparse: a new path with a coincidentally equal mtime would
   * otherwise serve the previous file's records. */
  g_mtime = 0;
  g_size = -1;
  g_n = 0;
  g_ue_n = 0;
  pthread_mutex_unlock(&g_lock);
}

/* Caller holds the lock. */
static void reload_locked(void)
{
  struct stat st;
  if (stat(g_path, &st) != 0) {
    g_n = 0;
    g_ue_n = 0;
    g_mtime = 0;
    g_size = -1;
    return;
  }
  if (st.st_mtime == g_mtime && st.st_size == g_size)
    return;                                   /* unchanged since last parse */

  FILE* fh = fopen(g_path, "r");
  if (fh == NULL) {
    g_n = 0;
    g_ue_n = 0;
    return;
  }
  int n = 0;
  char line[256];
  while (n < NSG_FEED_MAX && fgets(line, sizeof(line), fh) != NULL) {
    if (line[0] == '#' || line[0] == '\n')
      continue;
    char key[NSG_KEY_MAX];
    unsigned long long imsi = 0;
    double value = 0.0;
    if (sscanf(line, "%63s %llu %lf", key, &imsi, &value) != 3)
      continue;                               /* a malformed line is skipped,
                                                 never guessed at */
    snprintf(g_rec[n].key, NSG_KEY_MAX, "%s", key);
    g_rec[n].imsi = (uint64_t)imsi;
    g_rec[n].value = value;
    n++;
  }
  fclose(fh);
  /* The distinct UEs, ascending. Insertion sort over an array that is at
   * most a few hundred long and reparsed once a window — a hash table here
   * would be more code for no measurable difference. */
  int un = 0;
  for (int i = 0; i < n; i++) {
    uint64_t id = g_rec[i].imsi;
    int at = 0;
    while (at < un && g_ue[at] < id)
      at++;
    if (at < un && g_ue[at] == id)
      continue;                               /* already have it */
    if (un >= NSG_UE_MAX)
      continue;
    for (int k = un; k > at; k--)
      g_ue[k] = g_ue[k - 1];
    g_ue[at] = id;
    un++;
  }
  g_ue_n = un;

  /* Publish the new parse only once it is complete: a filler that read
   * halfway through would mix two windows. */
  g_n = n;
  g_mtime = st.st_mtime;
  g_size = st.st_size;
}

bool nsg_feed_get(const char* key, uint64_t imsi, double* out)
{
  if (key == NULL || out == NULL)
    return false;
  bool found = false;
  pthread_mutex_lock(&g_lock);
  reload_locked();
  for (int i = 0; i < g_n; i++) {
    if (g_rec[i].imsi == imsi && strcmp(g_rec[i].key, key) == 0) {
      *out = g_rec[i].value;
      found = true;
      break;
    }
  }
  pthread_mutex_unlock(&g_lock);
  return found;
}

static pthread_mutex_t g_ctrl_lock = PTHREAD_MUTEX_INITIALIZER;
static char g_ctrl_path[512] = "/tmp/nsg_tier_b_ctrl.txt";

void nsg_ctrl_set_path(const char* path)
{
  pthread_mutex_lock(&g_ctrl_lock);
  snprintf(g_ctrl_path, sizeof(g_ctrl_path), "%s",
           path ? path : "/tmp/nsg_tier_b_ctrl.txt");
  pthread_mutex_unlock(&g_ctrl_lock);
}

bool nsg_ctrl_append(const char* line)
{
  if (line == NULL)
    return false;
  pthread_mutex_lock(&g_ctrl_lock);
  FILE* fh = fopen(g_ctrl_path, "a");
  bool ok = false;
  if (fh != NULL) {
    ok = fprintf(fh, "%s\n", line) > 0;
    /* Flush inside the lock: the reader drains once a window and a line
     * still sitting in stdio would arrive a window late, or not at all if
     * the agent is killed between. */
    if (fflush(fh) != 0)
      ok = false;
    fclose(fh);
  }
  pthread_mutex_unlock(&g_ctrl_lock);
  return ok;
}

int nsg_feed_ue_count(void)
{
  pthread_mutex_lock(&g_lock);
  reload_locked();
  int n = g_ue_n;
  pthread_mutex_unlock(&g_lock);
  return n;
}

uint64_t nsg_feed_ue_at(int index)
{
  pthread_mutex_lock(&g_lock);
  reload_locked();
  uint64_t id = (index >= 0 && index < g_ue_n) ? g_ue[index] : 0;
  pthread_mutex_unlock(&g_lock);
  return id;
}

int nsg_feed_size(void)
{
  pthread_mutex_lock(&g_lock);
  reload_locked();
  int n = g_n;
  pthread_mutex_unlock(&g_lock);
  return n;
}
