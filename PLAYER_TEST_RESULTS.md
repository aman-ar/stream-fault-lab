# Player failure test results

Record actual app observations separately from server evidence. A WHEP HTTP 201 confirms signaling success; moving video must be confirmed in the app.

## Current results — WHEP on Android emulator

| Scenario | Fault / request scope | App error label or category | App description | Recovery observed | Status |
| --- | --- | --- | --- | --- | --- |
| Edge outage | Edge stopped for 15 seconds | Signaling / SIGNALING (user-reported label; numeric code not supplied) | `WHEP post failed with status 502` | Video resumed automatically, confirmed by user. Exact rendered-video recovery time not measured. | Automatic recovery observed in this run |
| HTTP 503 | New WHEP POSTs; 30-second fault | SIGNALING; code `10001`; `isFatal=false` | `WHEP POST failed with status 503: Lab injected HTTP 503` | Video started automatically after failures, confirmed by user. Exact rendered-video recovery time not measured. | Automatic recovery observed in this run |
| HTTP 502 | New WHEP POSTs | Not recorded | Not recorded | Not recorded | Pending app observation |
| Slow HTTP response | New WHEP POSTs | Not recorded | Not recorded | Not recorded | Pending app observation |
| HTTP request timeout | New WHEP POSTs | Not recorded | Not recorded | Not recorded | Pending app observation |
| ICE unreachable candidates | New WHEP answers; UDP/TCP candidates point to discard endpoints | No explicit ICE error reported; `Requested` and `AudioStarted` observed | Full trace not supplied | User reports continued retries and recovery on the next attempt after restoration | Automatic recovery observed in this run; detailed timings pending |
| VPN / TURN field observation | VPN enabled; compare with TURN configured | Not supplied | WHEP connection unstable with VPN; worked with TURN credentials configured | Successful operation with TURN reported; selected relay path and duration not captured | User-reported real-world case; controlled reproduction pending |
| Mid-stream media transport outage | Existing/new WebRTC traffic discarded over UDP and TCP; WHEP HTTP available | No playback error reported | No full trace supplied | Video froze, then resumed automatically, confirmed by user | Automatic recovery observed; exact duration/run and recovery timing not identified |
| DTLS fingerprint mismatch | New WHEP answers; certificate fingerprint changed | DTLS; code `10004`; ten nonfatal errors, then fatal | `WebRTC DTLS transport failed`; then `Ended(ERROR, DTLS)` | User confirms retries every 1 second and exhaustion after about 10 seconds; playback ended during the fault | DTLS detection and recovery exhaustion confirmed |

HLS results will be recorded separately when HLS is selected and tested. An input published through WHIP can be used for either output.

## Run 1 — edge outage, 2026-10-09

- Device: Android emulator (`emulator-5554`). User confirmed healthy moving video before testing.
- Output: WHEP, `http://10.0.2.2:8080/live/whep`; HLS output disabled.
- Input: OBS / WHIP. Proxyman was enabled; its exact mapping configuration was not captured in this record.
- Run ID: `20261009T212130Z-44747f`.
- Fault ID: `8e844e8dc008`.
- User report: error label “Signaling,” description “WHEP post failed with status 502.” The user confirmed that video resumed automatically after the outage.
- Full app trace, numeric error code, fatal/retryable flags, and time of the first recovered frame were not supplied. Do not reuse the error code from the earlier, unrelated Cronet empty-response setup failure.

### Server timeline

Times below use America/Toronto (EDT). The saved evidence retains the original UTC timestamps.

| Time on Oct 9 | Seconds after edge stopped | Evidence |
| --- | ---: | --- |
| 17:36:27.795 | 0.00 | Edge outage applied; configured duration 15 seconds |
| 17:36:32.558 | 4.76 | Fresh WHEP POST returned 502; gateway outcome `upstream_unavailable` |
| 17:36:35.585 | 7.79 | Another WHEP POST returned 502 |
| 17:36:41.613 | 13.82 | Another WHEP POST returned 502 |
| 17:36:43.112 | 15.32 | Edge restored and fault cleared |
| 17:36:51.648 | 23.85 | Fresh WHEP POST returned 201 and created a new session resource |

The successful POST completed approximately 8.54 seconds after edge restoration. This is signaling recovery timing, not the measured time when video resumed.

### Interpretation

The configured fault stopped the edge. During recovery, WHEP connection attempts could not reach it, so the gateway returned 502. The reported SIGNALING label describes those failed WHEP POST attempts; the supplied app report does not establish how the app classified the original loss of WebRTC media.

Automatic video recovery was confirmed by the user for this run. The server independently confirms repeated connection attempts and eventual signaling success. This result does not yet establish consistent recovery across repeated runs or other outage durations.

[Saved fault and request evidence](test-results/2026-10-09-whep-edge-outage.json)

## Run 2 — HTTP 503, 2026-10-09

- Output/device: WHEP on the Android emulator, using Proxyman.
- Actual injected fault: HTTP 503 for new WHEP POSTs, **30 seconds** (from the server trace). The edge remained running; failing POSTs were answered by the gateway without forwarding to the edge.
- Run ID: `20261009T212130Z-44747f`.
- Fault ID: `26eba100395e`.
- User confirmed that video started automatically after the failures.
- Existing playback continuing during injection was not explicitly reported and is not marked as verified.

### App observations

All three supplied events were emitted by `WebRtcPlayerAdapter` with `event=Error`, `protocol=WEB_RTC`, and `accepted=true`:

| App logcat time (Oct 9) | Error code | Category | Fatal | Description |
| --- | --- | --- | --- | --- |
| 17:40:20.846 | 10001 | SIGNALING | false | `WHEP POST failed with status 503: Lab injected HTTP 503` |
| 17:40:26.871 | 10001 | SIGNALING | false | Same description |
| 17:40:36.886 | 10001 | SIGNALING | false | Same description |

The supplied error events are approximately 6.03 and 10.02 seconds apart. They show repeated nonfatal signaling errors. A separate retryable flag was not supplied.

### Server evidence

Times use America/Toronto (EDT). App and server timestamps are retained separately; exact synchronization was not verified.

| Server time on Oct 9 | Evidence |
| --- | --- |
| 17:40:09.066 | HTTP 503 fault applied for 30 seconds |
| 17:40:17.874 | WHEP POST answered with injected 503 |
| 17:40:18.979 | WHEP POST answered with injected 503 |
| 17:40:21.992 | WHEP POST answered with injected 503 |
| 17:40:28.021 | WHEP POST answered with injected 503 |
| 17:40:38.040 | WHEP POST answered with injected 503 |
| 17:40:39.068 | Fault cleared automatically |
| 17:40:53.061 | Fresh WHEP POST returned 201 and created a session |

The first successful POST completed approximately **13.99 seconds after fault restoration**. This measures signaling recovery; the first recovered video-frame time was not recorded. The three app events and five gateway failures are different evidence sets; the provided excerpt does not establish the app identity of every gateway request.

### Outcome

Automatic recovery from a temporary WHEP POST 503 was observed in this run. The app emitted error code `10001`, category `SIGNALING`, and `isFatal=false`; the user confirmed automatic video start afterward. Repeated-run reliability and behavior after retry exhaustion remain untested. No assumptions are made about whether the client honors the gateway's `Retry-After` header.

[Saved app events and server evidence](test-results/2026-10-09-whep-http503.json)

## ICE, DTLS, and media coverage

These entries distinguish implemented lab faults from Android app observations. The new presets have lab verification; Android ICE recovery was reported by the user; DTLS detection and terminal playback have been captured; post-restoration recovery remains unreported.

| Layer | Separate test needed | Current lab coverage |
| --- | --- | --- |
| ICE connectivity | WHEP POST succeeds, but no advertised UDP or TCP candidate is reachable | Implemented `ice-unreachable`: keep WHEP HTTP 201, rewrite all UDP/TCP candidate ports to lab-owned discard endpoints for new connections. Existing media connections are unchanged. Established-path outages are a separate `media-outage` preset. |
| DTLS security | ICE succeeds, but the DTLS handshake fails, for example with a syntactically valid SDP fingerprint that does not match the edge certificate | Implemented `dtls-fingerprint`: keep HTTP 201 and reachable candidates, change the answer fingerprint without changing its valid SDP format. Existing connections are unchanged. Other DTLS failure modes remain untested. |
| Established transport | Interrupt an active connection, including ICE checks, while signaling stays available | Implemented `media-outage` through a bidirectional UDP/TCP relay. User confirmed video freeze followed by automatic recovery, with no playback error reported. |
| Media flow | Keep ICE connectivity alive while RTP audio/video packets stop | No dedicated media-only packet-drop preset. HTTP failures do not stop established WebRTC media. |

Read-only inspection of the Android checkout on 2026-10-09 found:

- `UnifiedPlayerEventMapper.WebRtcError` defines SIGNALING `10001`, ICE `10002`, MEDIA `10003`, DTLS `10004`, and RECOVERY_EXHAUSTED `10005` (category MEDIA).
- `WebRtcPlayerAdapter` handles session errors as SIGNALING and checks transport stats for `dtlsState == failed`, emitting a DTLS error through the recovery handler.
- The adapter currently has no call that emits `WebRtcError.ICE` and no ICE state subscription. The lower-level PeerConnection observer records ICE state, but the adapter does not map those transitions to the unified playback error trace. Defining code `10002` does not establish that the app emits it.
- The recovery manager can retry after stalled frames without an immediate ICE error event; a subsequent unsuccessful WHEP POST can produce the observed SIGNALING error.

This describes the current local source, including existing uncommitted Android changes. The installed emulator APK was not verified to match it. No Android source was modified during this inspection. The user subsequently reported no explicit ICE error, only Requested and AudioStarted, followed by recovery on the next attempt after the ICE fault ended. Full logcat, retry count, fault duration and recovery timing were not supplied. AudioStarted alone does not confirm received audio or rendered video. The supplied DTLS trace confirms code 10004 followed by fatal playback termination; post-restoration recovery remains unreported.

References: [WebRTC ICE states](https://www.w3.org/TR/webrtc/#rtciceconnectionstate-enum), [DTLS transport and fingerprint failures](https://www.w3.org/TR/webrtc/#rtcdtlstransport-interface).

## Fields to capture in subsequent runs

For each scenario, retain the selected protocol and scope, fault duration, full app error event (category, numeric code, description, fatal/retryable flags), retry attempts, whether moving video resumes automatically or after manual action, and recovery time. Use the trace run/fault IDs to match server events to app observations. Leave unobserved fields blank rather than inferring them from server status.

## Run 3 — ICE unreachable, user report (2026-10-09)

The user reported only `Requested` and `AudioStarted`, with no explicit ICE error. The player kept retrying, and recovered on the next attempt after the fault ended. This records observed automatic recovery and a missing explicit ICE error in the supplied report. No full trace, fault ID, duration, retry count, or rendered-frame recovery timestamp was supplied; those fields remain unmeasured.

### Server evidence for the latest completed ICE scenario

The running lab recorded fault `bcd58d01b5e6`, run `20261009T215947Z-ab62cf`, for 30 seconds. It applied at 22:02:53.746 UTC and restored at 22:03:23.747 UTC. Two new answers had their four UDP/TCP candidates changed. This supports that the lab injected the connectivity fault; user log timestamps and identities were not supplied for exact client correlation. The saved record retains the first healthy POST after restoration separately from rendered-video recovery.

[Saved ICE observation and server evidence](test-results/2026-10-09-whep-ice-unreachable.json)

## Run 4 — DTLS fingerprint mismatch, 2026-10-09

- Server: fault `fe89125830ee`, run `20261009T215947Z-ab62cf`; configured duration 30 seconds. Applied 22:04:33.401 UTC; restored 22:05:03.402 UTC. The gateway retained HTTP 201 and changed one certificate fingerprint on each affected answer.
- App: Request at 18:04:37.926, AudioStarted at 18:04:38.028, then eleven Error events from 18:04:38.938 through 18:04:49.315. All errors have code `10004`, category `DTLS`, description `WebRTC DTLS transport failed`, and accepted=true. The first ten have isFatal=false; the final one has isFatal=true.
- At 18:04:49.316 the app emitted `Ended`, reason `ERROR`, rawEndReason `DTLS`. First error to fatal error: approximately 10.38 seconds; Request to terminal event: approximately 11.39 seconds, calculated from supplied app timestamps.
- Error events are approximately one second apart. This is consistent with repeated recovery failures, but the playback trace alone does not prove one new PeerConnection or WHEP POST per event. Server evidence retains individual affected POSTs separately. Clock synchronization and exact client/server event correlation were not verified.
- The earlier HlsPlayerAdapter Ended(USER_EXIT) does not establish HLS fallback. AudioStarted does not establish successful audio receipt or video rendering. Automatic recovery after restoration is not confirmed by this excerpt.

[Saved app events and DTLS server evidence](test-results/2026-10-09-whep-dtls-fingerprint.json)

### DTLS recovery follow-up

The user confirmed: “player kept trying after 1 sec delay and exhausted in 10 seconds.” Record the approximately one-second retry interval and approximately ten-second recovery exhaustion as user observations. These agree with the ten nonfatal errors followed by a fatal error and Ended(ERROR, DTLS) in the supplied trace. The configured fault lasted 30 seconds, so playback terminated while the fault remained active. This is retry exhaustion, not a demonstrated recovery after restoration. The user did not supply a post-restoration playback observation.

## Client fix required — DTLS recovery exhausts too early

Status: noted for client implementation; fix not implemented. Requested by the user on 2026-10-09.

Observed problem: during the 30-second DTLS fingerprint fault, the Android player retried approximately once per second, exhausted recovery after approximately ten seconds, emitted fatal DTLS error 10004, and ended playback with ERROR / DTLS. The retry budget was consumed before the temporary fault cleared. Automatic recovery after restoration was not established in this run.

Expected behavior, clarified by the user: route DTLS failures through the existing recovery manager just like other recoverable playback errors. The manager must apply its configured backoff, retry budget, cancellation, reset, and exhaustion policy. Keep the DTLS classification and code 10004. DTLS-specific handling may require a fresh WHEP answer and PeerConnection, but it must not independently schedule retries or bypass the manager’s policy. Within that policy’s recovery window, a healthy connection should resume rendered video without manual reopening. Continue to reject mismatched fingerprints on failing attempts.

Implementation investigation: inspect the DTLS failure branch in WebRtcPlayerAdapter and the retry/backoff policy in WebRtcRecoveryManager. Check whether this branch bypasses normal backoff or whether duplicate failure callbacks consume the retry budget. These are investigation questions, not confirmed causes. Use the existing manager’s configured recovery window and terminal behavior. The requested fix is consistent application of that policy; it does not request a separate DTLS policy or an increase in the retry limit.

Acceptance checks for the client fix:

- Inject temporary DTLS faults of 5, 15, and 30 seconds. Within the configured recovery window, restoration leads to fresh signaling and moving video without manual reopening. Record first-frame recovery time.
- DTLS attempts use the existing manager’s configured delays and limits, verified against another recoverable error using the same policy. Repeated failure notifications for one attempt do not create concurrent retries or prematurely consume the budget. Failed sessions and peers are cleaned up.
- A persistent DTLS fault still reaches a documented, bounded terminal outcome; user exit cancels pending recovery.
- Logs identify attempts, delay, elapsed recovery time, and exhaustion separately from the underlying DTLS failure. Successful playback resets recovery state.

Evidence: Run 4 and test-results/2026-10-09-whep-dtls-fingerprint.json. This note requests an Android client fix; no server change is required to reproduce this observed retry-exhaustion behavior.

## Run 5 — mid-stream media transport outage, 2026-10-09

The user reported no playback error, then confirmed: “Video froze, then resumed automatically.” Record automatic video recovery as observed. An absent Error event alone does not establish a recovery failure or a client defect. Buffering, stall, retry, and recovery events were not supplied in a full trace.

The relay recorded UDP traffic discarded during recent 15-, 3-, 1-, and 2-second media outages in run `20261009T221229Z-1bba26`. The user did not identify which fault duration/ID the observation refers to; do not mark every duration as independently verified. Saved server evidence keeps those runs separate and retains counter deltas when available. First recovered-frame time, retry count, and whether recovery reused the same PeerConnection remain unmeasured.

Outcome: video stall and automatic recovery observed; explicit error classification not reported. Any requirement for a stall/error/recovered event is a separate client telemetry decision. The DTLS retry-exhaustion fix remains a separate open item.

[Saved mid-stream observation and server evidence](test-results/2026-10-09-whep-midstream-media-outage.json)

## Field observation — VPN instability and TURN

The user reported: “VPN caused unstable WHEP connection and worked with TURN credentials.” Record this as a real-world connectivity observation. It is not tied to a particular local lab run or fault. Endpoint, device, VPN configuration, TURN transport, exact error events, and observation duration were not supplied. Credentials are not included in this record.

Interpretation: this is consistent with a direct ICE path being unsuitable under the VPN and a TURN-assisted connection providing a usable alternative. The selected candidate pair was not captured, so neither actual relay selection nor UDP blocking is confirmed. TURN configuration alone does not prove that a relay candidate was selected. [WebRTC transports and relays](https://www.rfc-editor.org/rfc/rfc8835.html#section-3.4)

Prioritized reproduction checks:

- VPN off with direct connectivity: establish a measured playback baseline.
- VPN on with TURN unavailable: reproduce the instability and record ICE states, frame progression, and normal recovery-policy behavior.
- VPN on with valid TURN configured: confirm the selected candidate pair includes a relay candidate and measure successful playback, freezes, and reconnection. Record direct candidate transport separately from client-to-TURN transport; UDP, TCP, and TLS coverage are distinct.
- Follow-up: TURN unreachable or credentials rejected, and VPN toggled during established playback. Verify bounded recovery and cleanup rather than retry storms.

Lab gap: current local configuration has no STUN/TURN server. Controlled TURN validation needs an actual TURN service and an edge endpoint reachable through it. Direct ICE-TCP success in the current lab does not establish TURN TCP/TLS coverage.
