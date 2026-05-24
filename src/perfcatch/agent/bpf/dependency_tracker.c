/*
 * dependency_tracker.c - eBPF program to track outgoing dependency calls
 *
 * Monitors connect() syscalls to track when a request makes outbound
 * connections to dependency services (databases, caches, other APIs).
 */

#include <uapi/linux/ptrace.h>
#include <net/sock.h>
#include <linux/socket.h>
#include <linux/in.h>

/* Dependency call event */
struct dep_event {
    u64 start_ns;
    u64 duration_ns;
    u32 pid;
    u32 tid;
    u64 bytes_sent;
    u64 bytes_recv;
    u32 dest_ip;           /* Dependency service IP */
    u16 dest_port;         /* Dependency service port */
    u16 protocol;          /* IPPROTO_TCP or IPPROTO_UDP */
    char comm[16];
};

/* Track connect() start times */
struct connect_state {
    u64 start_ns;
    u32 dest_ip;
    u16 dest_port;
};

BPF_HASH(active_connects, u64, struct connect_state, 32768);
BPF_HASH(dep_bytes_sent, u64, u64, 32768);
BPF_HASH(dep_bytes_recv, u64, u64, 32768);
BPF_PERF_OUTPUT(dep_events);

/*
 * Kprobe: tcp_v4_connect
 * Track outgoing connection attempts (dependency calls)
 */
int trace_connect_entry(struct pt_regs *ctx, struct sock *sk,
                        struct sockaddr *addr, int addrlen) {
    u64 pid_tgid = bpf_get_current_pid_tgid();

    /* Only track IPv4 TCP connections */
    u16 family = 0;
    bpf_probe_read_kernel(&family, sizeof(family), &sk->__sk_common.skc_family);
    if (family != AF_INET)
        return 0;

    struct connect_state state = {};
    state.start_ns = bpf_ktime_get_ns();

    /* Read destination from sockaddr */
    struct sockaddr_in *sin = (struct sockaddr_in *)addr;
    bpf_probe_read_user(&state.dest_ip, sizeof(state.dest_ip), &sin->sin_addr.s_addr);
    bpf_probe_read_user(&state.dest_port, sizeof(state.dest_port), &sin->sin_port);
    state.dest_port = ntohs(state.dest_port);

    active_connects.update(&pid_tgid, &state);
    return 0;
}

/*
 * Kretprobe: tcp_v4_connect return
 * Connection established or failed
 */
int trace_connect_return(struct pt_regs *ctx) {
    int ret = PT_REGS_RC(ctx);
    u64 pid_tgid = bpf_get_current_pid_tgid();

    struct connect_state *state = active_connects.lookup(&pid_tgid);
    if (!state)
        return 0;

    /* If connect failed immediately (not EINPROGRESS), clean up */
    if (ret != 0 && ret != -115) { /* -115 = EINPROGRESS */
        active_connects.delete(&pid_tgid);
        return 0;
    }

    return 0;
}

/*
 * Kprobe: tcp_close for outgoing connections
 * Emit dependency call event when outgoing connection closes
 */
int trace_dep_close(struct pt_regs *ctx, struct sock *sk) {
    u64 pid_tgid = bpf_get_current_pid_tgid();

    struct connect_state *state = active_connects.lookup(&pid_tgid);
    if (!state)
        return 0;

    u64 now = bpf_ktime_get_ns();

    struct dep_event event = {};
    event.start_ns = state->start_ns;
    event.duration_ns = now - state->start_ns;
    event.pid = pid_tgid >> 32;
    event.tid = (u32)pid_tgid;
    event.dest_ip = state->dest_ip;
    event.dest_port = state->dest_port;
    event.protocol = IPPROTO_TCP;

    /* Read accumulated bytes */
    u64 *sent = dep_bytes_sent.lookup(&pid_tgid);
    u64 *recv = dep_bytes_recv.lookup(&pid_tgid);
    if (sent) event.bytes_sent = *sent;
    if (recv) event.bytes_recv = *recv;

    bpf_get_current_comm(&event.comm, sizeof(event.comm));

    dep_events.perf_submit(ctx, &event, sizeof(event));

    active_connects.delete(&pid_tgid);
    dep_bytes_sent.delete(&pid_tgid);
    dep_bytes_recv.delete(&pid_tgid);

    return 0;
}
