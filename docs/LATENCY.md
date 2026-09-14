# Latency measurements

There is no zero-delay claim. Software dispatch, OS scheduling, internet routing and your separate screen feed all contribute different delays.

## Reproducible local experiment

Run `python scripts/benchmark.py --output docs/latency-local.json` after installing the test extras. The committed [raw measurements](latency-local.json) contain 600 target acknowledgments per trial. Linux, Python 3.12.14, verified WSS/TLS over loopback, one host for controller/relay/target, and a dedicated target worker using a **fake input sink**. There is no native injection or screen feed in this experiment.

The reference policy deliberately flushes every 8 ms. The optimized policy wakes immediately using the production input queue. Both preserve input order and coalesce three adjacent moves into one; each trial reduces 1,000 generated input events to 600 frames. This is a controlled policy comparison, not a before/after measurement of an earlier shipped app. A second pair of trials adds 20 ms of ordered receive delay at each endpoint (40 ms additional round trip), with no artificial per-frame serialization bottleneck. Loss, jitter, bandwidth caps and public internet routes are not emulated.

All values below are **median / p95, in milliseconds**:

| Condition | Policy | Controller queue | Controller -> target -> controller RTT | Capture -> target ACK, including initial queue |
|---|---|---:|---:|---:|
| TLS loopback | 8 ms reference flush | 4.16 / 8.96 | 0.99 / 1.49 | 4.96 / 9.98 |
| TLS loopback | Immediate flush | 0.03 / 0.05 | 0.61 / 0.89 | 0.64 / 0.92 |
| TLS loopback + 40 ms simulated RTT | 8 ms reference flush | 4.13 / 7.74 | 41.70 / 42.40 | 45.73 / 49.70 |
| TLS loopback + 40 ms simulated RTT | Immediate flush | 0.03 / 0.04 | 41.77 / 42.56 | 41.80 / 42.59 |

Removing the artificial flush timer reduced initial local queue delay in this run. It did not remove network delay; RTT in the two 40 ms trials was essentially unchanged. Values vary with host load and scheduling.

Target queue median/p95 was 0.07/0.14 ms before and 0.05/0.08 ms after on loopback. With the 40 ms receive-delay simulation it was 0.07/0.10 ms before and 0.05/0.08 ms after. Fake-sink processing rounded to 0.000/0.002 ms in each trial; **those values do not measure Windows SendInput or macOS CGEventPost**. Detailed controller sender-queue measurements are included in the raw JSON.

## First live relay check

On 14 September 2026, the owner reported a successful Windows probe to `wss://172-86-119-204.sslip.io/ws`, with certificate verification, WebSocket upgrade, and the Python relay's diagnostic reply all succeeding.

| Measurement | Samples | Median | p95 |
|---|---:|---:|---:|
| That Windows PC to the deployed Ubuntu relay and back | 10 | 225.67 ms | 226.23 ms |

These are user-reported measurements over the actual client-to-relay connection. The PC's location, wired/Wi-Fi connection, and traffic conditions were not supplied. The target-to-relay leg, native injection time, full controller-to-target-to-controller RTT, and separate screen feed have not yet been measured on this pair. This small live sample is separate from the controlled optimization experiment above; the different networks cannot be compared as a before/after software result.

## Measure the real pair

While the controller is paused, type **M + Enter** after using control for a while. The rolling report contains:

- `controller_queue`: callback-enqueue to network handoff, including the oldest constituent age of coalesced moves.
- `controller_send_queue`: the final bounded sender queue before the websocket write.
- `controller_target_controller_rtt`: controller send-queue admission through the relay to the target and back after target processing. Probes work while paused; every 32nd input sequence adds an actual post-injection sample while controlling.
- `target_queue`: target receive-to-worker dispatch.
- `target_injection`: sampled duration of native input API calls, excluding no-op probes. This measures API processing, not when the destination application draws a response.
- `controller_relay_link_rtt`: websocket ping/pong on just the controller's relay connection.

The RTT clock is the controller's monotonic clock echoed by the target, so clocks do not need synchronization. Queue/injection clocks are local durations. RTT is **not one-way input delay**; dividing it by two assumes symmetric routing and symmetric processing, which have not been established. Capture-to-ACK is a return-trip diagnostic including local buffering. The visible delay also includes your independently supplied screen feed, which this app neither captures nor measures.

## Choose a relay using both PCs

Run **Measure Relay** on both PCs for each candidate deployment. For named output files, from a developer shell or the packaged binary:

```sh
RemoteInput probe --relay wss://CANDIDATE-HOST/ws --count 100 --output controller-candidate.json
RemoteInput probe --relay wss://CANDIDATE-HOST/ws --count 100 --output target-candidate.json
```

Run the first command on the controller and the second on the target. Repeat at the time of day you actually work, and record location, connection type, region and congestion. Compare both link median RTTs added together; use both p95 values to screen for unstable routes. A sum of p95 values is a comparison heuristic, not the measured p95 of the full path. Then validate the best candidates with the live target acknowledgments and actual input.

For a US/China pair, select the candidate with the lowest measured combined path and stable tails, rather than assuming a geographically central host is fastest. **One PC's live measurement is recorded above; measurements from the other PC and alternative relays are still needed before recommending a winning region.** Avoid a hosting plan that sleeps or suspends persistent websocket sessions. Placement advice must be checked against both actual networks.
