"""Kisa bir CANLI oturum ac ve kapat; amac kalicilik yolunu uctan uca denemek."""
import sys
from harness import run
from scenario import build
log = run(build(seed=1), lambda kpm: [], max_ticks=12)
print("ok:", log.ok, "| frame:", len(log.ticks), "| hata:", log.error)
