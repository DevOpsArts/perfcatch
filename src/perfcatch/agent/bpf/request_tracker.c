/*
 * request_tracker.c - eBPF program to track HTTP request lifecycle
 *
 * Correlates incoming TCP connections with resource usage per request.
 * Uses socket pointer as key (stable across threads) and sched_switch
 * tracepoint for accurate CPU time measurement.
 */

#include <uapi/linux/ptrace.h>
#include <net/sock.h>
#include <linux/tcp.h>
#include <linux/sched.h>
#include <bcc/proto.h>

/* Max bytes to capture from first recv for header parsing */
#define HEADER_BUF_SIZE 256

/* Per-request tracking state */
struct request_state {
    u64 start_ns;          /* Request start timestamp */
    u32 pid;               /* Process ID (tgid) */
    u32 tid;               /* Thread ID */
    u64 bytes_sent;        /* Total bytes sent */
    u64 bytes_recv;        /* Total bytes received */
    u16 dport;             /* Remote port */
    u16 sport;             /* Local port (server port) */
    u32 saddr;             /* Source IP */
    u32 daddr;             /* Destination IP */
    char comm[16];         /* Process command name */
    u8 headers_captured;   /* Flag: first recv already captured */
};

/* Completed request event sent to userspace */
struct request_event {
    u64 start_ns;
    u64 duration_ns;
    u64 cpu_time_ns;       /* Actual on-CPU time from sched_switch tracking */
    u32 pid;
    u32 tid;
    u64 bytes_sent;
    u64 bytes_recv;
    u16 lport;             /* Local port */
    u16 dport;             /* Remote port */
    u32 saddr;
    u32 daddr;
    char comm[16];
    char header_buf[HEADER_BUF_SIZE]; /* First bytes of HTTP request for header parsing */
    u16 header_len;        /* Actual bytes captured */
};

/* Map: sock -> captured HTTP header bytes */
struct header_data {
    char buf[HEADER_BUF_SIZE];
    u16 len;
};
BPF_HASH(sock_headers, struct sock *, struct header_data, 4096);

/* Map: socket pointer -> request state (active requests) */
BPF_HASH(active_requests, struct sock *, struct request_state, 65536);

/* Map: pid_tgid -> sock pointer (for recvmsg return probe) */
BPF_HASH(sock_by_thread, u64, struct sock *, 65536);

/* CPU time tracking via sched_switch */
/* Map: tid -> sock pointer (threads handling active requests) */
BPF_HASH(tid_to_sock, u32, struct sock *, 65536);

/* Map: tid -> timestamp when last scheduled ON cpu */
BPF_HASH(tid_on_cpu_ts, u32, u64, 65536);

/* Map: sock pointer -> accumulated cpu nanoseconds */
BPF_HASH(sock_cpu_ns, struct sock *, u64, 65536);

/* Per-CPU array to avoid stack overflow (event struct > 512 bytes) */
BPF_PERCPU_ARRAY(event_buf, struct request_event, 1);

/* Ring buffer for completed request events */
BPF_PERF_OUTPUT(request_events);

/*
 * Kretprobe: inet_csk_accept return
 * Triggered when a new TCP connection is accepted (incoming request start)
 */
int trace_accept_return(struct pt_regs *ctx) {
    struct sock *sk = (struct sock *)PT_REGS_RC(ctx);
    if (!sk)
        return 0;

    u64 pid_tgid = bpf_get_current_pid_tgid();
    u32 pid = pid_tgid >> 32;
    u32 tid = (u32)pid_tgid;

    struct request_state state = {};
    state.start_ns = bpf_ktime_get_ns();
    state.pid = pid;
    state.tid = tid;
    state.bytes_sent = 0;
    state.bytes_recv = 0;

    /* Extract connection info */
    u16 dport = 0;
    u16 sport = 0;
    u32 saddr = 0;
    u32 daddr = 0;

    bpf_probe_read_kernel(&sport, sizeof(sport), &sk->__sk_common.skc_num);
    bpf_probe_read_kernel(&dport, sizeof(dport), &sk->__sk_common.skc_dport);
    bpf_probe_read_kernel(&saddr, sizeof(saddr), &sk->__sk_common.skc_rcv_saddr);
    bpf_probe_read_kernel(&daddr, sizeof(daddr), &sk->__sk_common.skc_daddr);

    state.sport = sport;
    state.dport = ntohs(dport);
    state.saddr = saddr;
    state.daddr = daddr;

    bpf_get_current_comm(&state.comm, sizeof(state.comm));

    active_requests.update(&sk, &state);

    return 0;
}

/*
 * Helper: Register this thread as handling an active request socket.
 * Enables sched_switch CPU tracking for this thread.
 */
static __always_inline void register_thread_for_cpu(struct sock *sk) {
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u32 tid = (u32)pid_tgid;

    /* Only register once per thread-socket pair */
    struct sock **existing = tid_to_sock.lookup(&tid);
    if (existing)
        return;

    tid_to_sock.update(&tid, &sk);

    /* Record current time as on-cpu start (thread is running now) */
    u64 now = bpf_ktime_get_ns();
    tid_on_cpu_ts.update(&tid, &now);

    /* Initialize cpu accumulator for this socket if not present */
    u64 *existing_cpu = sock_cpu_ns.lookup(&sk);
    if (!existing_cpu) {
        u64 zero = 0;
        sock_cpu_ns.update(&sk, &zero);
    }
}

/*
 * Kprobe: tcp_sendmsg
 * Track bytes sent per connection
 */
int trace_tcp_sendmsg(struct pt_regs *ctx, struct sock *sk, struct msghdr *msg, size_t size) {
    struct request_state *state = active_requests.lookup(&sk);
    if (!state)
        return 0;

    register_thread_for_cpu(sk);
    state->bytes_sent += size;
    return 0;
}

/* Map: pid_tgid -> sock pointer for header capture across syscall boundary */
struct recv_args {
    struct sock *sk;
};
BPF_HASH(recv_args_map, u64, struct recv_args, 65536);

/* Map: pid_tgid -> user buffer address saved at syscall entry */
BPF_HASH(recv_ubuf_map, u64, u64, 65536);

/*
 * Tracepoint: syscalls/sys_enter_recvfrom
 * Save user buffer pointer BEFORE tcp_recvmsg fires.
 * Order: sys_enter_recvfrom → tcp_recvmsg_entry → tcp_recvmsg_return → sys_exit_recvfrom
 */
TRACEPOINT_PROBE(syscalls, sys_enter_recvfrom) {
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u64 ubuf = (u64)args->ubuf;
    recv_ubuf_map.update(&pid_tgid, &ubuf);
    return 0;
}

/*
 * Tracepoint: syscalls/sys_enter_read
 * Save user buffer pointer unconditionally for read() on sockets.
 * If this read() is not on a socket, tcp_recvmsg won't fire and
 * sys_exit_read will just clean up without capturing.
 */
TRACEPOINT_PROBE(syscalls, sys_enter_read) {
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u64 ubuf = (u64)args->buf;
    recv_ubuf_map.update(&pid_tgid, &ubuf);
    return 0;
}

/*
 * Kprobe: tcp_recvmsg entry
 * Fires AFTER sys_enter_read/recvfrom but BEFORE sys_exit.
 * Save sock pointer for header capture and register for CPU tracking.
 */
int trace_tcp_recvmsg_entry(struct pt_regs *ctx, struct sock *sk) {
    u64 pid_tgid = bpf_get_current_pid_tgid();
    sock_by_thread.update(&pid_tgid, &sk);

    struct recv_args args = {};
    args.sk = sk;
    recv_args_map.update(&pid_tgid, &args);

    struct request_state *state = active_requests.lookup(&sk);
    if (state)
        register_thread_for_cpu(sk);

    return 0;
}

/*
 * Kretprobe: tcp_recvmsg return
 * Track bytes received. Do NOT delete recv_args_map here -
 * sys_exit_read/recvfrom needs it for header capture.
 */
int trace_tcp_recvmsg_return(struct pt_regs *ctx) {
    int bytes = PT_REGS_RC(ctx);
    if (bytes <= 0)
        return 0;

    u64 pid_tgid = bpf_get_current_pid_tgid();
    struct recv_args *rargs = recv_args_map.lookup(&pid_tgid);
    if (!rargs)
        return 0;

    struct sock *sk = rargs->sk;
    struct request_state *state = active_requests.lookup(&sk);
    if (state) {
        state->bytes_recv += (u64)bytes;
    }

    /* Do NOT delete recv_args_map - sys_exit needs it for header capture */
    return 0;
}

/*
 * Helper: capture HTTP headers from user buffer after syscall completes.
 * Called from sys_exit_recvfrom and sys_exit_read.
 */
static __always_inline void try_capture_headers(u64 pid_tgid, long ret) {
    if (ret <= 0)
        goto cleanup;

    u64 *ubuf_ptr = recv_ubuf_map.lookup(&pid_tgid);
    if (!ubuf_ptr)
        goto cleanup_args;

    u64 ubuf = *ubuf_ptr;

    struct recv_args *rargs = recv_args_map.lookup(&pid_tgid);
    if (!rargs)
        goto cleanup;

    struct sock *sk = rargs->sk;
    struct request_state *state = active_requests.lookup(&sk);
    if (!state || state->headers_captured)
        goto cleanup;

    /* Check if buffer starts with HTTP method */
    char peek = 0;
    bpf_probe_read_user(&peek, 1, (void *)ubuf);
    if (peek != 'G' && peek != 'P' && peek != 'D' &&
        peek != 'H' && peek != 'O' && peek != 'C')
        goto cleanup;

    /* Capture headers */
    struct header_data hdr = {};
    u16 to_read = ret < HEADER_BUF_SIZE ? (u16)ret : HEADER_BUF_SIZE;
    hdr.len = to_read;
    bpf_probe_read_user(&hdr.buf, HEADER_BUF_SIZE, (void *)ubuf);
    sock_headers.update(&sk, &hdr);
    state->headers_captured = 1;

cleanup:
    recv_ubuf_map.delete(&pid_tgid);
cleanup_args:
    recv_args_map.delete(&pid_tgid);
    sock_by_thread.delete(&pid_tgid);
}

/*
 * Tracepoint: syscalls/sys_exit_recvfrom
 * At this point, data has been copied to user buffer and tcp_recvmsg has
 * already set recv_args_map. Safe to read from ubuf and capture headers.
 */
TRACEPOINT_PROBE(syscalls, sys_exit_recvfrom) {
    u64 pid_tgid = bpf_get_current_pid_tgid();
    try_capture_headers(pid_tgid, args->ret);
    return 0;
}

/*
 * Tracepoint: syscalls/sys_exit_read
 * Same as sys_exit_recvfrom but for read() syscall on sockets.
 */
TRACEPOINT_PROBE(syscalls, sys_exit_read) {
    u64 pid_tgid = bpf_get_current_pid_tgid();
    try_capture_headers(pid_tgid, args->ret);
    return 0;
}

/*
 * Tracepoint: sched_switch
 * Accumulate actual on-CPU nanoseconds for threads handling requests.
 */
TRACEPOINT_PROBE(sched, sched_switch) {
    u64 now = bpf_ktime_get_ns();

    /* Task being switched OFF: accumulate its on-cpu time */
    u32 prev_tid = args->prev_pid;  /* kernel: prev_pid is the tid */
    struct sock **prev_sk = tid_to_sock.lookup(&prev_tid);
    if (prev_sk) {
        u64 *on_ts = tid_on_cpu_ts.lookup(&prev_tid);
        if (on_ts) {
            u64 delta = now - *on_ts;
            u64 *accum = sock_cpu_ns.lookup(prev_sk);
            if (accum) {
                *accum += delta;
            }
        }
        tid_on_cpu_ts.delete(&prev_tid);
    }

    /* Task being switched ON: record start time if it's tracked */
    u32 next_tid = args->next_pid;
    struct sock **next_sk = tid_to_sock.lookup(&next_tid);
    if (next_sk) {
        tid_on_cpu_ts.update(&next_tid, &now);
    }

    return 0;
}

/*
 * Kprobe: tcp_close
 * Connection closed - emit completed request event with real CPU time
 */
int trace_tcp_close(struct pt_regs *ctx, struct sock *sk) {
    struct request_state *state = active_requests.lookup(&sk);
    if (!state)
        return 0;

    u64 now = bpf_ktime_get_ns();
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u32 tid = (u32)pid_tgid;

    /* Compute CPU time: accumulated + final on-cpu slice */
    u64 cpu_total = 0;
    u64 *accum = sock_cpu_ns.lookup(&sk);
    if (accum)
        cpu_total = *accum;

    /* Add current on-cpu slice (thread is running now at close time) */
    u64 *on_ts = tid_on_cpu_ts.lookup(&tid);
    if (on_ts) {
        u64 final_slice = now - *on_ts;
        cpu_total += final_slice;
    }

    struct request_event *event;
    int zero = 0;
    event = event_buf.lookup(&zero);
    if (!event)
        return 0;

    __builtin_memset(event, 0, sizeof(*event));
    event->start_ns = state->start_ns;
    event->duration_ns = now - state->start_ns;
    event->cpu_time_ns = cpu_total;
    event->pid = state->pid;
    event->tid = state->tid;
    event->bytes_sent = state->bytes_sent;
    event->bytes_recv = state->bytes_recv;
    event->lport = state->sport;
    event->dport = state->dport;
    event->saddr = state->saddr;
    event->daddr = state->daddr;
    __builtin_memcpy(event->comm, state->comm, sizeof(event->comm));

    /* Copy captured HTTP headers if available */
    struct header_data *hdr = sock_headers.lookup(&sk);
    if (hdr) {
        __builtin_memcpy(event->header_buf, hdr->buf, HEADER_BUF_SIZE);
        event->header_len = hdr->len;
    } else {
        event->header_len = 0;
    }

    request_events.perf_submit(ctx, event, sizeof(*event));

    /* Cleanup all maps */
    active_requests.delete(&sk);
    sock_cpu_ns.delete(&sk);
    sock_headers.delete(&sk);
    tid_to_sock.delete(&tid);
    tid_on_cpu_ts.delete(&tid);

    return 0;
}
