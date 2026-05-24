/*
 * resource_tracker.c - eBPF program to track CPU and memory per-process
 *
 * Uses scheduler tracepoints to measure CPU time consumed,
 * and cgroup memory accounting for memory usage per request.
 */

#include <uapi/linux/ptrace.h>
#include <linux/sched.h>
#include <linux/mm_types.h>

/* CPU time sample (collected on context switch) */
struct cpu_sample {
    u64 timestamp_ns;
    u32 pid;
    u32 tid;
    u64 on_cpu_ns;         /* Time spent on CPU since last switch */
    char comm[16];
};

/* Memory snapshot for a process */
struct mem_snapshot {
    u64 timestamp_ns;
    u32 pid;
    u64 rss_bytes;         /* Resident set size */
    u64 vm_bytes;          /* Virtual memory size */
    char comm[16];
};

/* Track when each thread was scheduled on */
BPF_HASH(thread_start, u64, u64, 65536);

/* Per-pid cumulative CPU time within measurement window */
BPF_HASH(pid_cpu_time, u32, u64, 16384);

/* Output events */
BPF_PERF_OUTPUT(cpu_events);
BPF_PERF_OUTPUT(mem_events);

/*
 * Tracepoint: sched_switch
 * Measure CPU time for the task being switched off
 */
TRACEPOINT_PROBE(sched, sched_switch) {
    u64 now = bpf_ktime_get_ns();
    u32 prev_pid = args->prev_pid;
    u32 next_pid = args->next_pid;

    /* Calculate CPU time for the task being switched off */
    u64 prev_key = ((u64)prev_pid << 32) | args->prev_pid;
    u64 *start = thread_start.lookup(&prev_key);
    if (start) {
        u64 delta = now - *start;

        /* Accumulate CPU time for this PID */
        u64 *total = pid_cpu_time.lookup(&prev_pid);
        if (total) {
            *total += delta;
        } else {
            pid_cpu_time.update(&prev_pid, &delta);
        }

        thread_start.delete(&prev_key);
    }

    /* Record start time for the task being switched on */
    u64 next_key = ((u64)next_pid << 32) | next_pid;
    thread_start.update(&next_key, &now);

    return 0;
}

/*
 * Kprobe: finish_task_switch (alternative for older kernels)
 * Fallback CPU tracking method
 */
int trace_finish_task_switch(struct pt_regs *ctx, struct task_struct *prev) {
    u64 now = bpf_ktime_get_ns();
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u32 pid = pid_tgid >> 32;

    /* Mark when this task starts running */
    thread_start.update(&pid_tgid, &now);

    /* If prev task was being tracked, record its CPU usage */
    u32 prev_pid = 0;
    bpf_probe_read_kernel(&prev_pid, sizeof(prev_pid), &prev->tgid);

    u64 prev_key = ((u64)prev_pid << 32) | prev_pid;
    u64 *start = thread_start.lookup(&prev_key);
    if (start) {
        u64 delta = now - *start;
        u64 *total = pid_cpu_time.lookup(&prev_pid);
        if (total) {
            *total += delta;
        } else {
            pid_cpu_time.update(&prev_pid, &delta);
        }
        thread_start.delete(&prev_key);
    }

    return 0;
}

/*
 * Periodic memory sampling (triggered via perf event or timer)
 * Reads RSS from task_struct->mm
 */
int sample_memory(struct pt_regs *ctx) {
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u32 pid = pid_tgid >> 32;

    struct task_struct *task = (struct task_struct *)bpf_get_current_task();
    struct mm_struct *mm = NULL;
    bpf_probe_read_kernel(&mm, sizeof(mm), &task->mm);
    if (!mm)
        return 0;

    struct mem_snapshot snap = {};
    snap.timestamp_ns = bpf_ktime_get_ns();
    snap.pid = pid;
    bpf_get_current_comm(&snap.comm, sizeof(snap.comm));

    /* Read RSS counter (in pages) */
    long rss = 0;
    bpf_probe_read_kernel(&rss, sizeof(rss), &mm->rss_stat);
    snap.rss_bytes = (u64)rss * 4096;  /* PAGE_SIZE */

    unsigned long total_vm = 0;
    bpf_probe_read_kernel(&total_vm, sizeof(total_vm), &mm->total_vm);
    snap.vm_bytes = total_vm * 4096;

    mem_events.perf_submit(ctx, &snap, sizeof(snap));
    return 0;
}
