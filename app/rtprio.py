#!/usr/bin/env python3
"""Real-time scheduling helper for Shairport Sync.

AirPlay 2 group timing is driven by NQPTP: the PTP samples and the audio
thread must not be preempted, otherwise a group join is rejected by the
sender ("playback error" in the iOS menu) and an established group drops
out after a few seconds.

This image is Alpine (musl). musl's sched_setscheduler() wrapper returns
ENOSYS (errno 38) even though the kernel supports the call, so the usual
os.sched_setscheduler() promotion always failed here and shairport-sync kept
running as SCHED_OTHER. The kernel syscall itself works, so fall back to it.
"""
import ctypes
import os
import platform
import sys

SCHED_OTHER = 0
SCHED_FIFO = 1

# sched_setscheduler(2) numbers per architecture.
SYS_SCHED_SETSCHEDULER = {
    "x86_64": 144,
    "amd64": 144,
    "aarch64": 119,
    "arm64": 119,
    "armv7l": 156,
    "armv8l": 156,
    "armv6l": 156,
    "i386": 156,
    "i686": 156,
}


def _libc():
    for name in ("libc.so.6", "libc.so"):
        try:
            return ctypes.CDLL(name, use_errno=True)
        except OSError:
            continue
    return None


def promote(pid=0, prio=20):
    """Put `pid` (0 = this process) into SCHED_FIFO. Returns (ok, how)."""
    try:
        os.sched_setscheduler(pid, SCHED_FIFO, os.sched_param(prio))
        return True, "sched_setscheduler"
    except OSError as e:
        first_error = "%s(%d)" % (e.strerror or "error", e.errno or 0)
        if (e.errno or 0) not in (38, 22):  # ENOSYS / EINVAL only
            return False, first_error

    num = SYS_SCHED_SETSCHEDULER.get(platform.machine().lower())
    libc = _libc()
    if num is None or libc is None:
        return False, first_error
    param = ctypes.c_int(int(prio))
    ctypes.set_errno(0)
    ret = libc.syscall(num, int(pid), SCHED_FIFO, ctypes.byref(param))
    if ret == 0:
        return True, "syscall"
    return False, "syscall errno %d" % ctypes.get_errno()


def promote_threads(pid, prio=20):
    """Promote every thread of `pid`, not just its main thread.

    sched_setscheduler(pid) only affects the *main* thread. Shairport Sync
    creates its RTSP listener, metadata and player threads at startup, i.e.
    before the panel promotes it, so those threads stayed SCHED_OTHER. On this
    heavily loaded host that delayed RTSP replies and PTP handling badly enough
    for the sender to drop the receiver out of an AirPlay 2 group.
    """
    try:
        tids = [int(t) for t in os.listdir("/proc/%d/task" % int(pid))]
    except Exception as e:
        return False, "no task dir: %r" % (e,)
    done = 0
    failed = 0
    for tid in tids:
        ok, _how = promote(tid, prio)
        if ok:
            done += 1
        else:
            failed += 1
    return (failed == 0), "%d/%d threads" % (done, len(tids))


def threads_policy(pid):
    """'SCHED_FIFO' only if every thread of the process is FIFO."""
    try:
        tids = [int(t) for t in os.listdir("/proc/%d/task" % int(pid))]
    except Exception:
        return "not running"
    policies = [policy_of(t) for t in tids]
    if not policies:
        return "?"
    if all(p == "SCHED_FIFO" for p in policies):
        return "SCHED_FIFO"
    other = sorted({p for p in policies if p != "SCHED_FIFO"})
    return "mixed(%s)" % ",".join(other)


def policy_of(pid):
    """Scheduling policy of `pid` as a readable name (no libc needed)."""
    try:
        with open("/proc/%d/stat" % int(pid), "r") as f:
            stat = f.read()
        fields = stat[stat.rindex(")") + 2:].split()
        policy = int(fields[38])       # 41st field overall
    except Exception:
        try:
            return {0: "SCHED_OTHER", 1: "SCHED_FIFO", 2: "SCHED_RR", 5: "SCHED_IDLE",
                    6: "SCHED_DEADLINE"}.get(os.sched_getscheduler(pid), "?")
        except Exception:
            return "?"
    return {0: "SCHED_OTHER", 1: "SCHED_FIFO", 2: "SCHED_RR", 5: "SCHED_IDLE",
            6: "SCHED_DEADLINE"}.get(policy, str(policy))


def main(argv):
    pid = 0
    prio = 20
    if len(argv) > 1:
        pid = int(argv[1])
    if len(argv) > 2:
        prio = int(argv[2])
    ok, how = promote(pid, prio)
    print("rtprio pid=%s prio=%d -> %s (%s)" % (pid, prio, "ok" if ok else "failed", how),
          flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
